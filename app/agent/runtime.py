"""Start and resume roster-agent tasks (design doc 7.10), and match people's answers to the task
that waits for them.

- `workflow_for(request)` picks the workflow a confirmed request starts;
- `start` creates the `agent_task` and runs it until it waits, finishes or escalates;
- `answers_for_*` turn an API event (a published run, an approved or rejected request, a chosen
  conflict option, a nurse's reply) into (task id, answer) pairs; `resume` delivers one.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.agent import loop
from app.agent.env import Env
from app.agent.tools import AgentCtx
from app.domain.edits import CHARGE_ID
from app.domain.models import Request, RequestKind, RequestStatus, RunStatus, Ward
from app.llm.gateway import LLMGateway
from app.log.decision_log import DecisionLog
from app.store import repo

Answer = tuple[str, dict[str, Any]]


def workflow_for(r: Request, sender: str | None = None) -> str | None:
    """The workflow a request starts once confirmed (None: a plain re-plan, no agent needed)."""
    if r.kind is RequestKind.DEMAND and r.status is RequestStatus.APPLIED:
        return "demand_change"
    if r.kind in (RequestKind.UNAVAILABLE, RequestKind.LEAVE) and r.status is RequestStatus.ACTIVE:
        return "sick_call"
    if r.kind is RequestKind.LEAVE and r.status is RequestStatus.PENDING_APPROVAL:
        return "leave"
    if r.kind is RequestKind.SWAP and (
        r.status is RequestStatus.PENDING_APPROVAL
        or (r.status is RequestStatus.ACTIVE and r.counterpart_id is None)
    ):
        return "swap"
    return None


def next_task_id(conn: sqlite3.Connection) -> str:
    (n,) = conn.execute("SELECT COUNT(*) FROM agent_task").fetchone()
    return f"task-{int(n) + 1:04d}"


def make_ctx(conn: sqlite3.Connection, ward: Ward, task: dict[str, Any], *, env: Env,
             gateway: LLMGateway, log: DecisionLog) -> AgentCtx:  # fmt: skip
    return AgentCtx(conn, ward, task, env, gateway, log)


async def start(
    conn: sqlite3.Connection,
    ward: Ward,
    workflow: str,
    *,
    env: Env,
    gateway: LLMGateway,
    log: DecisionLog,
    prefetch: str | None = None,
    task_id: str | None = None,
    mode: str = "agent",
    **trigger: Any,
) -> str:
    """`mode="fallback"` runs the fixed-code workflow from the start (the eval's baseline)."""
    tid = task_id or next_task_id(conn)
    ref = next((f"{k}:{v}" for k, v in trigger.items() if v), workflow)
    state = loop.new_state(**{k: v for k, v in trigger.items() if v})
    if prefetch:
        state["prefetch"] = prefetch
        state["last_run"] = prefetch
    repo.insert_agent_task(conn, tid, ward_id=ward.id, workflow=workflow, trigger_ref=ref,
                           state=state)  # fmt: skip
    task = repo.get_agent_task(conn, tid)
    assert task is not None
    task["mode"] = mode
    ctx = make_ctx(conn, ward, task, env=env, gateway=gateway, log=log)
    task["messages"] = loop.first_messages(ctx)
    log.emit("agent.started", "llm:agent", None, None, about_run=prefetch, task_id=tid,
             workflow=workflow, trigger=ref)  # fmt: skip
    await loop.advance(ctx)
    return tid


async def resume(
    conn: sqlite3.Connection, ward: Ward, task_id: str, answer: dict[str, Any], *, env: Env,
    gateway: LLMGateway, log: DecisionLog,
) -> None:  # fmt: skip
    task = repo.get_agent_task(conn, task_id)
    if task is None:
        return
    await loop.resume(make_ctx(conn, ward, task, env=env, gateway=gateway, log=log), answer)


def _waiting(conn: sqlite3.Connection, ward_id: str, status: str) -> list[dict[str, Any]]:
    return repo.list_agent_tasks(conn, ward_id, status=status, limit=200)


def _approvals(conn: sqlite3.Connection, ward_id: str, kind: str) -> list[dict[str, Any]]:
    out = []
    for t in _waiting(conn, ward_id, "waiting_approval"):
        w = t["state"].get("waiting") or {}
        if w.get("approval_kind") == kind:
            out.append(t | {"waiting": w})
    return out


def answers_for_published(conn: sqlite3.Connection, ward_id: str, run_id: str) -> list[Answer]:
    """The charge nurse published `run_id`: approved for the task that proposed it; every other
    run-approval wait whose run is no longer proposed was superseded."""
    out: list[Answer] = []
    prop = repo.proposal_for_run(conn, run_id)
    nurse = None if prop is None else prop["chosen_nurse"]
    for t in _approvals(conn, ward_id, "run"):
        w = t["waiting"]
        if run_id in w["runs"]:
            out.append((t["id"], {"decision": "approved", "run_id": run_id, "nurse": nurse}))
            continue
        run = repo.get_run(conn, str(w["ref"]))
        if run is None or run["status"] != RunStatus.PROPOSED.value:
            out.append((t["id"], {"decision": "superseded", "run_id": run_id}))
    return out


def answers_for_request(
    conn: sqlite3.Connection, ward_id: str, request_id: str, approved: bool,
    prefetch: str | None,
) -> list[Answer]:  # fmt: skip
    out: list[Answer] = []
    for t in _approvals(conn, ward_id, "request"):
        if t["waiting"]["ref"] != request_id:
            continue
        ans: dict[str, Any] = {"decision": "approved" if approved else "rejected",
                               "request_id": request_id, "by": CHARGE_ID}  # fmt: skip
        if approved and prefetch:
            ans["prefetch"] = prefetch
        out.append((t["id"], ans))
    return out


def answers_for_conflict(
    conn: sqlite3.Connection, ward_id: str, conflict_id: str, option_id: str, run_id: str
) -> list[Answer]:
    return [
        (t["id"], {"decision": "approved", "conflict_id": conflict_id, "option_id": option_id,
                   "prefetch": run_id})
        for t in _approvals(conn, ward_id, "conflict")
        if t["waiting"]["ref"] == conflict_id
    ]  # fmt: skip


def answers_for_reply(
    conn: sqlite3.Connection, ward_id: str, message_id: str, accepted: bool
) -> list[Answer]:
    out: list[Answer] = []
    for t in _waiting(conn, ward_id, "waiting_reply"):
        w = t["state"].get("waiting") or {}
        if w.get("message_id") == message_id:
            out.append(
                (
                    t["id"],
                    {
                        "reply": "accepted" if accepted else "declined",
                        "nurse_id": w.get("nurse_id"),
                        "message_id": message_id,
                    },
                )
            )
    return out


def answers_for_conditional(
    conn: sqlite3.Connection, ward_id: str, message_id: str, condition: dict[str, Any]
) -> list[Answer]:
    """The task waiting on this message (if any still is) gets the "yes, if..."."""
    out: list[Answer] = []
    for t in _waiting(conn, ward_id, "waiting_reply"):
        w = t["state"].get("waiting") or {}
        if w.get("message_id") == message_id:
            out.append(
                (
                    t["id"],
                    {
                        "reply": "conditional",
                        "nurse_id": w.get("nurse_id"),
                        "message_id": message_id,
                        "condition": condition,
                    },
                )
            )
    return out
