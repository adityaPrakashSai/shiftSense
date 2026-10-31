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


def cascade_progress(t: dict[str, Any]) -> dict[str, Any] | None:
    """The contact plan and who answered how, for the Agent tab (None: no plan involved)."""
    st = t["state"]
    w = st.get("waiting") or {}
    ac = st.get("approved_cascade") or {}
    order = ac.get("order") or w.get("cascade") or []
    if not order:
        return None
    asked: dict[str, str] = {}
    for c in st.get("calls", []):
        r = c.get("result") or {}
        if c["tool"] == "wait_for_reply" and r.get("reply"):
            asked[str(r.get("nurse_id"))] = str(r["reply"])
    if t["status"] == "waiting_reply" and w.get("nurse_id"):
        asked.setdefault(str(w["nurse_id"]), "waiting")
    return {"order": [str(n) for n in order], "asked": asked, "approved": bool(ac),
            "deadline_at": w.get("deadline_at"), "since": w.get("since")}  # fmt: skip


def task_out(t: dict[str, Any], *, calls: bool = False) -> AgentTaskOut:
    st = t["state"]
    return AgentTaskOut.model_validate(
        {
            **t,
            "trace": st.get("trace", []),
            "waiting": st.get("waiting"),
            "counters": st.get("counters", {}),
            "fallback_reason": st.get("fallback_reason"),
            "cascade": cascade_progress(t),
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
                               summary_source=w.get("summary_source", "agent"),
                               cascade=w.get("cascade"))  # fmt: skip
    return None


@router.post("/agent/tasks/{task_id}/approve-cascade", response_model=AgentTaskOut)
async def approve_cascade(conn: Conn, task_id: str) -> AgentTaskOut:
    """Approve the task's contact plan ("ask B, then C, then D"). This publishes nothing:
    the first accept still comes back for one Publish."""
    ward = require_ward(conn)
    t = repo.get_agent_task(conn, task_id)
    if t is None:
        raise ApiError(404, "TASK_NOT_FOUND", f"No agent task {task_id}.")
    w = t["state"].get("waiting") or {}
    if t["status"] != "waiting_approval" or not w.get("cascade"):
        raise ApiError(
            409, "CASCADE_NOT_PENDING", f"Task {task_id} is not waiting for a contact plan."
        )
    jobs.live_log(conn, ward.id).emit(
        "agent.cascade_approved",
        "user:charge",
        None,
        None,
        task_id=task_id,
        run_id=w["ref"],
        order=w["cascade"],
    )
    jobs.agent_answer(ward.id, [(task_id, {"decision": "approved", "run_id": w["ref"],
                                           "cascade": w["cascade"],
                                           "parent_run": w.get("parent_run")})])  # fmt: skip
    out = repo.get_agent_task(conn, task_id)
    assert out is not None
    return task_out(out)


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
    if m["status"] == "expired":
        raise ApiError(409, "ASK_EXPIRED", f"Message {m['id']} expired without an answer.")
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
    from app.agent import loop, runtime

    if accept and m["kind"] == "replacement":
        for moot in loop.withdraw_open_asks(conn, ward_id, jobs.live_log(conn, ward_id), actor,
                                            day=m["day"], shift=m["shift"],
                                            except_id=m["id"]):  # fmt: skip
            await hub.broadcast(ward_id, {"type": "message_for_nurse",
                                          "nurse_id": moot["nurse_id"]})  # fmt: skip
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
