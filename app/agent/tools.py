"""Roster agent tools (design doc 7.10): the planner, evidence and messaging functions the agent
may call, in three classes.

- read: no side effects (the explainer tools of 7.4, `get_open_cells`, `get_nurse`,
  `get_pending_requests`);
- compute: create runs that are never published by themselves (`rank_replacements`,
  `run_resolve`, `run_full`, `what_if`, `rule_check_swap`, `explain_denied_requests`, `diagnose`,
  `list_conflict_options`, `apply_option`) or compute a read-only what-if (`staffing_check`);
- action: side effects, all logged (`send_message`, `wait_for_reply`, `request_approval`,
  `escalate`). `wait_for_reply` and `request_approval` pause the task until a person answers.

There is no publish tool: publishing happens only through the charge nurse's Approve button
(`POST /api/runs/{id}/publish` or `/api/proposals/{id}/choose`). A test checks the registry.

Every tool is `async (ctx, **args) -> dict`; bad arguments raise ToolError, which the loop returns
to the model as {"error": ...}. A run started elsewhere for the same trigger (the API starts the
re-solve the moment a request is confirmed, so the ranking runs while the model thinks) is handed
to the task as `state["prefetch"]` and used by the first compute tool that would start one.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from app.agent import fallback, timeouts
from app.agent.env import Env
from app.agent.writer import short_date, write
from app.config import get_settings
from app.domain.models import RequestKind, RequestStatus, RunStatus, Ward
from app.evidence.counterfactual import Question, explain_denied, what_if
from app.explain import tools as ex
from app.explain.claims import check
from app.intake.dates import ward_now
from app.llm.gateway import LLMGateway
from app.log.decision_log import DecisionLog
from app.planner.build_input import day_index, horizon_dates
from app.planner.staffing import run_check
from app.solver.input import SolveInput
from app.solver.rules import rule_check
from app.store import repo
from app.store.db import connect


class ToolClass(StrEnum):
    READ = "read"
    COMPUTE = "compute"
    ACTION = "action"


class ToolError(Exception):
    """A bad argument or a refused action; returned to the model as {"error": ...}."""


class Forbidden(ToolError):
    """A call that would break a guardrail (approval first); counted and logged."""


@dataclass
class AgentCtx:
    conn: sqlite3.Connection
    ward: Ward
    task: dict[str, Any]
    env: Env
    gateway: LLMGateway
    log: DecisionLog

    @property
    def state(self) -> dict[str, Any]:
        state: dict[str, Any] = self.task["state"]
        return state

    def published_id(self) -> str | None:
        pub = repo.get_published_run(self.conn, self.ward.id)
        return None if pub is None else str(pub["run_id"])

    def explain_ctx(self, run_id: str | None = None) -> ex.ToolContext:
        rid = run_id or self.published_id() or ""
        return ex.ToolContext(self.conn, self.ward, rid, "charge")

    def name(self, nurse_id: str | None) -> str | None:
        return next((n.name for n in self.ward.nurses if n.id == nurse_id), nurse_id)


Fn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    cls: ToolClass
    description: str
    params: dict[str, Any]
    required: tuple[str, ...]
    fn: Fn
    waits: bool = False  # pauses the task until a person answers

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.params,
                    "required": list(self.required),
                },
            },
        }


# ---------------------------------------------------------------- helpers


def _request(ctx: AgentCtx, request_id: Any = None) -> Any:
    rid = str(request_id or ctx.state.get("request_id") or "")
    r = repo.get_request(ctx.conn, rid) if rid else None
    if r is None:
        raise ToolError(f"no request {rid!r}")
    return r


def _take_prefetch(ctx: AgentCtx) -> str | None:
    v = ctx.state.pop("prefetch", None)
    return None if v is None else str(v)


async def _finished_run(ctx: AgentCtx, run_id: str) -> dict[str, Any]:
    await ctx.env.await_run(run_id)
    run = repo.get_run(ctx.conn, run_id)
    if run is None:
        raise ToolError(f"run {run_id} disappeared")
    return run


def _run_brief(ctx: AgentCtx, run: dict[str, Any]) -> dict[str, Any]:
    """What every solve tool returns: status, run id, and the summary or the conflict."""
    rid = str(run["run_id"])
    if run["status"] == RunStatus.INFEASIBLE.value:
        c = repo.conflict_for_run(ctx.conn, rid)
        return {"status": "infeasible", "run_id": rid,
                "conflict_id": None if c is None else c["conflict_id"],
                "note": "no legal roster: a conflict task takes over"}  # fmt: skip
    if run["status"] != RunStatus.PROPOSED.value and not run["published_at"]:
        return {"status": str(run["status"]), "run_id": rid}
    out: dict[str, Any] = ex.get_run_summary(ctx.explain_ctx(rid), rid)
    out["status"] = "proposed"  # a proposal the charge nurse may already have published
    out["already_published"] = bool(run["published_at"])
    return out


# ---------------------------------------------------------------- read tools


def _explainer(name: str) -> Fn:
    async def fn(ctx: AgentCtx, **args: Any) -> dict[str, Any]:
        res = ex.call(ctx.explain_ctx(args.get("run_id")), name, args)
        if "error" in res:
            raise ToolError(str(res["error"]))
        return res

    fn.__name__ = name
    return fn


async def get_open_cells(ctx: AgentCtx) -> dict[str, Any]:
    """The published cells the trigger request removes (sick call), or the slots a demand change
    adds."""
    r = _request(ctx)
    pub = ctx.published_id()
    grid = repo.get_roster(ctx.conn, pub) if pub else {}
    days = horizon_dates(ctx.ward)
    today = ward_now(ctx.ward.tz, get_settings().demo_now).date()
    cells = []
    if r.kind is RequestKind.DEMAND:
        for d in r.dates:
            for s in r.shifts or []:
                i = day_index(ctx.ward, d)
                on = sum(1 for row in grid.values() if 0 <= i < len(row) and row[i] == s.value)
                cells.append({"nurse": None, "day": d.isoformat(), "shift": s.value,
                              "target_total": r.target_total, "assigned": on})  # fmt: skip
    else:
        row = grid.get(r.nurse_id) or []
        wanted = None if r.shifts is None else {s.value for s in r.shifts}
        for d in r.dates:
            i = day_index(ctx.ward, d)
            if d < today or not 0 <= i < len(row) or row[i] is None:
                continue
            if wanted is None or row[i] in wanted:
                cells.append({"nurse": r.nurse_id, "day": days[i].isoformat(), "shift": row[i]})
    return {"request_id": r.id, "kind": r.kind.value, "nurse": r.nurse_id,
            "name": ctx.name(r.nurse_id), "open_cells": cells}  # fmt: skip


async def get_nurse(ctx: AgentCtx, nurse: Any) -> dict[str, Any]:
    try:
        nid = ex._nurse(ctx.explain_ctx(), nurse)
    except ex.ToolError as exc:
        raise ToolError(str(exc)) from exc
    n = ctx.ward.nurse(nid)
    pub = ctx.published_id()
    row = (repo.get_roster(ctx.conn, pub) if pub else {}).get(nid) or []
    return {"nurse": nid, "name": n.name, "grade": n.grade.value, "standby": n.standby,
            "can_night": n.can_night, "language": n.language,
            "shifts_this_period": sum(1 for x in row if x)}  # fmt: skip


async def get_pending_requests(ctx: AgentCtx) -> dict[str, Any]:
    waiting = (RequestStatus.PENDING_APPROVAL, RequestStatus.PENDING_CONFIRM)
    rows = [r for r in repo.list_requests(ctx.conn, ctx.ward.id) if r.status in waiting]
    return {"requests": [{"request_id": r.id, "nurse": r.nurse_id, "kind": r.kind.value,
                          "status": r.status.value,
                          "dates": [d.isoformat() for d in r.dates]} for r in rows]}  # fmt: skip


# ---------------------------------------------------------------- compute tools


async def rank_replacements(ctx: AgentCtx, exclude: Any = None) -> dict[str, Any]:
    """6.4: re-solve with stability and rank up to 3 replacements. `exclude` = nurses who declined
    this cell; they cannot take it in this re-solve (6.5)."""
    excl = [str(x) for x in (exclude or [])]
    run_id = None if excl else _take_prefetch(ctx)
    if run_id is None:
        r = _request(ctx)
        cell = ctx.state.get("open_cell")
        if excl and not cell:
            raise ToolError("nothing to exclude from: no open cell was ranked yet")
        blocked = []
        affected = list(r.dates)
        if cell:
            d = date.fromisoformat(cell["day"])
            i = day_index(ctx.ward, d)
            blocked = [(n, i, cell["shift"]) for n in excl]
            affected = [d]
        n = 1 + sum(1 for c in ctx.state.get("calls", []) if c["tool"] == "rank_replacements")
        run_id = await ctx.env.start_resolve(
            ctx.conn, ctx.ward.id, trigger=f"agent:{ctx.task['id']}:rank{n}", affected=affected,
            request_id=r.id, extra_unavailable=blocked,
        )  # fmt: skip
    run = await _finished_run(ctx, run_id)
    brief = _run_brief(ctx, run)
    if brief["status"] != "proposed":
        return brief
    try:
        prop = ex.get_proposal(ctx.explain_ctx(run_id), run_id)
    except ex.ToolError as exc:
        raise ToolError(f"run {run_id} has no proposal: {exc}") from exc
    if prop.get("open_cell") and not ctx.state.get("open_cell"):
        ctx.state["open_cell"] = prop["open_cell"]
    raw = repo.proposal_for_run(ctx.conn, run_id)
    runs_by_nurse = (
        {}
        if raw is None
        else {str(c.get("nurse")): c.get("run_id") for c in raw["ranking"].get("candidates", [])}
    )  # the explainer hides run ids; the cascade needs each candidate's run
    cands = [{**c, "run_id": runs_by_nurse.get(str(c.get("nurse")))}
             for c in prop.get("candidates", [])]  # fmt: skip
    return {
        "status": "proposed",
        "run_id": run_id,
        "changed_cells": brief.get("changed_cells"),
        "excluded_by_decline": excl,
        "open_cell": prop.get("open_cell"),
        "candidates": cands,
        "excluded": prop.get("excluded"),
        "coverage_worse": prop.get("coverage_worse"),
    }


async def run_resolve(ctx: AgentCtx) -> dict[str, Any]:
    """A minimal-change re-solve from the published roster (the one already started for this
    step when there is one)."""
    run_id = _take_prefetch(ctx)
    if run_id is None:
        r = repo.get_request(ctx.conn, str(ctx.state.get("request_id") or ""))
        run_id = await ctx.env.start_resolve(
            ctx.conn, ctx.ward.id, trigger=f"agent:{ctx.task['id']}:resolve",
            affected=[] if r is None else list(r.dates), request_id=None if r is None else r.id,
        )  # fmt: skip
    return _run_brief(ctx, await _finished_run(ctx, run_id))


async def run_full(ctx: AgentCtx) -> dict[str, Any]:
    run_id = _take_prefetch(ctx)
    if run_id is None:
        run_id = await ctx.env.start_full(ctx.conn, ctx.ward.id, trigger=f"agent:{ctx.task['id']}")
    return _run_brief(ctx, await _finished_run(ctx, run_id))


def _swap_forcings(ctx: AgentCtx, r: Any) -> tuple[tuple[str, int, str | None], ...]:
    if not r.counterpart_id or not r.dates or not r.counterpart_dates:
        raise ToolError(f"request {r.id} is not a complete swap")
    pub = ctx.published_id()
    grid = repo.get_roster(ctx.conn, pub) if pub else {}
    d1, d2 = day_index(ctx.ward, r.dates[0]), day_index(ctx.ward, r.counterpart_dates[0])
    s1 = (grid.get(r.nurse_id) or [None] * (d1 + 1))[d1]
    s2 = (grid.get(r.counterpart_id) or [None] * (d2 + 1))[d2]
    out = [(r.counterpart_id, d1, s1), (r.nurse_id, d2, s2)]
    if d1 != d2:
        out += [(r.nurse_id, d1, None), (r.counterpart_id, d2, None)]
    return tuple(out)


async def what_if_tool(ctx: AgentCtx, request_id: Any = None) -> dict[str, Any]:
    """Impact of granting a pending leave or swap on the published roster (full scope, 6.6)."""
    r = _request(ctx, request_id)
    pub = ctx.published_id()
    if pub is None:
        raise ToolError("nothing is published yet")
    if r.kind is RequestKind.LEAVE:
        idx = [day_index(ctx.ward, d) for d in r.dates]
        q = Question(blocked=tuple((r.nurse_id, i, None) for i in idx if 0 <= i < 400))
    elif r.kind is RequestKind.SWAP:
        q = Question(forcings=_swap_forcings(ctx, r))
    else:
        raise ToolError(f"what_if takes a leave or swap request, not {r.kind.value}")
    v = await asyncio.to_thread(_what_if_in_thread, pub, q)
    delta = {} if v.alt is None else {
        t: v.alt.get(t, 0) - v.base.get(t, 0) for t in v.base if v.alt.get(t, 0) != v.base.get(t, 0)
    }  # fmt: skip
    return {"request_id": r.id, "kind": r.kind.value, "nurse": r.nurse_id,
            "name": ctx.name(r.nurse_id), "run_id": pub, "verdict": v.verdict,
            "reason_code": v.reason_code, "base": v.base, "alt": v.alt, "delta": delta,
            "core": v.core, "changed_cells": v.changed_cells,
            "proven": v.base_proven and bool(v.alt_proven)}  # fmt: skip


def _what_if_in_thread(run_id: str, q: Question) -> Any:
    conn = connect(get_settings().db_path)
    try:
        return what_if(conn, run_id, q)
    finally:
        conn.close()


async def staffing_check(
    ctx: AgentCtx, hires: Any = 0, removed: Any = None, coverage: Any = "target",
    standby: Any = True,
) -> dict[str, Any]:  # fmt: skip
    """6.7: minimum headcount, shortfall and standby shifts for the period under a what-if.
    Read-only: nothing is stored and the published roster is untouched."""
    gone = [str(n) for n in (removed or [])]
    if coverage not in ("target", "floor"):
        raise ToolError("coverage is 'target' or 'floor'")
    try:
        out = await asyncio.to_thread(_staffing_in_thread, ctx.ward.id, int(hires), gone,
                                      coverage, bool(standby))  # fmt: skip
    except (ValueError, LookupError) as exc:
        raise ToolError(str(exc)) from exc
    return {k: out[k] for k in ("status", "inputs", "shortfall", "headcount", "hires_needed",
                                "standby_shifts", "short_cells", "used", "caveat")}  # fmt: skip


def _staffing_in_thread(
    ward_id: str, hires: int, removed: list[str], coverage: Any, standby: bool
) -> dict[str, Any]:
    conn = connect(get_settings().db_path)
    try:
        return run_check(conn, ward_id, hires=hires, removed=removed, coverage=coverage,
                         standby=standby)  # fmt: skip
    finally:
        conn.close()


async def rule_check_swap(ctx: AgentCtx, request_id: Any = None) -> dict[str, Any]:
    """Hard rules each nurse would break by taking the other's cell, everyone else fixed."""
    r = _request(ctx, request_id)
    pub = ctx.published_id()
    run = None if pub is None else repo.get_run(ctx.conn, pub)
    if run is None:
        raise ToolError("nothing is published yet")
    inp = SolveInput.from_json(run["input_json"])
    grid = repo.get_roster(ctx.conn, pub or "")
    roster = {(n, d): s for n, row in grid.items() for d, s in enumerate(row)}
    forced = _swap_forcings(ctx, r)
    for n, d, s in forced:  # apply the whole swap, then check each moved cell
        roster[n, d] = s
    broken = {
        f"{n}:d{d}": rule_check(roster, inp, n, d, s, nurse_only=True)
        for n, d, s in forced
        if s is not None
    }
    return {"request_id": r.id, "nurse": r.nurse_id, "counterpart": r.counterpart_id,
            "legal": not any(broken.values()), "broken": broken}  # fmt: skip


