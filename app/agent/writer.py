"""Message writer (design doc 7.7): Muse drafts the ask to the replacement (or swap counterpart)
in that nurse's language; code checks the draft.

Input: the nurse's name, the date, shift name and times, and the facts that explain why this nurse
was asked (standby, changed cells, tie-break). Never the absent nurse's reason. Post-checks: valid
JSON, at most 400 characters, contains the date and the shift name, no word from the health
blocklist. On any failure the template from `intake/templates/{en,hi}.toml` is used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError

from app.domain.models import ShiftCode, Ward
from app.explain.narrate import health_words
from app.intake import templates
from app.llm.cache import PROMPTS
from app.llm.gateway import LLMGateway
from app.llm.jsonproto import loads_json
from app.llm.types import LLMError, LLMRequest, Role

MAX_CHARS = 400


class Draft(BaseModel):
    message: str


@dataclass
class Written:
    text: str
    language: str  # en | hi
    source: str  # model | template
    problems: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    draft: str = ""  # the model's text before the checks (evals count privacy leaks in it)


def short_date(d: date) -> str:
    """The form every message must contain: "3 Nov"."""
    return f"{d.day} {d:%b}"


def reason_text(lang: str, facts: dict[str, Any]) -> str:
    if facts.get("tie") and facts.get("tie_break"):
        return templates.text(lang, "message", "reason_tie", tie_break=facts["tie_break"])
    cells = int(facts.get("changed_cells") or 0)
    if cells <= 1:
        key = "reason_standby" if facts.get("standby") else "reason_no_change"
        return templates.text(lang, "message", key)
    return templates.text(lang, "message", "reason_changes", cells=cells - 1)


def context(
    ward: Ward, nurse_id: str, day: date, shift: str, kind: str, facts: dict[str, Any]
) -> dict[str, Any]:
    nurse = ward.nurse(nurse_id)
    lang = templates.reply_lang(nurse.language)
    st = next((s for s in ward.shifts if s.code.value == shift), None)
    out = {
        "kind": kind,
        "nurse_name": nurse.name,
        "language": lang,
        "date": short_date(day),
        "weekday": f"{day:%A}",
        "shift_name": templates.table(lang)["shift"].get(shift, shift),
        "times": "" if st is None else f"{st.start}-{st.end}",
        "facts": {k: v for k, v in facts.items() if k not in ("absent", "reason")},
    }
    return out


def template(ctx: dict[str, Any]) -> str:
    lang = ctx["language"]
    if ctx["kind"] == "swap":
        f = ctx["facts"]
        return templates.text(lang, "message", "swap", name=ctx["nurse_name"],
                              requester=f.get("requester", ""), theirs=f.get("theirs", ""),
                              mine=f.get("mine", ""))  # fmt: skip
    return templates.text(
        lang, "message", "replacement", name=ctx["nurse_name"], shift=ctx["shift_name"],
        date=ctx["date"], times=ctx["times"], why=reason_text(lang, ctx["facts"]),
    )  # fmt: skip


def check(text: str, ctx: dict[str, Any]) -> list[str]:
    problems = []
    low = text.lower()
    if not text.strip():
        problems.append("empty message")
    if len(text) > MAX_CHARS:
        problems.append(f"{len(text)} characters (max {MAX_CHARS})")
    if ctx["kind"] == "replacement":
        if ctx["date"].lower() not in low:
            problems.append(f"date {ctx['date']!r} missing")
        if ctx["shift_name"].lower() not in low:
            problems.append(f"shift {ctx['shift_name']!r} missing")
    bad = health_words(text)
    if bad:
        problems.append(f"health words {bad}")
    return problems


async def write(
    ward: Ward,
    *,
    nurse_id: str,
    day: date,
    shift: str,
    kind: str = "replacement",
    facts: dict[str, Any] | None = None,
    gateway: LLMGateway,
    run_id: str | None = None,
) -> Written:
    ctx = context(ward, nurse_id, day, ShiftCode(shift).value, kind, facts or {})
    req = LLMRequest(
        Role.MESSAGE,
        [
            {"role": "system", "content": (PROMPTS / "message.md").read_text(encoding="utf-8")},
            {"role": "user", "content": "<context>"
             + json.dumps(ctx, ensure_ascii=False, sort_keys=True) + "</context>"},
        ],
        response_schema=Draft.model_json_schema(),
    )  # fmt: skip
    try:
        resp = await gateway.complete(req, run_id=run_id)
        d = Draft.model_validate(resp.parsed if resp.parsed is not None else loads_json(resp.text))
        text = " ".join(d.message.split())
        problems = check(text, ctx)
    except (LLMError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        text, problems = "", [f"{type(exc).__name__}: {str(exc)[:200]}"]
    if problems:
        return Written(template(ctx), ctx["language"], "template", problems, ctx, text)
    return Written(text, ctx["language"], "model", [], ctx, text)
