"""Roster agent loop (design doc 7.10): a resumable Muse Spark tool loop, one `agent_task` per
workflow.

`start` creates the task and runs it until it waits on a person (`request_approval`,
`wait_for_reply`), finishes or escalates; the task stores its chat, its calls and the pending call,
so `resume` (an approval, a reply) continues the same plan. Guardrails:

- per-workflow allowlist; a call outside it, or one that breaks a guardrail (a replacement message
  before approval), is refused, counted and logged (`agent.rejected`);
- budgets: 12 tool calls (waiting on a person is not counted), 4 messages to nurses, 3 declines,
  60 s of model time; the first three end in `escalate`, model time switches to the fallback;
- two failures in a row (model error, malformed or refused call, stopping before the workflow is
  done) switch the task to the deterministic fallback (`fallback.next_call`), labelled in the trace
  and the log;
- every approval summary goes through the claim checker; failing claims are replaced by
  `describe()` text.
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.agent import fallback
from app.agent.tools import ALLOWLIST, REGISTRY, AgentCtx, Forbidden, ToolError, specs
from app.config import get_settings
from app.intake.dates import ward_now
from app.llm.cache import PROMPTS
from app.llm.types import LLMError, LLMRequest, Role
from app.store import repo

MAX_TOOL_CALLS = 12
MAX_MESSAGES = 4
MAX_DECLINES = 3
MODEL_S = 60.0
MAX_FAILURES = 2
MAX_STEPS = 40  # loop iterations per advance, a backstop


def new_state(**trigger: Any) -> dict[str, Any]:
    counters = {
        "tool_calls": 0,
        "messages": 0,
        "declines": 0,
        "timeouts": 0,
        "model_ms": 0,
        "turns": 0,
        "failures": 0,
        "forbidden": 0,
        "approval_violations": 0,
    }
    return {"calls": [], "trace": [], "counters": counters, "approved_runs": [], **trigger}


def context(ctx: AgentCtx) -> dict[str, Any]:
    st, w = ctx.state, ctx.ward
    trigger = {k: st[k] for k in ("request_id", "conflict_id", "run_id", "counterpart")
               if st.get(k)}  # fmt: skip
    r = repo.get_request(ctx.conn, st["request_id"]) if st.get("request_id") else None
    if r is not None:
        trigger |= {
            "kind": r.kind.value,
            "nurse": r.nurse_id,
            "dates": [d.isoformat() for d in r.dates],
            "shifts": None if r.shifts is None else [s.value for s in r.shifts],
        }
    now = ward_now(w.tz, get_settings().demo_now)
    return {
        "workflow": ctx.task["workflow"],
        "trigger": trigger,
        "today": now.date().isoformat(),
        "now": now.strftime("%H:%M"),
        "published_run": ctx.published_id(),
        "nurses": [f"{n.id} | {n.name}{' | standby' if n.standby else ''}" for n in w.nurses],
        "budgets": {
            "tool_calls": MAX_TOOL_CALLS,
            "messages": MAX_MESSAGES,
            "declines": MAX_DECLINES,
        },  # fmt: skip
    }


def first_messages(ctx: AgentCtx) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": (PROMPTS / "agent.md").read_text(encoding="utf-8")},
        {"role": "user", "content": "<context>"
         + json.dumps(context(ctx), ensure_ascii=False, sort_keys=True) + "</context>"},
    ]  # fmt: skip


# ---------------------------------------------------------------- bookkeeping


def _actor(ctx: AgentCtx) -> str:
    return "llm:agent" if ctx.task["mode"] == "agent" else "planner"


def _trace(ctx: AgentCtx, kind: str, **data: Any) -> None:
    tr = ctx.state["trace"]
    tr.append({"n": len(tr) + 1, "ts": repo.now_iso_ms(), "kind": kind,
               "mode": ctx.task["mode"], **data})  # fmt: skip


async def _save(ctx: AgentCtx) -> None:
    ctx.task["steps"] = len(ctx.state["calls"])
    repo.save_agent_task(ctx.conn, ctx.task)
    await ctx.env.broadcast(ctx.ward.id, {"type": "agent_updated", "task_id": ctx.task["id"]})


def _names(ctx: AgentCtx) -> dict[str, str]:
    return {n.id: n.name for n in ctx.ward.nurses}


def withdraw_open_asks(
    conn: Any, ward_id: str, log: Any, actor: str, *, day: Any = None, shift: Any = None,
    except_id: Any = None, task_id: Any = None,
) -> list[dict[str, Any]]:  # fmt: skip
    """Withdraw open replacement asks (an accept made them moot); returns what changed."""
    out = []
    for m in repo.list_nurse_messages(conn, ward_id):
        if m["kind"] != "replacement" or m["status"] != "asked":
            continue
        if (day is not None or shift is not None) and (m["day"], m["shift"]) != (day, shift):
            continue
        if except_id is not None and m["id"] == except_id:
            continue
        if task_id is not None and m["task_id"] != task_id:
            continue
        repo.set_nurse_message_status(conn, m["id"], "withdrawn")
        log.emit("replacement.withdrawn", actor, None, None, message_id=m["id"],
                 nurse=m["nurse_id"], task_id=m["task_id"])  # fmt: skip
        out.append(m)
    return out


async def _finish(ctx: AgentCtx, status: str, outcome: str) -> None:
    for m in withdraw_open_asks(ctx.conn, ctx.ward.id, ctx.log, _actor(ctx),
                                task_id=ctx.task["id"]):  # fmt: skip
        await ctx.env.broadcast(ctx.ward.id, {"type": "message_for_nurse",
                                              "nurse_id": m["nurse_id"]})  # fmt: skip
    ctx.task.update(status=status, waiting_on=None, pending_call=None, outcome=outcome)
    _trace(ctx, status, brief=outcome)
    kind = "agent.escalated" if status == "escalated" else "agent.done"
    ctx.log.emit(kind, _actor(ctx), None, None, about_run=ctx.state.get("last_run"),
                 task_id=ctx.task["id"], workflow=ctx.task["workflow"], outcome=outcome,
                 mode=ctx.task["mode"], counters=ctx.state["counters"])  # fmt: skip
    await _save(ctx)


def outcome_line(ctx: AgentCtx) -> str:
    calls, names = ctx.state["calls"], _names(ctx)
    for c in reversed(calls):
        r = c.get("result") or {}
        if c["tool"] == "wait_for_reply" and r.get("reply"):
            who = str(r.get("nurse_id"))
            return f"{names.get(who, who)} {r['reply']}"
        if c["tool"] == "request_approval" and r.get("decision"):
            what = "published" if r["decision"] == "approved" else r["decision"]
            return f"{what} by the charge nurse" if r["decision"] != "superseded" else (
                "superseded by another published roster")  # fmt: skip
        if c["tool"] in ("rank_replacements", "run_resolve", "run_full") and r.get(
            "status") == "infeasible":  # fmt: skip
            return f"no legal roster: conflict {r.get('conflict_id')} opened"
    return "finished"


def _switch_to_fallback(ctx: AgentCtx, reason: str) -> None:
    if ctx.task["mode"] == "fallback":
        return
    ctx.task["mode"] = "fallback"
    ctx.state["fallback_reason"] = reason
    _trace(ctx, "fallback", brief=reason)
    ctx.log.emit("agent.fallback", "planner", None, None, task_id=ctx.task["id"],
                 workflow=ctx.task["workflow"], reason=reason)  # fmt: skip


def _fail(ctx: AgentCtx, why: str) -> None:
    c = ctx.state["counters"]
    c["failures"] += 1
    _trace(ctx, "error", brief=why)
    if c["failures"] >= MAX_FAILURES:
        _switch_to_fallback(ctx, f"{c['failures']} failures in a row; last: {why}")


# ---------------------------------------------------------------- one call


async def _execute(ctx: AgentCtx, name: str, args: dict[str, Any], plan: str,
                   model_call_id: str | None) -> tuple[dict[str, Any], bool]:  # fmt: skip
    """Run one call; returns (result, waits). A waiting call is recorded with no result yet."""
    c = ctx.state["counters"]
    cid = f"c{len(ctx.state['calls']) + 1}"
    tool = REGISTRY.get(name)
    if tool is None or name not in ALLOWLIST[ctx.task["workflow"]]:
        c["forbidden"] += 1
        why = f"tool {name!r} is not allowed in the {ctx.task['workflow']} workflow"
        ctx.log.emit("agent.rejected", _actor(ctx), None, None, task_id=ctx.task["id"],
                     tool=name, reason=why)  # fmt: skip
        result: dict[str, Any] = {"error": why}
    else:
        if not tool.waits:
            c["tool_calls"] += 1
        if name == "send_message":
            c["messages"] += 1
        try:
            result = await tool.fn(ctx, **args)
        except Forbidden as exc:
            c["forbidden"] += 1
            c["approval_violations"] += 1
            ctx.log.emit("agent.rejected", _actor(ctx), None, None, task_id=ctx.task["id"],
                         tool=name, reason=str(exc))  # fmt: skip
            result = {"error": str(exc)}
        except ToolError as exc:
            result = {"error": str(exc)}
        except TypeError as exc:
            result = {"error": f"bad arguments: {exc}"}
    if "answered" in result:  # the person answered before the agent asked
        result = apply_answer(ctx, result["answered"])
    waits = tool is not None and tool.waits and "wait" in result
    entry = {"call_id": cid, "tool": name, "args": args, "result": None if waits else result,
             "model_call_id": model_call_id, "mode": ctx.task["mode"]}  # fmt: skip
    ctx.state["calls"].append(entry)
    if "error" in result:
        _fail(ctx, f"{name}: {result['error']}")
    else:
        c["failures"] = 0
    for key in ("run_id",):
        if not waits and isinstance(result.get(key), str):
            ctx.state["last_run"] = result[key]
    brief = fallback.brief(name, result, _names(ctx)) if not waits else _wait_brief(ctx, result)
    _trace(ctx, "wait" if waits else "tool", tool=name, plan=plan, brief=brief, call_id=cid)
    ctx.log.emit("agent.step", _actor(ctx), None, None, about_run=ctx.state.get("last_run"),
                 task_id=ctx.task["id"], n=len(ctx.state["calls"]), tool=name,
                 args=_compact(args), brief=brief, mode=ctx.task["mode"])  # fmt: skip
    if waits:
        _wait(ctx, cid, name, args, result, model_call_id)
    return result, waits


def _compact(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k != "claims"}


def _wait_brief(ctx: AgentCtx, w: dict[str, Any]) -> str:
    if w.get("wait") == "reply":
        return f"waiting for {_names(ctx).get(w['on'], w['on'])}'s reply"
    return f"waiting for the charge nurse: {w.get('approval', {}).get('kind')}"


def _wait(ctx: AgentCtx, cid: str, name: str, args: dict[str, Any], w: dict[str, Any],
          model_call_id: str | None) -> None:  # fmt: skip
    if name == "request_approval":
        summary, claims, source = w["summary"], w["claims"], "agent"
        if w["problems"] or not summary.strip():
            prior = [x for x in ctx.state["calls"] if x["call_id"] != cid]
            summary, claims = fallback.approval_summary(prior)
            source = "describe"
        ap = w["approval"]
        ctx.state["waiting"] = {"kind": "approval", "approval_kind": ap["kind"], "ref": ap["ref"],
                                "runs": ap.get("runs", []), "summary": summary,
                                "claims": claims, "summary_source": source,
                                "cascade": ap.get("cascade"), "parent_run": ap.get("parent_run"),
                                "problems": w["problems"], "call_id": cid}  # fmt: skip
        ctx.task.update(status="waiting_approval", waiting_on="charge")
    else:
        ctx.state["waiting"] = {"kind": "reply", "nurse_id": w["on"],
                                "message_id": w["message_id"], "call_id": cid,
                                "since": repo.now_iso_ms(),
                                "deadline_at": w.get("deadline_at")}  # fmt: skip
        ctx.task.update(status="waiting_reply", waiting_on=w["on"])
    ctx.task["pending_call"] = {"call_id": cid, "name": name, "args": args,
                                "model_call_id": model_call_id}  # fmt: skip
    ctx.log.emit("agent.waiting", _actor(ctx), None, None, about_run=ctx.state.get("last_run"),
                 task_id=ctx.task["id"], on=ctx.task["waiting_on"],
                 waiting=_compact(ctx.state["waiting"]))  # fmt: skip


def _tool_message(ctx: AgentCtx, call: dict[str, Any]) -> None:
    """Append a finished call's result to the chat, in the format the model asked in."""
    if not ctx.task["messages"]:
        return
    body = json.dumps({"call_id": call["call_id"], "tool": call["tool"], "args": call["args"],
                       "result": call["result"]}, ensure_ascii=False, default=str,
                      sort_keys=True)  # fmt: skip
    mid = call.get("model_call_id")
    if mid is None:
        ctx.task["messages"].append({"role": "user", "content": f"Tool result:\n{body}"})
    else:
        ctx.task["messages"].append({"role": "tool", "tool_call_id": mid, "content": body})


