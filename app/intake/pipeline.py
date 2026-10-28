"""Intake pipeline (design doc 7.3 steps 1-6): a message -> ConstraintEdit -> pending requests.

1. Normalise (text only until M12). 2. Prompt: `prompts/intake.md` + few-shot examples + a
context block (sender, ward time, 35-day calendar, shifts, nurse list, open requests). 3. Call
`Role.INTAKE` with the ConstraintEdit schema. 4. Pydantic validation; one repair call with the
errors, then a "please rephrase" clarification. 5. Semantic validation (semantic.py). 6. Clarify,
or store `pending_confirm` requests and reply with a confirmation rendered from a template.

The model never writes the confirmation and never sets `hard`; step 7 (confirm) lives in
`app/planner/requests.py`.
"""

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.config import get_settings
from app.domain.edits import CHARGE_ID, ConstraintEdit, EditOp, constraint_edit_schema
from app.domain.models import Request, RequestStatus, Ward
from app.intake import templates as tpl
from app.intake.dates import calendar, ward_now
from app.intake.semantic import OPEN, Problem, ResolvedEdit, check_edit
from app.llm.cache import PROMPTS
from app.llm.gateway import LLMGateway
from app.llm.jsonproto import loads_json
from app.llm.types import LLMRequest, LLMResponse, Role
from app.log.decision_log import DecisionLog
from app.planner.build_input import horizon_dates
from app.store import repo

MIN_CONFIDENCE = 0.6
SCHEMA = constraint_edit_schema()


# ---------------------------------------------------------------- prompt (step 2)


def system_prompt() -> str:
    examples = []
    lines = (PROMPTS / "intake_examples.jsonl").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(x for x in lines if x.strip()):
        ex = json.loads(line)
        examples.append(
            f"Example {i + 1}. Today: {ex['today']} ({ex['weekday']}). Sender: {ex['sender']}.\n"
            f"Message: {ex['message']}\n"
            f"Output: {json.dumps(ex['output'], ensure_ascii=False)}"
        )
    body = (PROMPTS / "intake.md").read_text(encoding="utf-8")
    return body.replace("{examples}", "\n\n".join(examples))


def build_context(
    conn: sqlite3.Connection, ward: Ward, sender_id: str, now: datetime
) -> dict[str, Any]:
    today = now.date()
    cal = calendar(today)
    is_charge = sender_id == CHARGE_ID
    sender = next((n for n in ward.nurses if n.id == sender_id), None)
    open_reqs = [
        r
        for r in repo.list_requests(conn, ward.id, nurse_id=None if is_charge else sender_id)
        if r.status in OPEN and r.status is not RequestStatus.PENDING_CONFIRM
    ]
    period = horizon_dates(ward)
    ctx: dict[str, Any] = {
        "sender": {
            "id": sender_id,
            "name": "Charge nurse" if is_charge else (sender.name if sender else sender_id),
            "role": "charge" if is_charge else "nurse",
        },
        "today": today.isoformat(),
        "weekday": cal[0].weekday,
        "weekday_hi": cal[0].weekday_hi,
        "now": now.strftime("%H:%M"),
        "timezone": ward.tz,
        "calendar": [c.row() for c in cal],
        "shifts": [f"{s.code.value} | {s.name} | {s.start}-{s.end}" for s in ward.shifts],
        "nurses": [f"{n.id} | {n.name} | {', '.join(n.aliases)}" for n in ward.nurses],
        "active_requests": [
            {
                "id": r.id,
                "nurse": r.nurse_id,
                "kind": r.kind.value,
                "dates": [d.isoformat() for d in r.dates],
                "shifts": None if r.shifts is None else [s.value for s in r.shifts],
                "status": r.status.value,
            }
            for r in open_reqs
        ],
        "roster_period": f"{period[0].isoformat()} to {period[-1].isoformat()}",
    }
    if is_charge:
        ctx["demand"] = [
            {"date": d.day.isoformat(), "shift": d.shift.value, "target": d.target_total}
            for d in sorted(ward.demand, key=lambda d: (d.day, d.shift.value))
            if d.day >= today
        ]
    return ctx


def build_request(context: dict[str, Any], text: str) -> LLMRequest:
    user = (
        f"<context>{json.dumps(context, ensure_ascii=False)}</context>\n\n"
        f"Message from {context['sender']['name']} ({context['sender']['id']}):\n"
        f"<message>{text}</message>"
    )
    return LLMRequest(
        Role.INTAKE,
        [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}],
        response_schema=SCHEMA,
    )


# ---------------------------------------------------------------- call + schema (steps 3-4)


