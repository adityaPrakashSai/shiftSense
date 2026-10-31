"""Roster agent and nurse inbox (design doc 7.10, 6.5, 10.1, 10.3): the Agent tab's task traces,
the replacement inbox, and the nurse's Accept / Decline that resumes the waiting task."""

import sqlite3
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter

from app.api import jobs
from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.schemas import (
    AgentTaskOut,
    ApprovalAsk,
    ConditionCheckBody,
    ConditionCheckOut,
    ConditionIn,
    NurseMessageOut,
    ProposalRespondBody,
    RespondBody,
)
from app.api.ws import hub
from app.config import get_settings
from app.domain.models import RequestKind, Ward
from app.intake import templates as tpl
from app.intake.dates import fmt_day, ward_now
from app.intake.pipeline import build_context, build_request, clarification, parse
from app.intake.semantic import check_edit
from app.llm.gateway import get_gateway
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


def _waiting_reply_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    t = repo.get_agent_task(conn, task_id)
    if t is None:
        raise ApiError(404, "TASK_NOT_FOUND", f"No agent task {task_id}.")
    if t["status"] != "waiting_reply":
        raise ApiError(
            409, "NOT_WAITING_REPLY", f"Task {task_id} is {t['status']}, not waiting on a reply."
        )
    return t


@router.post("/agent/tasks/{task_id}/skip", response_model=AgentTaskOut)
async def skip_task(conn: Conn, task_id: str) -> AgentTaskOut:
    """Treat the task's open ask as timed out now; the cascade moves to the next nurse."""
    ward = require_ward(conn)
    t = _waiting_reply_task(conn, task_id)
    hit = jobs.expire_open_ask(conn, t)
    if hit is None:
        raise ApiError(409, "ALREADY_ANSWERED", f"Task {task_id}'s ask is already answered.")
    m, ans = hit
    jobs.live_log(conn, ward.id).emit("agent.steered", "user:charge", None, None,
                                     task_id=task_id, action="skip", message_id=m["id"],
                                     nurse=m["nurse_id"])  # fmt: skip
    jobs.agent_answer(ward.id, [(task_id, ans)])
    await hub.broadcast(ward.id, {"type": "message_for_nurse", "nurse_id": m["nurse_id"]})
    out = repo.get_agent_task(conn, task_id)
    assert out is not None
    return task_out(out)


@router.post("/agent/tasks/{task_id}/cancel", response_model=AgentTaskOut)
async def cancel_task(conn: Conn, task_id: str) -> AgentTaskOut:
    """End the task without publishing: withdraw its open asks, mark it cancelled."""
    from app.agent import loop

    ward = require_ward(conn)
    t = _waiting_reply_task(conn, task_id)
    log = jobs.live_log(conn, ward.id)
    log.emit("agent.steered", "user:charge", None, None, task_id=task_id, action="cancel")
    for m in loop.withdraw_open_asks(conn, ward.id, log, "user:charge", task_id=task_id):
        await hub.broadcast(ward.id, {"type": "message_for_nurse", "nurse_id": m["nurse_id"]})
    st = t["state"]
    st.pop("waiting", None)
    st["trace"].append({"n": len(st["trace"]) + 1, "ts": repo.now_iso_ms(), "kind": "done",
                        "mode": t["mode"], "brief": "cancelled"})  # fmt: skip
    t.update(status="done", waiting_on=None, pending_call=None, outcome="cancelled")
    repo.save_agent_task(conn, t)
    log.emit("agent.done", "user:charge", None, None, about_run=st.get("last_run"),
             task_id=task_id, workflow=t["workflow"], outcome="cancelled", mode=t["mode"],
             counters=st.get("counters", {}))  # fmt: skip
    await hub.broadcast(ward.id, {"type": "agent_updated", "task_id": task_id})
    return task_out(t)


@router.post("/agent/tasks/{task_id}/reject", response_model=AgentTaskOut)
async def reject_approval(conn: Conn, task_id: str) -> AgentTaskOut:
    """Turn down the run the task proposed: a conditional deal reopens the nurse's ask
    as a plain follow-up, a plain accept ends the task. Publishes nothing."""
    ward = require_ward(conn)
    t = repo.get_agent_task(conn, task_id)
    if t is None:
        raise ApiError(404, "TASK_NOT_FOUND", f"No agent task {task_id}.")
    w = t["state"].get("waiting") or {}
    if t["status"] != "waiting_approval" or w.get("approval_kind") != "run" or w.get("cascade"):
        raise ApiError(409, "NO_RUN_APPROVAL", f"Task {task_id} is not waiting on a run approval.")
    jobs.live_log(conn, ward.id).emit(
        "agent.steered",
        "user:charge",
        None,
        None,
        task_id=task_id,
        action="reject",
        run_id=w.get("ref"),
    )
    jobs.agent_answer(ward.id, [(task_id, {"decision": "rejected", "run_id": w.get("ref")})])
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
    return _enriched(conn, rows)