# ---------------------------------------------------------------- the loop


async def _model_turn(ctx: AgentCtx) -> list[tuple[str, dict[str, Any], str, str | None]] | None:
    """One Muse turn -> calls as (name, args, plan, model call id); [] = it stopped; None = the
    turn failed."""
    c = ctx.state["counters"]
    req = LLMRequest(Role.AGENT, list(ctx.task["messages"]), tools=specs(ctx.task["workflow"]))
    t0 = time.perf_counter()
    try:
        resp = await ctx.gateway.complete(req, run_id=ctx.state.get("last_run"))
    except LLMError as exc:
        c["model_ms"] += int(1000 * (time.perf_counter() - t0))
        _fail(ctx, f"model call failed: {exc}")
        return None
    c["model_ms"] += 0 if resp.cache_hit else resp.latency_ms
    c["turns"] += 1
    plan = " ".join((resp.text or "").split())[:160]
    if not resp.tool_calls:
        ctx.state["final_text"] = plan
        return []
    calls = list(resp.tool_calls)
    waits = [i for i, tc in enumerate(calls) if tc.name in ("wait_for_reply", "request_approval")]
    if waits:
        calls = calls[: waits[0] + 1]  # nothing runs after a pause
    if resp.json_protocol:
        tc = calls[0]
        ctx.task["messages"].append({"role": "assistant", "content": json.dumps(
            {"tool": tc.name, "args": tc.arguments}, sort_keys=True)})  # fmt: skip
        return [(tc.name, dict(tc.arguments), plan, None)]
    ctx.task["messages"].append({
        "role": "assistant", "content": resp.text or "",
        "tool_calls": [{"id": tc.id, "type": "function", "function": {
            "name": tc.name, "arguments": json.dumps(tc.arguments, sort_keys=True)}}
            for tc in calls],
    })  # fmt: skip
    return [(tc.name, dict(tc.arguments), plan, tc.id) for tc in calls]