@dataclass
class ParseOutcome:
    edit: ConstraintEdit | None
    first_pass_ok: bool
    repaired: bool
    errors: list[str]
    calls: list[LLMResponse] = field(default_factory=list)


def _validate(resp: LLMResponse) -> tuple[ConstraintEdit | None, list[str]]:
    try:
        obj = resp.parsed if resp.parsed is not None else loads_json(resp.text)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, [f"not valid JSON: {exc}"]
    try:
        return ConstraintEdit.model_validate(obj), []
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(x) for x in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
        ]


async def parse(
    gateway: LLMGateway, req: LLMRequest, *, request_id: str | None = None
) -> ParseOutcome:
    """One call, and one repair call if the answer does not validate."""
    first = await gateway.complete(req, request_id=request_id)
    edit, errors = _validate(first)
    if edit is not None:
        return ParseOutcome(edit, True, False, [], [first])
    repair_text = (PROMPTS / "intake_repair.md").read_text(encoding="utf-8")
    repair = LLMRequest(
        Role.INTAKE,
        [
            *req.messages,
            {"role": "assistant", "content": first.text or ""},
            {"role": "user", "content": repair_text.replace("{errors}", "\n".join(errors))},
        ],
        response_schema=req.response_schema,
    )
    second = await gateway.complete(repair, request_id=request_id)
    edit2, errors2 = _validate(second)
    return ParseOutcome(edit2, False, True, errors if edit2 else errors + errors2, [first, second])


# ---------------------------------------------------------------- result (steps 5-6)


@dataclass
class IntakeResult:
    message_id: str
    parse: ConstraintEdit | None
    reply: str
    reply_kind: str  # confirm | clarify | answer | refuse
    language: str
    request_ids: list[str] = field(default_factory=list)
    resolved: list[ResolvedEdit] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    first_pass_ok: bool = False
    repaired: bool = False
    schema_failed: bool = False
    latency_ms: int = 0
    cost_usd: float = 0.0
    cache_hits: int = 0
    llm_calls: list[str] = field(default_factory=list)

    @property
    def request_id(self) -> str | None:
        return self.request_ids[0] if self.request_ids else None


def guess_language(text: str) -> str:
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    words = set(re.findall(r"[a-z]+", text.lower()))
    hinglish = {"nahi", "kal", "aaj", "mujhe", "chahiye", "hai", "subah", "raat", "duty", "kar"}
    return "hinglish" if len(words & hinglish) >= 2 else "en"


def summarize(lang: str, r: Request) -> str:
    """A short description of a stored request ("Day off, Tue 3 Nov")."""
    kind = r.kind.value.replace("_", " ")
    return f"{kind}, {tpl.days(lang, r.dates)}" if r.dates else kind


def confirmation(lang: str, conn: sqlite3.Connection, ward: Ward, items: list[ResolvedEdit]) -> str:
    """The confirmation text, from templates only (7.3 step 6)."""
    lines = []
    names = {n.id: n.name for n in ward.nurses}
    for it in items:
        e = it.edit
        p: dict[str, Any] = {
            "days": tpl.days(lang, e.dates),
            "shifts": tpl.shifts(lang, e.shifts),
            "duty": tpl.duty(lang, e.dates, e.shifts),
            "target": e.target_total,
            "request_id": e.request_id,
        }
        if e.op is EditOp.PROPOSE_SWAP:
            p["counterpart"] = names.get(it.counterpart_id or "", it.counterpart_id)
            p["mine"] = tpl.cell(lang, e.dates[0] if e.dates else None, e.shifts)
            p["theirs"] = tpl.cell(
                lang, e.counterpart_dates[0] if e.counterpart_dates else None, e.counterpart_shifts
            )
        if e.op is EditOp.CANCEL_REQUEST and e.request_id:
            r = repo.get_request(conn, e.request_id)
            p["summary"] = summarize(lang, r) if r else e.request_id
        line = tpl.text(lang, "confirm", e.op.value, **p)
        if it.for_other and it.nurse_id and e.op is not EditOp.CHANGE_DEMAND:
            line = f"{names.get(it.nurse_id, it.nurse_id)}: {line}"
        lines.append(line)
    lines.append(tpl.text(lang, "confirm", "question"))
    return "\n".join(lines)


def clarification(lang: str, parse: ConstraintEdit | None, problems: list[Problem]) -> str:
    if problems:
        p = problems[0]
        return tpl.text(lang, "clarify", p.code, **p.params)
    if parse is not None and parse.needs_clarification and parse.clarification:
        return parse.clarification
    return tpl.text(lang, "clarify", "generic" if parse is not None else "rephrase")