async def explain_denied_requests(ctx: AgentCtx, run_id: Any = None) -> dict[str, Any]:
    rid = str(run_id or ctx.state.get("last_run") or "")
    if not rid or repo.get_run(ctx.conn, rid) is None:
        raise ToolError(f"no run {rid!r}")
    await asyncio.to_thread(_denied_in_thread, rid)
    outs = [o for o in repo.get_request_outcomes(ctx.conn, rid) if o["status"] == "DENIED"]
    return {"run_id": rid, "denied": [
        {"request_id": o["request_id"], "reason_code": o["reason_code"]} for o in outs
    ]}  # fmt: skip


def _denied_in_thread(run_id: str) -> None:
    conn = connect(get_settings().db_path)
    try:
        explain_denied(conn, run_id, budget_s=8.0)
    finally:
        conn.close()


def _conflict(ctx: AgentCtx, conflict_id: Any) -> dict[str, Any]:
    cid = str(conflict_id or ctx.state.get("conflict_id") or "")
    c = repo.get_conflict(ctx.conn, cid) if cid else None
    if c is None:
        raise ToolError(f"no conflict {cid!r}")
    return c


async def diagnose(ctx: AgentCtx, conflict_id: Any = None) -> dict[str, Any]:
    """The unsat core (5.5) the planner found for an infeasible run, and why others cannot help."""
    c = _conflict(ctx, conflict_id)
    return {"conflict_id": c["conflict_id"], "run_id": c["run_id"], "core": c["core"]["names"],
            "context": c["core"].get("context", [])}  # fmt: skip