def _over_budget(ctx: AgentCtx) -> str | None:
    c = ctx.state["counters"]
    if c["tool_calls"] >= MAX_TOOL_CALLS:
        return f"tool-call budget ({MAX_TOOL_CALLS}) used up"
    if c["messages"] > MAX_MESSAGES:
        return f"message budget ({MAX_MESSAGES}) used up"
    if c["declines"] > MAX_DECLINES:
        return f"{c['declines']} declines"
    return None


async def advance(ctx: AgentCtx) -> None:
    """Run the task until it waits, finishes or escalates."""
    st = ctx.state
    st["published_now"] = ctx.published_id()  # the fallback compares it to the ranking's parent
    for _ in range(MAX_STEPS):
        if ctx.task["status"] != "running":
            return
        over = _over_budget(ctx)
        if over and fallback.finished(ctx.task["workflow"], st["calls"], st):
            over = None  # the last allowed call finished the workflow
        if over:
            await _execute(ctx, "escalate", {"reason": over}, "Budget used up.", None)
            await _finish(ctx, "escalated", over)
            return
        if ctx.task["mode"] == "agent" and st["counters"]["model_ms"] >= MODEL_S * 1000:
            _switch_to_fallback(ctx, f"model time budget ({MODEL_S:.0f} s) used up")

        if ctx.task["mode"] == "agent":
            turn = await _model_turn(ctx)
            if turn is None:
                await _save(ctx)
                continue
            if not turn:
                if fallback.finished(ctx.task["workflow"], st["calls"], st):
                    await _finish(ctx, "done", outcome_line(ctx))
                    return
                nxt = fallback.next_call(ctx.task["workflow"], st["calls"], st)
                _fail(ctx, "stopped before the workflow was finished")
                ctx.task["messages"].append({"role": "user", "content": (
                    "The workflow is not finished. Next in the default order: "
                    f"{nxt[0] if nxt else '?'}. Continue with tool calls.")})  # fmt: skip
                await _save(ctx)
                continue
            calls = turn
        else:
            step = fallback.next_call(ctx.task["workflow"], st["calls"], st)
            if step is None:
                await _finish(ctx, "done", outcome_line(ctx))
                return
            calls = [(step[0], step[1], fallback.PLAN.get(step[0], ""), None)]

        mode = ctx.task["mode"]
        for name, args, plan, mid in calls:
            result, waits = await _execute(ctx, name, args, plan, mid)
            if waits:
                await _save(ctx)
                return
            _tool_message(ctx, ctx.state["calls"][-1])
            if name == "escalate" and "error" not in result:
                await _finish(ctx, "escalated", str(result.get("reason") or "escalated"))
                return
            if ctx.task["mode"] != mode:
                break  # switched to the fallback: the model's other calls are dropped
        await _save(ctx)
    _switch_to_fallback(ctx, "step limit reached")
    await _save(ctx)


