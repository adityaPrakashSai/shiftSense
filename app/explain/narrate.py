"""Conflict narrator (design doc 7.6): Muse writes a short summary over the options; code checks
it. The options themselves always come from code (planner/conflicts.py).

Validation: the same option ids, in the same order, nothing added, a non-empty summary and labels,
and no health words (the absent nurse's privacy). If anything fails, the template text is used.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, ValidationError

from app.llm.cache import PROMPTS
from app.llm.gateway import LLMGateway
from app.llm.jsonproto import loads_json
from app.llm.types import LLMError, LLMRequest, Role
from app.log.decision_log import DecisionLog
from app.store import repo

HEALTH = ["fever", "bukhar", "sick", "bimar", "ill", "illness", "symptom", "hospitalised",
          "pregnan", "injur", "medical", "doctor", "health"]  # fmt: skip


class NarrOption(BaseModel):
    option_id: str
    label: str


class Narrative(BaseModel):
    summary: str
    options: list[NarrOption]


def template(conflict: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    core = conflict["core"]["names"]
    short = [c for c in core if c.startswith(("min_cover", "senior_cover"))]
    who = sorted({names.get(c.split(":")[1], c.split(":")[1]) for c in core
                  if c.startswith(("unavailable", "leave"))})  # fmt: skip
    if short:
        what = "The roster cannot meet the minimum cover for " + ", ".join(
            s.split(":", 1)[1] for s in short
        )
        what += f" while {', '.join(who)} {'is' if len(who) == 1 else 'are'} away." if who else "."
    else:
        what = "The roster cannot meet these rules together: " + ", ".join(core) + "."
    return {
        "summary": what + " Pick one option.",
        "options": [
            {"option_id": o["option_id"], "label": o["label"]} for o in conflict["options"]
        ],
        "source": "template",
    }


def validate(n: Narrative, conflict: dict[str, Any]) -> list[str]:
    problems = []
    want = [o["option_id"] for o in conflict["options"]]
    got = [o.option_id for o in n.options]
    if got != want:
        problems.append(f"option ids {got} != {want}")
    text = " ".join([n.summary, *(o.label for o in n.options)]).lower()
    if not n.summary.strip() or any(not o.label.strip() for o in n.options):
        problems.append("empty text")
    bad = [w for w in HEALTH if w in text.split() or (len(w) > 4 and w in text)]
    if bad:
        problems.append(f"health words {bad}")
    return problems


async def narrate(
    conn: sqlite3.Connection,
    conflict_id: str,
    *,
    gateway: LLMGateway,
    log: DecisionLog | None = None,
) -> dict[str, Any]:
    c = repo.get_conflict(conn, conflict_id)
    if c is None:
        raise LookupError(conflict_id)
    run = repo.get_run(conn, c["run_id"])
    ward = repo.get_ward(conn, None if run is None else str(run["ward_id"]))
    assert ward is not None
    names = {n.id: n.name for n in ward.nurses}
    ctx = {
        "core": c["core"]["names"],
        "context": [
            {
                "name": x["name"],
                "cannot_work_because": sorted({r.split(":")[0] for r in x["rules"]}),
            }
            for x in c["core"].get("context", [])
        ],
        "options": [{"option_id": o["option_id"], "label": o["label"]} for o in c["options"]],
        "nurses": {k: v for k, v in names.items() if any(k in x for x in c["core"]["names"])},
    }
    req = LLMRequest(
        Role.NARRATE,
        [
            {"role": "system", "content": (PROMPTS / "narrate.md").read_text(encoding="utf-8")},
            {"role": "user",
             "content": f"<context>{json.dumps(ctx, ensure_ascii=False)}</context>"},
        ],
        response_schema=Narrative.model_json_schema(),
    )  # fmt: skip
    out = template(c, names)
    problems: list[str] = []
    try:
        resp = await gateway.complete(req, run_id=c["run_id"])
        n = Narrative.model_validate(
            resp.parsed if resp.parsed is not None else loads_json(resp.text)
        )
        problems = validate(n, c)
        if not problems:
            out = {**n.model_dump(), "source": "model"}
    except (LLMError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        problems = [f"{type(exc).__name__}: {str(exc)[:200]}"]
    out["problems"] = problems
    repo.set_conflict_narrative(conn, conflict_id, out)
    (log or DecisionLog(conn, ward.id)).emit(
        "conflict.narrated", "llm:narrate", c["run_id"], None,
        conflict_id=conflict_id, summary=out["summary"], source=out["source"], problems=problems,
    )  # fmt: skip
    return out
