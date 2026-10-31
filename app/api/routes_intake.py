"""Intake and requests (design doc 10.1): POST /api/messages, confirm, reject, approve, lists."""

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile

from app.agent.runtime import answers_for_request, workflow_for
from app.api import jobs
from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.schemas import MessageRecord, MessageReply, RequestAction, RequestOut
from app.api.ws import hub
from app.domain.edits import CHARGE_ID, EditOp
from app.domain.models import Request, RequestStatus, Ward
from app.explain.agent import get_explanation
from app.intake import templates as tpl
from app.intake import voice
from app.intake.pipeline import handle_message
from app.llm.gateway import get_gateway
from app.planner import requests as lifecycle
from app.planner.runs import start_full, start_resolve
from app.store import repo

router = APIRouter(prefix="/api", tags=["intake"])

# Edits that queue for the next Generate when nothing is published yet, instead of starting a
# solve of their own: hard time-off plus every preference. Demand changes are charge-side and
# keep their current behavior.
QUEUED_WHEN_UNPUBLISHED = frozenset(
    {
        EditOp.MARK_UNAVAILABLE,
        EditOp.REQUEST_LEAVE,
        EditOp.PROPOSE_SWAP,
        EditOp.REQUEST_DAY_OFF,
        EditOp.REQUEST_SHIFT,
        EditOp.AVOID_SHIFT,
    }
)


def request_out(r: Request, outcomes: dict[str, dict[str, Any]]) -> RequestOut:
    return RequestOut.model_validate({**r.model_dump(mode="json"), "outcome": outcomes.get(r.id)})


@router.post("/messages", response_model=MessageReply)
async def post_message(
    conn: Conn,
    sender_id: Annotated[str, Form()],
    text: Annotated[str | None, Form()] = None,
    audio: Annotated[UploadFile | None, File()] = None,
    image: Annotated[UploadFile | None, File()] = None,
) -> MessageReply:
    """Intake (7.3): parse a message, or a voice note's transcript, into a ConstraintEdit, then
    confirm or clarify."""
    ward = require_ward(conn)
    if sender_id != CHARGE_ID and not any(n.id == sender_id for n in ward.nurses):
        raise ApiError(404, "NURSE_NOT_FOUND", f"No nurse {sender_id} in {ward.id}.")
    if image is not None:
        raise ApiError(400, "USE_PHOTO_IMPORT", "Roster photos go to POST /api/import/photo.")
    channel, transcript = "text", None
    if audio is not None:
        try:
            fmt = voice.audio_format(audio.content_type, audio.filename)
            transcript = await voice.transcribe(await audio.read(), fmt, get_gateway())
        except voice.VoiceError as exc:
            raise ApiError(422, exc.code, str(exc)) from exc
        channel, text = "voice", transcript
    if not text or not text.strip():
        raise ApiError(400, "EMPTY_MESSAGE", "Send some text.")
    res = await handle_message(
        conn, ward, sender_id, text, gateway=get_gateway(), log=jobs.live_log(conn, ward.id),
        channel=channel,
    )  # fmt: skip
    if (
        res.reply_kind == "confirm"
        and res.parse is not None
        and any(e.op in QUEUED_WHEN_UNPUBLISHED for e in res.parse.edits)
        and repo.get_published_run(conn, ward.id) is None
    ):
        lang = tpl.reply_lang(res.language)
        # the note goes in front: the normal confirmation below still shows the date and
        # shift the message was understood as, and the nurse confirms that understanding
        res.reply = "\n".join([tpl.text(lang, "confirm", "unpublished"), res.reply])
        repo.set_message_reply(
            conn,
            res.message_id,
            language=res.language,
            parse=res.parse.model_dump(mode="json"),
            reply=res.reply,
            reply_kind=res.reply_kind,
            request_ids=res.request_ids,
            explanation_id=res.explanation_id,
        )
    for rid in res.request_ids:
        await hub.broadcast(ward.id, {"type": "request_updated", "request_id": rid})
    return MessageReply(
        message_id=res.message_id,
        parse=res.parse,
        request_id=res.request_id,
        request_ids=res.request_ids,
        reply=res.reply,
        reply_kind=res.reply_kind,
        language=res.language,
        explanation_id=res.explanation_id,
        claims=res.claims,
        transcript=transcript,
    )


@router.get("/messages", response_model=list[MessageRecord])
def list_messages(conn: Conn, nurse_id: str, limit: int = 100) -> list[MessageRecord]:
    """A sender's chat history, oldest first, with the current status of each parsed request."""
    ward = require_ward(conn)
    outcomes = repo.latest_outcomes(conn, ward.id)
    out = []
    for m in repo.list_messages(conn, ward.id, nurse_id, limit):
        reqs = [repo.get_request(conn, rid) for rid in m["request_ids"]]
        ex = get_explanation(conn, m["explanation_id"]) if m.get("explanation_id") else None
        out.append(
            MessageRecord.model_validate(
                {
                    **m,
                    "requests": [request_out(r, outcomes) for r in reqs if r is not None],
                    "claims": [] if ex is None else ex["claims"],
                }
            )
        )
    return out