def _swap_open(conn: sqlite3.Connection, t: dict[str, Any] | None) -> bool:
    """Whether the task negotiates an open swap (named swaps do not take conditions)."""
    rid = (t or {}).get("state", {}).get("request_id") if t else None
    r = repo.get_request(conn, str(rid)) if rid else None
    return r is not None and r.kind is RequestKind.SWAP and r.counterpart_id is None


def _swap_requester_name(
    conn: sqlite3.Connection, ward: Ward, t: dict[str, Any] | None
) -> str | None:
    """The first name of the nurse whose swap is being negotiated, if any."""
    rid = (t or {}).get("state", {}).get("request_id") if t else None
    r = repo.get_request(conn, str(rid)) if rid else None
    if r is None:
        return None
    return ward.nurse(r.nurse_id).name.split()[0]


def _enriched(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[NurseMessageOut]:
    """Inbox rows with their counter-offer state: the condition, if any, and whether a fresh
    ask still takes "yes, if..." (a follow-up does not: one condition round per nurse)."""
    tasks: dict[str, Any] = {}
    out = []
    for r in rows:
        t = None
        if r["task_id"] and r["task_id"] not in tasks:
            tasks[r["task_id"]] = repo.get_agent_task(conn, r["task_id"])
        t = tasks.get(r["task_id"]) if r["task_id"] else None
        st = {} if t is None else t["state"]
        rounds = (st.get("condition_rounds") or {}).get(r["nurse_id"], 0)
        conditionable = r["kind"] == "replacement" or (r["kind"] == "swap" and _swap_open(conn, t))
        out.append(
            NurseMessageOut.model_validate(
                {
                    **r,
                    "condition": (st.get("conditions") or {}).get(r["id"]),
                    "can_condition": conditionable and r["status"] == "asked" and rounds < 1,
                }
            )
        )
    return out


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
    return _enriched(conn, [out])[0]


def _condition_rounds(conn: sqlite3.Connection, m: dict[str, Any]) -> int:
    if not m["task_id"]:
        return 0
    t = repo.get_agent_task(conn, m["task_id"])
    if t is None:
        return 0
    return int(t["state"].get("condition_rounds", {}).get(m["nurse_id"], 0))


def _condition_phrase(lang: str, kind: str, day: str, shift: str | None) -> str:
    if kind == "day_off":
        return tpl.text(lang, "message", "condition_day_off", date=day)
    return tpl.text(lang, "message", "condition_avoid", date=day,
                    shift=tpl.text(lang, "shift", shift or "M"))  # fmt: skip


def _checked_condition(ward: Ward, lang: str, kind: str, day: Any, shift: Any) -> dict[str, Any]:
    """One condition, validated: a day off or one shift to avoid, inside the horizon."""
    if kind not in ("day_off", "avoid_shift"):
        raise ApiError(422, "CONDITION_UNCLEAR", tpl.text(lang, "clarify", "condition_scope"))
    if kind == "avoid_shift" and shift not in ("M", "E", "N"):
        raise ApiError(422, "CONDITION_UNCLEAR", tpl.text(lang, "clarify", "missing_shifts"))
    days = [(ward.horizon_start + timedelta(days=i)).isoformat()
            for i in range(ward.horizon_days)]  # fmt: skip
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    if iso not in days:
        raise ApiError(
            422,
            "CONDITION_UNCLEAR",
            tpl.text(lang, "clarify", "outside_period", day=iso, first=days[0], last=days[-1]),
        )
    return {"kind": kind, "day": iso, "shift": shift if kind == "avoid_shift" else None}


@router.post("/inbox/{message_id}/condition/check", response_model=ConditionCheckOut)
async def check_condition(
    conn: Conn, message_id: str, body: ConditionCheckBody
) -> ConditionCheckOut:
    """Parse a "yes, if..." text through intake, restricted to one day off or one
    shift to avoid for this nurse; answers the confirm chip, or a clarification."""
    ward = require_ward(conn)
    m = repo.get_nurse_message(conn, message_id)
    if m is None:
        raise ApiError(404, "MESSAGE_NOT_FOUND", f"No message {message_id}.")
    if m["status"] == "expired":
        raise ApiError(409, "ASK_EXPIRED", f"Message {m['id']} expired without an answer.")
    if m["status"] != "asked":
        raise ApiError(409, "ALREADY_ANSWERED", f"Message {m['id']} is {m['status']}.")
    lang = ward.nurse(m["nurse_id"]).language
    t = repo.get_agent_task(conn, m["task_id"]) if m["task_id"] else None
    conditionable = m["kind"] == "replacement" or (m["kind"] == "swap" and _swap_open(conn, t))
    if not conditionable or not m["task_id"] or _condition_rounds(conn, m) >= 1:
        raise ApiError(422, "CONDITION_UNCLEAR", tpl.text(lang, "clarify", "condition_scope"))
    now = ward_now(ward.tz, get_settings().demo_now)
    out = await parse(get_gateway(), build_request(build_context(conn, ward, m["nurse_id"], now),
                                                   body.text))  # fmt: skip
    if out.edit is None or out.edit.intent != "edit" or len(out.edit.edits) != 1:
        raise ApiError(422, "CONDITION_UNCLEAR", tpl.text(lang, "clarify", "rephrase"))
    resolved, probs = check_edit(conn, ward, out.edit.edits[0], sender_id=m["nurse_id"],
                                 message=body.text, now=now, lang=lang)  # fmt: skip
    if probs or resolved is None:
        raise ApiError(422, "CONDITION_UNCLEAR", clarification(lang, out.edit, probs))
    key = resolved.key()
    if key["op"] not in ("request_day_off", "avoid_shift") or key["nurse"] != m["nurse_id"]:
        if key["op"] == "propose_swap" and m["kind"] == "swap":
            name = _swap_requester_name(conn, ward, t)
            if name is not None:
                raise ApiError(422, "CONDITION_UNCLEAR",
                               tpl.text(lang, "clarify", "bigger_swap", name=name))  # fmt: skip
        raise ApiError(422, "CONDITION_UNCLEAR", tpl.text(lang, "clarify", "condition_scope"))
    if len(key["dates"]) != 1:
        raise ApiError(422, "CONDITION_UNCLEAR", tpl.text(lang, "clarify", "condition_scope"))
    kind = "day_off" if key["op"] == "request_day_off" else "avoid_shift"
    shifts = key.get("shifts") or []
    if kind == "avoid_shift" and len(shifts) != 1:
        raise ApiError(422, "CONDITION_UNCLEAR", tpl.text(lang, "clarify", "missing_shifts"))
    cond = _checked_condition(ward, lang, kind, key["dates"][0],
                              shifts[0] if shifts else None)  # fmt: skip
    chip = tpl.text(lang, "message", f"chip_{kind}",
                    date=fmt_day(date.fromisoformat(cond["day"]), lang),
                    shift=tpl.text(lang, "shift", cond["shift"] or "M"))  # fmt: skip
    return ConditionCheckOut(condition=ConditionIn.model_validate(cond), chip=chip)


async def answer_conditional(
    conn: sqlite3.Connection, ward: Ward, m: dict[str, Any], cond: ConditionIn
) -> NurseMessageOut:
    """Yes, if...: the ask becomes conditional and the task evaluates the condition."""
    if m["status"] == "expired":
        raise ApiError(409, "ASK_EXPIRED", f"Message {m['id']} expired without an answer.")
    if m["status"] != "asked":
        raise ApiError(409, "ALREADY_ANSWERED", f"Message {m['id']} is {m['status']}.")
    t = repo.get_agent_task(conn, m["task_id"]) if m["task_id"] else None
    conditionable = m["kind"] == "replacement" or (m["kind"] == "swap" and _swap_open(conn, t))
    if not conditionable or not m["task_id"]:
        raise ApiError(422, "CONDITION_UNCLEAR", tpl.text(
            ward.nurse(m["nurse_id"]).language, "clarify", "condition_scope"))  # fmt: skip
    if _condition_rounds(conn, m) >= 1:
        return await answer(conn, ward.id, m, False)  # a second condition is a decline
    condition = _checked_condition(ward, ward.nurse(m["nurse_id"]).language, cond.kind, cond.day,
                                   cond.shift)  # fmt: skip
    repo.set_nurse_message_status(conn, m["id"], "conditional")
    actor = f"user:{m['nurse_id']}"
    if m["run_id"]:
        prop = repo.proposal_for_run(conn, m["run_id"])
        if prop is not None:
            repo.set_replacement_status(conn, prop["run_id"], "conditional")
    jobs.live_log(conn, ward.id).emit("replacement.conditional", actor, m["run_id"], None,
                                      message_id=m["id"], nurse=m["nurse_id"],
                                      condition=condition)  # fmt: skip
    from app.agent import runtime

    jobs.agent_answer(ward.id, runtime.answers_for_conditional(conn, ward.id, m["id"], condition))
    await hub.broadcast(ward.id, {"type": "message_for_nurse", "nurse_id": m["nurse_id"]})
    out = repo.get_nurse_message(conn, m["id"])
    assert out is not None
    return _enriched(conn, [out])[0]


@router.post("/inbox/{message_id}/respond", response_model=NurseMessageOut)
async def respond(conn: Conn, message_id: str, body: RespondBody) -> NurseMessageOut:
    """The nurse taps Accept or Decline (or "yes, if..."); the waiting agent task continues."""
    ward = require_ward(conn)
    m = repo.get_nurse_message(conn, message_id)
    if m is None:
        raise ApiError(404, "MESSAGE_NOT_FOUND", f"No message {message_id}.")
    if body.condition is not None:
        return await answer_conditional(conn, ward, m, body.condition)
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