async def list_conflict_options(ctx: AgentCtx, conflict_id: Any = None) -> dict[str, Any]:
    c = _conflict(ctx, conflict_id)
    narr = c.get("narrative") or {}
    labels = {o.get("option_id"): o.get("label") for o in narr.get("options", [])}
    return {"conflict_id": c["conflict_id"], "summary": narr.get("summary"), "options": [
        {"option_id": o["option_id"], "kind": o["kind"],
         "label": labels.get(o["option_id"]) or o["label"]} for o in c["options"]
    ]}  # fmt: skip


async def apply_option(ctx: AgentCtx, option_id: Any, conflict_id: Any = None) -> dict[str, Any]:
    """Confirms the option the charge nurse chose (the choice itself is only theirs)."""
    c = _conflict(ctx, conflict_id)
    if c["chosen_option"] is None:
        raise Forbidden("the charge nurse has not chosen an option yet: call request_approval")
    if c["chosen_option"] != str(option_id):
        raise Forbidden(f"the charge nurse chose {c['chosen_option']}, not {option_id}")
    return {"conflict_id": c["conflict_id"], "option_id": c["chosen_option"], "applied": True,
            "run_id": c["resolved_run"]}  # fmt: skip


# ---------------------------------------------------------------- action tools


def _open_replacement_ask(ctx: AgentCtx, day: str, shift: str) -> dict[str, Any] | None:
    """An ask for this cell still awaiting an answer, if any (at most one may be open)."""
    for m in repo.list_nurse_messages(ctx.conn, ctx.ward.id):
        if (m["kind"] == "replacement" and m["status"] == "asked"
                and m["day"] == day and m["shift"] == shift):  # fmt: skip
            return m
    return None