@router.get("/requests", response_model=list[RequestOut])
def list_requests(
    conn: Conn, nurse_id: str | None = None, status: RequestStatus | None = None
) -> list[RequestOut]:
    ward = require_ward(conn)
    outcomes = repo.latest_outcomes(conn, ward.id)
    rows = repo.list_requests(conn, ward.id, nurse_id=nurse_id, status=status)
    return [request_out(r, outcomes) for r in rows]


Action = Callable[..., lifecycle.Transition]


async def _act(
    conn: sqlite3.Connection,
    ward: Ward,
    request_id: str,
    action: Action,
    *,
    allow_solve: bool = True,
) -> RequestAction:
    try:
        t = action(conn, ward, request_id, log=jobs.live_log(conn, ward.id))
    except LookupError as exc:
        raise ApiError(404, "REQUEST_NOT_FOUND", f"No request {request_id}.") from exc
    except lifecycle.RequestStateError as exc:
        raise ApiError(409, "REQUEST_STATE", str(exc)) from exc
    run_id = None
    if t.solve and allow_solve:
        run_id = await start_solve(conn, ward, request_id, t.requests)
    for r in t.requests:
        await hub.broadcast(ward.id, {"type": "request_updated", "request_id": r.id})
    outcomes = repo.latest_outcomes(conn, ward.id)
    changed = [request_out(r, outcomes) for r in t.requests]
    main = next((x for x in changed if x.id == request_id), changed[0])
    return RequestAction(request=main, requests=changed, run_id=run_id)


async def start_solve(
    conn: sqlite3.Connection, ward: Ward, request_id: str, changed: list[Request]
) -> str:
    """A minimal-change re-solve with ranked replacements (6.2, 6.4) from the published roster;
    a full solve when nothing is published yet."""
    log = jobs.live_log(conn, ward.id)
    trigger = f"request:{request_id}"
    if repo.get_published_run(conn, ward.id) is None:
        prep = start_full(conn, ward.id, trigger=trigger, log=log)
        await hub.broadcast(ward.id, {"type": "run_started", "run_id": prep.run_id})
        jobs.spawn(prep)
        return prep.run_id
    affected = sorted(
        {d for r in changed for d in [*r.dates, *r.counterpart_dates]}
        | {d for r in changed if r.ref_request_id for d in _ref_dates(conn, r.ref_request_id)}
    )
    plan = start_resolve(
        conn, ward.id, trigger=trigger, affected=affected, request_id=request_id, log=log
    )
    await hub.broadcast(ward.id, {"type": "run_started", "run_id": plan.run_id})
    jobs.spawn_resolve(plan)
    return plan.run_id


def _ref_dates(conn: sqlite3.Connection, request_id: str) -> list[date]:
    r = repo.get_request(conn, request_id)
    return [] if r is None else list(r.dates)


def start_agent(conn: sqlite3.Connection, ward: Ward, out: RequestAction) -> str | None:
    """The roster agent takes over a confirmed sick call, demand change, leave or swap (7.10).
    The re-solve the confirm already started is handed over, so ranking runs while Muse thinks."""
    if repo.get_published_run(conn, ward.id) is None:
        return None
    for r in sorted(out.requests, key=lambda x: x.id != out.request.id):
        req = repo.get_request(conn, r.id)
        wf = None if req is None else workflow_for(req)
        if req is None or wf is None:
            continue
        prefetch = out.run_id if wf in ("sick_call", "demand_change") else None
        return jobs.agent_start(ward.id, wf, prefetch=prefetch, request_id=req.id,
                                counterpart=req.counterpart_id)  # fmt: skip
    return None


@router.post("/requests/{request_id}/confirm", response_model=RequestAction)
async def confirm_request(conn: Conn, request_id: str) -> RequestAction:
    """The sender confirms (7.3 step 7); a re-solve starts when the roster inputs changed, and
    the roster agent takes over the workflow. With nothing published yet the request just queues
    for the next Generate."""
    ward = require_ward(conn)
    queued = repo.get_published_run(conn, ward.id) is None
    out = await _act(conn, ward, request_id, lifecycle.confirm, allow_solve=not queued)
    start_agent(conn, ward, out)
    return out


@router.post("/requests/{request_id}/reject", response_model=RequestAction)
async def reject_request(conn: Conn, request_id: str) -> RequestAction:
    """The parse was wrong (sender), or the charge nurse declines a leave or swap."""
    ward = require_ward(conn)
    before = repo.get_request(conn, request_id)
    out = await _act(conn, ward, request_id, lifecycle.reject)
    if before is not None and before.status is RequestStatus.PENDING_APPROVAL:
        jobs.agent_answer(ward.id, answers_for_request(conn, ward.id, request_id, False, None))
    return out


@router.post("/requests/{request_id}/approve", response_model=RequestAction)
async def approve_request(conn: Conn, request_id: str) -> RequestAction:
    """The charge nurse approves a leave or swap (Q7); a waiting agent task continues with the
    re-solve this starts."""
    ward = require_ward(conn)
    out = await _act(conn, ward, request_id, lifecycle.approve)
    jobs.agent_answer(ward.id, answers_for_request(conn, ward.id, request_id, True, out.run_id))
    return out
