"""Request lifecycle after intake (design doc 7.3 step 7, Q7).

Confirm moves every `pending_confirm` request of the same message at once:
- unavailable, day off, shift wanted / avoided -> `active` (used by the next solve);
- leave and swaps from a nurse -> `pending_approval` until the charge nurse approves (Q7);
  from the charge nurse they are `active` straight away;
- cancel -> the referenced request is `withdrawn`, the cancel itself `applied`;
- demand -> the ward's target for each (date, shift) is set, the request `applied`.
`solve` says whether the roster inputs changed, so the caller starts a solve (a full re-solve
until M7 brings minimal change).
"""

import sqlite3
from dataclasses import dataclass, field

from app.domain.edits import CHARGE_ID
from app.domain.models import Request, RequestKind, RequestStatus, Ward
from app.log.decision_log import DecisionLog
from app.store import repo

NEEDS_APPROVAL = {RequestKind.LEAVE, RequestKind.SWAP}


class RequestStateError(Exception):
    """The request is not in a state that allows this action."""


@dataclass
class Transition:
    requests: list[Request] = field(default_factory=list)
    solve: bool = False


def _batch(conn: sqlite3.Connection, r: Request) -> list[Request]:
    """The pending_confirm requests parsed from the same message as `r` (one Confirm for all)."""
    rows = repo.requests_from_message(conn, r.source_message_id) if r.source_message_id else [r]
    return [x for x in rows if x.status is RequestStatus.PENDING_CONFIRM]


def _sender(conn: sqlite3.Connection, r: Request) -> str:
    msg = None if r.source_message_id is None else repo.get_message(conn, r.source_message_id)
    return str(msg["nurse_id"]) if msg else r.nurse_id


def _apply_demand(conn: sqlite3.Connection, ward: Ward, r: Request) -> None:
    if r.target_total is None:
        return
    t = r.target_total
    keys = {(d, s) for d in r.dates for s in r.shifts or []}
    for dm in ward.demand:
        if (dm.day, dm.shift) in keys:
            dm.target_total = t
            dm.min_total = min(dm.min_total, t)
            if dm.max_total is not None:
                dm.max_total = max(dm.max_total, t)
    repo.save_ward(conn, ward)


def confirm(
    conn: sqlite3.Connection, ward: Ward, request_id: str, *, log: DecisionLog | None = None
) -> Transition:
    r = repo.get_request(conn, request_id)
    if r is None:
        raise LookupError(request_id)
    if r.status is not RequestStatus.PENDING_CONFIRM:
        raise RequestStateError(f"request {request_id} is {r.status.value}, not pending_confirm")
    log = log or DecisionLog(conn, ward.id)
    batch = _batch(conn, r)
    sender = _sender(conn, r)
    out = Transition()
    for x in batch:
        status = RequestStatus.ACTIVE
        if x.kind in NEEDS_APPROVAL and sender != CHARGE_ID:
            status = RequestStatus.PENDING_APPROVAL
        elif x.kind is RequestKind.CANCEL:
            target = None if x.ref_request_id is None else repo.get_request(conn, x.ref_request_id)
            if target is not None and target.status is not RequestStatus.WITHDRAWN:
                out.solve |= target.status is RequestStatus.ACTIVE
                repo.set_request_status(conn, target.id, RequestStatus.WITHDRAWN)
            status = RequestStatus.APPLIED
        elif x.kind is RequestKind.DEMAND:
            _apply_demand(conn, ward, x)
            status, out.solve = RequestStatus.APPLIED, True
        else:
            out.solve = True
        repo.set_request_status(conn, x.id, status)
        log.emit(
            "request.confirmed",
            f"user:{sender}",
            None,
            None,
            request_id=x.id,
            status=status.value,
            edit=repo.get_request_edit(conn, x.id),
        )
        out.requests.append(repo.get_request(conn, x.id) or x)
    return out


def reject(
    conn: sqlite3.Connection, ward: Ward, request_id: str, *, log: DecisionLog | None = None
) -> Transition:
    """The sender says the parse was wrong (pending_confirm, the whole message), or the charge
    nurse declines a leave or swap (pending_approval)."""
    r = repo.get_request(conn, request_id)
    if r is None:
        raise LookupError(request_id)
    log = log or DecisionLog(conn, ward.id)
    if r.status is RequestStatus.PENDING_CONFIRM:
        batch = (
            repo.requests_from_message(conn, r.source_message_id) if r.source_message_id else [r]
        )
        batch = [x for x in batch if x.status is RequestStatus.PENDING_CONFIRM]
        actor = f"user:{_sender(conn, r)}"
    elif r.status is RequestStatus.PENDING_APPROVAL:
        batch, actor = [r], f"user:{CHARGE_ID}"
    else:
        raise RequestStateError(f"request {request_id} is {r.status.value}")
    out = Transition()
    for x in batch:
        repo.set_request_status(conn, x.id, RequestStatus.REJECTED)
        log.emit("request.rejected", actor, None, None, request_id=x.id, was=x.status.value)
        out.requests.append(repo.get_request(conn, x.id) or x)
    return out


def approve(
    conn: sqlite3.Connection, ward: Ward, request_id: str, *, log: DecisionLog | None = None
) -> Transition:
    """The charge nurse approves a leave or swap request (Q7); it becomes hard / forced."""
    r = repo.get_request(conn, request_id)
    if r is None:
        raise LookupError(request_id)
    if r.status is not RequestStatus.PENDING_APPROVAL:
        raise RequestStateError(f"request {request_id} is {r.status.value}, not pending_approval")
    repo.set_request_status(conn, r.id, RequestStatus.ACTIVE)
    (log or DecisionLog(conn, ward.id)).emit(
        "request.approved", f"user:{CHARGE_ID}", None, None, request_id=r.id, kind=r.kind.value
    )
    return Transition([repo.get_request(conn, r.id) or r], solve=True)