def apply_answer(ctx: AgentCtx, answer: dict[str, Any]) -> dict[str, Any]:
    """Book a person's answer (approval or reply); returns it as the call's result."""
    answer = dict(answer)
    prefetch = answer.pop("prefetch", None)
    if prefetch:
        ctx.state["prefetch"] = prefetch
    if answer.get("decision") == "approved" and answer.get("run_id"):
        ctx.state["approved_runs"].append(answer["run_id"])
        ctx.state["last_run"] = answer["run_id"]
    if answer.get("decision") == "approved" and answer.get("cascade") and not answer.get("nurse"):
        ctx.state["approved_cascade"] = {
            "run_id": answer["run_id"],
            "order": [str(n) for n in answer["cascade"]],
            "open_cell": ctx.state.get("open_cell"),
            "parent_run": answer.get("parent_run"),
        }  # approving a plan publishes nothing; the Publish button still does that
    if answer.get("reply") == "declined":
        ctx.state["counters"]["declines"] += 1
    elif answer.get("reply") == "timeout":
        # a silence counts like a decline against the budget, and is recorded separately
        ctx.state["counters"]["declines"] += 1
        c = ctx.state["counters"]
        c["timeouts"] = c.get("timeouts", 0) + 1  # tasks saved before Task A lack the key
    return answer