def _cascade_send(ctx: AgentCtx, nid: str, rid: str) -> None:
    """The cascade rules, enforced in code: the approved plan's run, a fresh ranking, the
    next uncontacted nurse in the order, and no other open ask for the cell."""
    ac = ctx.state.get("approved_cascade") or {}
    if not get_settings().agent_cascade or not ac or rid != ac.get("run_id"):
        raise Forbidden(f"run {rid or '?'} is not approved by the charge nurse yet")
    if ctx.published_id() != ac.get("parent_run"):
        asked = fallback.contacted(ctx.state.get("calls", []))
        raise ToolError(
            "the published roster changed since this ranking: call "
            f"rank_replacements(exclude={asked}) and request a new cascade approval"
        )
    asked = fallback.contacted(ctx.state.get("calls", []))
    nxt = next((n for n in ac.get("order", []) if n not in asked), None)
    if nid != nxt:
        raise Forbidden(f"{nid} is not next in the approved cascade (next: {nxt})")
    cell = ac.get("open_cell") or {}
    if _open_replacement_ask(ctx, str(cell.get("day")), str(cell.get("shift"))) is not None:
        raise Forbidden("another ask for this cell is still open: wait for its reply first")


async def send_message(
    ctx: AgentCtx, nurse_id: Any, kind: Any = "replacement", run_id: Any = None,
    request_id: Any = None,
) -> dict[str, Any]:  # fmt: skip
    """Muse writes the ask (7.7, post-checked); it lands in the nurse's inbox."""
    nid = str(nurse_id or "")
    if not any(n.id == nid for n in ctx.ward.nurses):
        raise ToolError(f"no nurse {nid!r}")
    kind = str(kind or "replacement")
    facts: dict[str, Any] = {}
    if kind == "replacement":
        rid = str(run_id or "")
        if rid not in ctx.state.get("approved_runs", []):
            _cascade_send(ctx, nid, rid)
        prop = repo.proposal_for_run(ctx.conn, rid)
        if prop is None:
            raise ToolError(f"run {rid} is not a proposal")
        cell = prop["ranking"].get("open_cell")
        if cell is None:
            raise ToolError(f"run {rid} has no open cell to ask about")
        cand = next((c for c in prop["ranking"].get("candidates", []) if c["nurse"] == nid), None)
        if cand is None:
            raise ToolError(f"{nid} is not a candidate of run {rid}")
        facts = {k: cand.get(k) for k in ("standby", "changed_cells", "tie", "tie_break", "rank")}
        day, shift, req_id = date.fromisoformat(cell["day"]), str(cell["shift"]), None
    elif kind == "swap":
        r = _request(ctx, request_id)
        if r.kind is not RequestKind.SWAP or r.counterpart_id != nid:
            raise ToolError(f"{nid} is not the counterpart of swap {r.id}")
        rid = ""
        forced = _swap_forcings(ctx, r)
        day, shift, req_id = r.dates[0], str(forced[0][2] or "M"), r.id
        days = horizon_dates(ctx.ward)
        facts = {"requester": ctx.name(r.nurse_id),
                 "theirs": f"{short_date(days[forced[0][1]])} {forced[0][2] or 'off'}",
                 "mine": f"{short_date(days[forced[1][1]])} {forced[1][2] or 'off'}"}  # fmt: skip
    else:
        raise ToolError("kind is replacement or swap")
    w = await write(ctx.ward, nurse_id=nid, day=day, shift=shift, kind=kind, facts=facts,
                    gateway=ctx.gateway, run_id=rid or None)  # fmt: skip
    mid = repo.next_nurse_message_id(ctx.conn)
    repo.insert_nurse_message(ctx.conn, {
        "id": mid, "ward_id": ctx.ward.id, "nurse_id": nid, "kind": kind, "run_id": rid or None,
        "request_id": req_id, "task_id": ctx.task["id"], "day": day.isoformat(), "shift": shift,
        "text": w.text, "language": w.language, "source": w.source, "status": "asked",
    })  # fmt: skip
    if kind == "replacement":
        prop = repo.proposal_for_run(ctx.conn, rid)
        if prop is not None:
            repo.set_proposal_message(ctx.conn, prop["run_id"], w.text, "asked")
    ctx.log.emit("message.drafted", "llm:message", rid or None, None, nurse=nid, message=w.text,
                 message_id=mid, source=w.source, problems=w.problems)  # fmt: skip
    await ctx.env.broadcast(ctx.ward.id, {"type": "message_for_nurse", "nurse_id": nid})
    return {"message_id": mid, "nurse_id": nid, "text": w.text, "language": w.language,
            "source": w.source}  # fmt: skip