async def handle_message(
    conn: sqlite3.Connection,
    ward: Ward,
    sender_id: str,
    text: str,
    *,
    gateway: LLMGateway,
    now: datetime | None = None,
    log: DecisionLog | None = None,
    check_roster: bool = True,
) -> IntakeResult:
    t0 = time.perf_counter()
    now = now or ward_now(ward.tz, get_settings().demo_now)
    log = log or DecisionLog(conn, ward.id)
    actor = f"user:{sender_id}"
    text = text.strip()
    message_id = f"msg-{uuid.uuid4().hex[:10]}"
    repo.insert_message(
        conn,
        message_id=message_id,
        ward_id=ward.id,
        nurse_id=sender_id,
        body=text,
        created_at=repo.now_iso_ms(),
    )
    log.emit(
        "message.received", actor, None, None,
        message_id=message_id, channel="text", language_guess=guess_language(text),
    )  # fmt: skip

    req = build_request(build_context(conn, ward, sender_id, now), text)
    out = await parse(gateway, req)
    res = IntakeResult(
        message_id=message_id,
        parse=out.edit,
        reply="",
        reply_kind="clarify",
        language=out.edit.language if out.edit else guess_language(text),
        first_pass_ok=out.first_pass_ok,
        repaired=out.repaired,
        schema_failed=out.edit is None,
        cost_usd=sum(c.cost_usd for c in out.calls),
        cache_hits=sum(c.cache_hit for c in out.calls),
        llm_calls=[c.call_id for c in out.calls],
    )
    lang = tpl.reply_lang(res.language)
    last = out.calls[-1]
    if out.repaired:
        log.emit("intake.repaired", "llm:intake", None, None, errors=out.errors, ok=bool(out.edit))
    edit = out.edit

    if edit is None:
        res.reply = tpl.text(lang, "clarify", "rephrase")
        log.emit("intake.clarify", "llm:intake", None, None, message_id=message_id,
                 clarification=res.reply, errors=out.errors)  # fmt: skip
        return _finish(conn, res, t0)

    log.emit(
        "intake.parsed", "llm:intake", None, None,
        message_id=message_id, edit=edit.model_dump(mode="json"), confidence=edit.confidence,
        llm_call_id=last.call_id, cache_hit=last.cache_hit,
    )  # fmt: skip

    if edit.intent == "out_of_scope":
        res.reply, res.reply_kind = tpl.text(lang, "reply", "refuse"), "refuse"
        return _finish(conn, res, t0)
    if edit.intent == "greeting":
        res.reply, res.reply_kind = tpl.text(lang, "reply", "greeting"), "answer"
        return _finish(conn, res, t0)
    if edit.intent == "question":
        # the explainer answers these from M8 (7.5); until then an acknowledgement
        res.reply, res.reply_kind = tpl.text(lang, "reply", "question"), "answer"
        return _finish(conn, res, t0)

    problems: list[Problem] = []
    for e in edit.edits:
        resolved, probs = check_edit(
            conn, ward, e, sender_id=sender_id, message=text, now=now, lang=res.language,
            check_roster=check_roster,
        )  # fmt: skip
        if resolved is not None:
            resolved.for_other = sender_id == CHARGE_ID
            res.resolved.append(resolved)
        problems += probs
    res.problems = problems
    unclear = edit.needs_clarification or edit.confidence < MIN_CONFIDENCE or not edit.edits
    if problems or unclear:
        res.reply = clarification(lang, edit, problems)
        log.emit("intake.clarify", "llm:intake", None, None, message_id=message_id,
                 clarification=res.reply, problems=[p.code for p in problems])  # fmt: skip
        return _finish(conn, res, t0)

    created = datetime.now().astimezone()
    for it in res.resolved:
        rid = repo.next_request_id(conn)
        r = Request.from_edit(
            it.edit,
            id=rid,
            nurse_id=it.nurse_id or CHARGE_ID,
            counterpart_id=it.counterpart_id,
            created_at=created,
            source_message_id=message_id,
        )
        payload = {"edit": it.edit.model_dump(mode="json"), "resolved": it.key()}
        repo.insert_request(conn, ward.id, r, edit_json=json.dumps(payload, ensure_ascii=False))
        res.request_ids.append(rid)
    res.reply, res.reply_kind = confirmation(lang, conn, ward, res.resolved), "confirm"
    return _finish(conn, res, t0)


def _finish(conn: sqlite3.Connection, res: IntakeResult, t0: float) -> IntakeResult:
    res.latency_ms = int((time.perf_counter() - t0) * 1000)
    repo.set_message_reply(
        conn,
        res.message_id,
        language=res.language,
        parse=None if res.parse is None else res.parse.model_dump(mode="json"),
        reply=res.reply,
        reply_kind=res.reply_kind,
        request_ids=res.request_ids,
    )
    return res