async def resume(ctx: AgentCtx, answer: dict[str, Any]) -> None:
    """Deliver a person's answer to the pending call, then continue."""
    pc = ctx.task.get("pending_call")
    if pc is None or ctx.task["status"] not in ("waiting_reply", "waiting_approval"):
        return
    call = next(c for c in ctx.state["calls"] if c["call_id"] == pc["call_id"])
    answer = apply_answer(ctx, answer)
    cell = ctx.state.get("open_cell") or {}
    if answer.get("reply") == "accepted" and cell.get("day") and cell.get("shift"):
        for m in withdraw_open_asks(ctx.conn, ctx.ward.id, ctx.log, _actor(ctx),
                                    day=cell["day"], shift=cell["shift"],
                                    except_id=answer.get("message_id")):  # fmt: skip
            await ctx.env.broadcast(ctx.ward.id, {"type": "message_for_nurse",
                                                  "nurse_id": m["nurse_id"]})  # fmt: skip
    call["result"] = answer
    ctx.task.update(status="running", waiting_on=None, pending_call=None)
    ctx.state.pop("waiting", None)
    brief = ", ".join(f"{k} {v}" for k, v in answer.items() if v is not None)
    _trace(ctx, "resumed", tool=call["tool"], brief=brief, call_id=call["call_id"])
    ctx.log.emit("agent.resumed", _actor(ctx), None, None, about_run=ctx.state.get("last_run"),
                 task_id=ctx.task["id"], answer=answer)  # fmt: skip
    _tool_message(ctx, call)
    await advance(ctx)
