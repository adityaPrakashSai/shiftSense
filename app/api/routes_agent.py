"""Roster agent and nurse inbox (design doc 7.10, 6.5, 10.1, 10.3): the Agent tab's task traces,
the replacement inbox, and the nurse's Accept / Decline that resumes the waiting task."""

import sqlite3
from typing import Any

from fastapi import APIRouter

from app.api import jobs
from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.schemas import (
    AgentTaskOut,
    ApprovalAsk,
    NurseMessageOut,
    ProposalRespondBody,
    RespondBody,
)
from app.api.ws import hub
from app.store import repo

router = APIRouter(prefix="/api", tags=["agent"])


def task_out(t: dict[str, Any], *, calls: bool = False) -> AgentTaskOut:
    st = t["state"]
    return AgentTaskOut.model_validate(
        {
            **t,
            "trace": st.get("trace", []),
            "waiting": st.get("waiting"),
            "counters": st.get("counters", {}),
            "fallback_reason": st.get("fallback_reason"),
            "calls": [
                {**c, "result": c.get("result") or {}} for c in st.get("calls", [])
            ] if calls else [],
        }
    )  # fmt: skip


def approval_for_run(conn: sqlite3.Connection, ward_id: str, run_id: str) -> ApprovalAsk | None:
    """The agent task waiting for the charge nurse's decision on this proposal, if any."""
    for t in repo.list_agent_tasks(conn, ward_id, status="waiting_approval"):
        w = t["state"].get("waiting") or {}
        if run_id in w.get("runs", []):
            return ApprovalAsk(task_id=t["id"], workflow=t["workflow"], kind=w["approval_kind"],
                               ref=w["ref"], summary=w["summary"], claims=w.get("claims", []),
                               summary_source=w.get("summary_source", "agent"))  # fmt: skip
    return None


@router.get("/agent/tasks", response_model=list[AgentTaskOut])
def list_tasks(conn: Conn, status: str | None = None, limit: int = 20) -> list[AgentTaskOut]:
    """Newest first, with each task's step trace (the Agent tab)."""
    ward = require_ward(conn)
    rows = repo.list_agent_tasks(conn, ward.id, status=status, limit=limit)
    return [task_out(t) for t in rows if t["status"] != "starting"]


@router.get("/agent/tasks/{task_id}", response_model=AgentTaskOut)
def get_task(conn: Conn, task_id: str) -> AgentTaskOut:
    t = repo.get_agent_task(conn, task_id)
    if t is None:
        raise ApiError(404, "TASK_NOT_FOUND", f"No agent task {task_id}.")
    return task_out(t, calls=True)


@router.get("/inbox", response_model=list[NurseMessageOut])
def inbox(conn: Conn, nurse_id: str | None = None) -> list[NurseMessageOut]:
    """Messages to nurses (replacement and swap asks), newest first; all nurses when no id."""
    ward = require_ward(conn)
    rows = repo.list_nurse_messages(conn, ward.id, nurse_id=nurse_id)
    return [NurseMessageOut.model_validate(r) for r in rows]


async def answer(conn: sqlite3.Connection, ward_id: str, m: dict[str, Any],
                 accept: bool) -> NurseMessageOut:  # fmt: skip
    if m["status"] != "asked":
        raise ApiError(409, "ALREADY_ANSWERED", f"Message {m['id']} is {m['status']}.")
    status = "accepted" if accept else "declined"
    repo.set_nurse_message_status(conn, m["id"], status)
    actor = f"user:{m['nurse_id']}"
    if m["kind"] == "replacement" and m["run_id"]:
        prop = repo.proposal_for_run(conn, m["run_id"])
        if prop is not None:
            repo.set_replacement_status(conn, prop["run_id"], status)
        jobs.live_log(conn, ward_id).emit(f"replacement.{status}", actor, m["run_id"], None,
                                          message_id=m["id"], nurse=m["nurse_id"])  # fmt: skip
    else:
        jobs.live_log(conn, ward_id).emit(
            f"swap.{status}", actor, None, None, message_id=m["id"], request_id=m["request_id"]
        )
    from app.agent import runtime

    jobs.agent_answer(ward_id, runtime.answers_for_reply(conn, ward_id, m["id"], accept))
    await hub.broadcast(ward_id, {"type": "message_for_nurse", "nurse_id": m["nurse_id"]})
    out = repo.get_nurse_message(conn, m["id"])
    assert out is not None
    return NurseMessageOut.model_validate(out)


@router.post("/inbox/{message_id}/respond", response_model=NurseMessageOut)
async def respond(conn: Conn, message_id: str, body: RespondBody) -> NurseMessageOut:
    """The nurse taps Accept or Decline; the waiting agent task continues (6.5)."""
    ward = require_ward(conn)
    m = repo.get_nurse_message(conn, message_id)
    if m is None:
        raise ApiError(404, "MESSAGE_NOT_FOUND", f"No message {message_id}.")
    return await answer(conn, ward.id, m, body.accept)


@router.post("/proposals/{run_id}/respond", response_model=NurseMessageOut)
async def respond_proposal(conn: Conn, run_id: str, body: ProposalRespondBody) -> NurseMessageOut:
    """The replacement's answer by proposal (10.1): the open ask to that nurse for this run."""
    ward = require_ward(conn)
    prop = repo.proposal_for_run(conn, run_id)
    if prop is None:
        raise ApiError(404, "PROPOSAL_NOT_FOUND", f"No proposal {run_id}.")
    runs = {prop["run_id"], *(c["run_id"] for c in prop["ranking"].get("candidates", []))}
    asks = [m for m in repo.list_nurse_messages(conn, ward.id, nurse_id=body.nurse_id)
            if m["run_id"] in runs and m["status"] == "asked"]  # fmt: skip
    if not asks:
        raise ApiError(404, "NO_OPEN_ASK", f"{body.nurse_id} has no open ask for {run_id}.")
    return await answer(conn, ward.id, asks[0], body.accept)