async def wait_for_reply(
    ctx: AgentCtx, nurse_id: Any, message_id: Any = None, timeout_minutes: Any = 30
) -> dict[str, Any]:
    """Pause for the nurse's Accept / Decline; a nurse who already answered (people can be quicker
    than the agent) is answered at once."""
    mid = str(message_id or "")
    if not mid:
        mine = [m for m in repo.list_nurse_messages(ctx.conn, ctx.ward.id, nurse_id=str(nurse_id))
                if m["task_id"] == ctx.task["id"]]  # fmt: skip
        if not mine:
            raise ToolError(f"no message to {nurse_id}: call send_message first")
        mid = mine[0]["id"]
    m = repo.get_nurse_message(ctx.conn, mid)
    if m is None or m["task_id"] != ctx.task["id"]:
        raise ToolError(f"message {mid} is not one this task sent")
    if m["status"] in ("accepted", "declined"):
        return {"answered": {"reply": m["status"], "nurse_id": m["nurse_id"],
                             "message_id": mid}}  # fmt: skip
    if m["status"] != "asked":
        raise ToolError(f"message {mid} is {m['status']}")
    # the model may ask for a shorter wait, never a longer one; the deadline is authoritative
    cap = get_settings().agent_reply_timeout_seconds
    secs = min(max(1, int(timeout_minutes or 30)) * 60, cap)
    return {"wait": "reply", "on": str(m["nurse_id"]), "message_id": mid,
            "timeout_minutes": (secs + 59) // 60,
            "deadline_at": timeouts.deadline_after(secs)}  # fmt: skip


