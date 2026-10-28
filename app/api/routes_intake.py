"""Intake and requests (design doc 10.1): POST /api/messages, confirm, reject, approve, lists."""

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile

from app.api import jobs
from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.schemas import MessageRecord, MessageReply, RequestAction, RequestOut
from app.api.ws import hub
from app.domain.edits import CHARGE_ID
from app.domain.models import Request, RequestStatus, Ward
from app.intake.pipeline import handle_message
from app.llm.gateway import get_gateway
from app.planner import requests as lifecycle
from app.planner.runs import start_full, start_resolve
from app.store import repo

router = APIRouter(prefix="/api", tags=["intake"])


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
    """Intake (7.3): parse a message into a ConstraintEdit, then confirm or clarify."""
    ward = require_ward(conn)
    if sender_id != CHARGE_ID and not any(n.id == sender_id for n in ward.nurses):
        raise ApiError(404, "NURSE_NOT_FOUND", f"No nurse {sender_id} in {ward.id}.")
    if audio is not None or image is not None:
        raise ApiError(400, "MEDIA_NOT_SUPPORTED", "Voice notes and photos arrive in M12.")
    if not text or not text.strip():
        raise ApiError(400, "EMPTY_MESSAGE", "Send some text.")
    res = await handle_message(
        conn, ward, sender_id, text, gateway=get_gateway(), log=jobs.live_log(conn, ward.id)
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
    )


@router.get("/messages", response_model=list[MessageRecord])
def list_messages(conn: Conn, nurse_id: str, limit: int = 100) -> list[MessageRecord]:
    """A sender's chat history, oldest first, with the current status of each parsed request."""
    ward = require_ward(conn)
    outcomes = repo.latest_outcomes(conn, ward.id)
    out = []
    for m in repo.list_messages(conn, ward.id, nurse_id, limit):
        reqs = [repo.get_request(conn, rid) for rid in m["request_ids"]]
        out.append(
            MessageRecord.model_validate(
                {**m, "requests": [request_out(r, outcomes) for r in reqs if r is not None]}
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
    conn: sqlite3.Connection, ward: Ward, request_id: str, action: Action
) -> RequestAction:
    try:
        t = action(conn, ward, request_id, log=jobs.live_log(conn, ward.id))
    except LookupError as exc:
        raise ApiError(404, "REQUEST_NOT_FOUND", f"No request {request_id}.") from exc
    except lifecycle.RequestStateError as exc:
        raise ApiError(409, "REQUEST_STATE", str(exc)) from exc
    run_id = None
    if t.solve:
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


@router.post("/requests/{request_id}/confirm", response_model=RequestAction)
async def confirm_request(conn: Conn, request_id: str) -> RequestAction:
    """The sender confirms (7.3 step 7); a full re-solve starts when the roster inputs changed."""
    return await _act(conn, require_ward(conn), request_id, lifecycle.confirm)


@router.post("/requests/{request_id}/reject", response_model=RequestAction)
async def reject_request(conn: Conn, request_id: str) -> RequestAction:
    """The parse was wrong (sender), or the charge nurse declines a leave or swap."""
    return await _act(conn, require_ward(conn), request_id, lifecycle.reject)


@router.post("/requests/{request_id}/approve", response_model=RequestAction)
async def approve_request(conn: Conn, request_id: str) -> RequestAction:
    """The charge nurse approves a leave or swap (Q7)."""
    return await _act(conn, require_ward(conn), request_id, lifecycle.approve)