def _checked_cascade(ctx: AgentCtx, rid: str, cascade: Any) -> list[str] | None:
    """The contact plan, validated in code: 1-3 distinct nurses, all ranked for this run."""
    if cascade is None:
        return None
    if not get_settings().agent_cascade:
        raise ToolError("contact plans are disabled (AGENT_CASCADE=0): ask for one run")
    prop = repo.proposal_for_run(ctx.conn, rid)
    if prop is None:
        raise ToolError(f"run {rid} is not a proposal: a cascade needs ranked candidates")
    order = [str(n) for n in cascade]
    cands = {str(c["nurse"]) for c in prop["ranking"].get("candidates", [])}
    if not 1 <= len(order) <= fallback.MAX_CASCADE:
        raise ToolError(f"a cascade names 1 to {fallback.MAX_CASCADE} nurses, not {len(order)}")
    if len(set(order)) != len(order):
        raise ToolError(f"a cascade names each nurse once: {order}")
    if not set(order) <= cands:
        raise ToolError(f"{sorted(set(order) - cands)} are not candidates of run {rid}")
    return order


async def request_approval(
    ctx: AgentCtx, summary: Any = "", claims: Any = None, run_id: Any = None,
    conflict_id: Any = None, request_id: Any = None, cascade: Any = None,
) -> dict[str, Any]:  # fmt: skip
    """Show the charge nurse a summary (claim-checked, 7.4) and wait for their decision."""
    cl = [c for c in (claims or []) if isinstance(c, dict)]
    results = {c["call_id"]: c["result"] for c in ctx.state.get("calls", [])}
    problems = check(cl, results)
    if not cl and results:
        problems.append("the summary cites no tool result")
    if cascade is not None and not run_id:
        raise ToolError("a cascade goes with run_id, not a conflict or request")
    # the charge nurse may decide before the agent asks (Approve is on screen as soon as the
    # proposal is): an existing decision is the answer at once
    if run_id:
        rid = str(run_id)
        run = repo.get_run(ctx.conn, rid)
        if run is None:
            raise ToolError(f"no run {rid}")
        order = _checked_cascade(ctx, rid, cascade)
        prop = repo.proposal_for_run(ctx.conn, rid)
        runs = [rid]
        if prop is not None:
            runs = [prop["run_id"], *(c["run_id"] for c in prop["ranking"].get("candidates", []))]
        status = {x: (repo.get_run(ctx.conn, x) or {}).get("status") for x in runs}
        done = next((x for x, s in status.items() if s == RunStatus.PUBLISHED.value), None)
        if done is None and run["published_at"]:
            done = rid  # published and already replaced by a later roster
        if done is not None:
            nurse = None if prop is None else prop["chosen_nurse"]
            pre: dict[str, Any] = {"decision": "approved", "run_id": done, "nurse": nurse}
            if order is not None:
                pre["cascade"] = order  # decided before the plan was shown: ask the nurse
            return {"answered": pre}
        if run["status"] != RunStatus.PROPOSED.value:
            return {"answered": {"decision": "superseded", "run_id": rid}}
        on = {"kind": "run", "ref": rid, "runs": sorted(set(runs))}
        if order is not None:
            on["cascade"] = order
            on["parent_run"] = run["parent_run"]
    elif conflict_id:
        c = _conflict(ctx, conflict_id)
        if c["chosen_option"] is not None:
            return {"answered": {"decision": "approved", "conflict_id": c["conflict_id"],
                                 "option_id": c["chosen_option"],
                                 "prefetch": c["resolved_run"]}}  # fmt: skip
        on = {"kind": "conflict", "ref": c["conflict_id"]}
    elif request_id:
        r = _request(ctx, request_id)
        if r.status in (RequestStatus.ACTIVE, RequestStatus.REJECTED):
            ok = r.status is RequestStatus.ACTIVE
            started = [x for x in repo.list_runs(ctx.conn, ctx.ward.id)
                       if x["trigger"] == f"request:{r.id}"]  # fmt: skip
            ans: dict[str, Any] = {"decision": "approved" if ok else "rejected",
                                   "request_id": r.id}  # fmt: skip
            if ok and started:
                ans["prefetch"] = started[-1]["run_id"]
            return {"answered": ans}
        if r.status is not RequestStatus.PENDING_APPROVAL:
            raise ToolError(f"request {r.id} is {r.status.value}, not pending_approval")
        on = {"kind": "request", "ref": r.id}
    else:
        raise ToolError("give run_id, conflict_id or request_id")
    return {"wait": "approval", "on": "charge", "approval": on, "summary": str(summary or ""),
            "claims": cl, "problems": problems}  # fmt: skip


async def escalate(ctx: AgentCtx, reason: Any = "") -> dict[str, Any]:
    return {"escalated": True, "reason": str(reason or "")}


# ---------------------------------------------------------------- registry

S = {"type": "string"}
NURSE = {"type": "string", "description": "nurse id"}
RUN = {"type": "string", "description": "run id"}
REQ = {"type": "string", "description": "request id; default: the request that started the task"}
CON = {"type": "string", "description": "conflict id; default: the task's conflict"}
CLAIMS = {"type": "array", "items": {"type": "object", "properties": {
    "text": S, "call_id": S, "path": S, "value": {}}}}  # fmt: skip


def _t(name: str, cls: ToolClass, desc: str, fn: Fn, params: dict[str, Any] | None = None,
       required: tuple[str, ...] = (), waits: bool = False) -> AgentTool:  # fmt: skip
    return AgentTool(name, cls, desc, params or {}, required, fn, waits)


def _explainer_tools() -> list[AgentTool]:
    out = []
    for spec in ex.SPECS:
        f = spec["function"]
        out.append(AgentTool(f["name"], ToolClass.READ, f["description"],
                             f["parameters"]["properties"], tuple(f["parameters"]["required"]),
                             _explainer(f["name"])))  # fmt: skip
    return out


REGISTRY: dict[str, AgentTool] = {
    t.name: t
    for t in [
        *_explainer_tools(),
        _t("get_open_cells", ToolClass.READ, "The published cells the task's request removes "
           "(sick call, leave), or the slots a demand change adds.", get_open_cells),
        _t("get_nurse", ToolClass.READ, "A nurse's grade, standby flag, night qualification, "
           "language and shifts this period.", get_nurse, {"nurse": NURSE}, ("nurse",)),
        _t("get_pending_requests", ToolClass.READ, "Requests waiting for confirmation or the "
           "charge nurse's approval.", get_pending_requests),
        _t("rank_replacements", ToolClass.COMPUTE, "Re-solve from the published roster with "
           "stability and rank up to 3 replacements for the open cell (tier vectors, changed "
           "cells, excluded nurses with rules). `exclude`: nurses who declined this cell.",
           rank_replacements, {"exclude": {"type": "array", "items": NURSE}}),
        _t("run_resolve", ToolClass.COMPUTE, "Minimal-change re-solve from the published roster "
           "(after an approved leave or swap, or a conflict option); returns the proposal "
           "summary or the conflict.", run_resolve),
        _t("run_full", ToolClass.COMPUTE, "Build a fresh roster for the period; returns its "
           "summary.", run_full),
        _t("what_if", ToolClass.COMPUTE, "Impact of granting a pending leave or swap on the "
           "published roster: verdict, reason code, tier deltas, changed cells.", what_if_tool,
           {"request_id": REQ}),
        _t("staffing_check", ToolClass.COMPUTE, "Read-only what-if for this period: minimum "
           "headcount, coverage shortfall cells and standby shifts with some nurses out for the "
           "whole period and/or k hypothetical hires. Numbers are lower bounds.", staffing_check,
           {"hires": {"type": "integer"}, "removed": {"type": "array", "items": NURSE},
            "coverage": {"type": "string", "enum": ["target", "floor"]},
            "standby": {"type": "boolean"}}),
        _t("rule_check_swap", ToolClass.COMPUTE, "Hard rules each nurse in a swap would break.",
           rule_check_swap, {"request_id": REQ}),
        _t("explain_denied_requests", ToolClass.COMPUTE, "Fill and list the reason code of each "
           "denied request in a run.", explain_denied_requests, {"run_id": RUN}),
        _t("diagnose", ToolClass.COMPUTE, "The conflict's unsat core and why other nurses cannot "
           "help.", diagnose, {"conflict_id": CON}),
        _t("list_conflict_options", ToolClass.COMPUTE, "The options the charge nurse can pick "
           "for a conflict.", list_conflict_options, {"conflict_id": CON}),
        _t("apply_option", ToolClass.COMPUTE, "Confirm the conflict option the charge nurse "
           "chose; returns the run started with it.", apply_option,
           {"option_id": S, "conflict_id": CON}, ("option_id",)),
        _t("send_message", ToolClass.ACTION, "Draft and send the ask to a nurse: kind "
           "'replacement' (needs the run approved by the charge nurse, or the nurse to be next "
           "in the approved cascade order) or 'swap' (to the swap counterpart). Returns the "
           "message id.", send_message,
           {"nurse_id": NURSE, "kind": {"type": "string", "enum": ["replacement", "swap"]},
            "run_id": RUN, "request_id": REQ}, ("nurse_id", "kind")),
        _t("wait_for_reply", ToolClass.ACTION, "Pause until the nurse taps Accept or Decline.",
           wait_for_reply, {"nurse_id": NURSE, "message_id": S,
                            "timeout_minutes": {"type": "integer"}}, ("nurse_id",), waits=True),
        _t("request_approval", ToolClass.ACTION, "Show the charge nurse a summary (with claims "
           "citing tool results) and pause for their decision on a proposal run, a conflict, or a "
           "pending leave or swap request. With run_id, `cascade` (1-3 ranked nurse ids in "
           "order) asks for a contact plan instead of publishing one candidate: approving it "
           "publishes nothing.", request_approval,
           {"summary": S, "claims": CLAIMS, "run_id": RUN, "conflict_id": CON,
            "request_id": REQ, "cascade": {"type": "array", "items": NURSE}},
           ("summary",), waits=True),
        _t("escalate", ToolClass.ACTION, "Hand the workflow to the charge nurse with what was "
           "tried.", escalate, {"reason": S}, ("reason",)),
    ]
}  # fmt: skip

READ_BASE = ("get_run_summary", "get_proposal", "get_fairness_history", "get_nurse", "find_nurse")
SICK = (
    *READ_BASE,
    "get_open_cells",
    "compare_candidates",
    "get_assignment_evidence",
    "rank_replacements",
    "send_message",
    "wait_for_reply",
    "request_approval",
    "escalate",
)
ALLOWLIST: dict[str, frozenset[str]] = {
    "sick_call": frozenset(SICK),
    "demand_change": frozenset(SICK),
    "conflict": frozenset((*READ_BASE, "diagnose", "list_conflict_options", "apply_option",
                           "staffing_check", "run_resolve", "request_approval", "escalate")),
    "swap": frozenset((*READ_BASE, "get_pending_requests", "rule_check_swap", "what_if",
                       "send_message", "wait_for_reply", "request_approval", "run_resolve",
                       "escalate")),
    "leave": frozenset((*READ_BASE, "get_pending_requests", "get_request_outcome", "what_if",
                        "staffing_check", "request_approval", "run_resolve", "escalate")),
    "new_period": frozenset((*READ_BASE, "get_request_outcome", "run_full",
                             "explain_denied_requests", "request_approval", "escalate")),
}  # fmt: skip


def specs(workflow: str) -> list[dict[str, Any]]:
    allowed = ALLOWLIST[workflow]
    return [t.spec() for name, t in REGISTRY.items() if name in allowed]
