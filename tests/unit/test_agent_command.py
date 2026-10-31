"""The read-only command agent: its allowlist holds only reads, the fallback routes goals
to one investigation round each, refusals name the blocked action, and answers cite
checked tool results. Writes are rejected by code, never by the model."""

import asyncio
import sqlite3

from app.agent import fallback, loop
from app.agent.tools import ALLOWLIST, REGISTRY, AgentCtx, ToolClass
from app.explain import claims as claimcheck
from app.explain.tools import SPECS
from app.log.decision_log import DecisionLog
from app.store import repo
from tests.helpers import mini_baseline


def test_command_allowlist_is_exactly_the_read_tools() -> None:
    allowed = ALLOWLIST["command"]
    assert allowed == frozenset(
        [s["function"]["name"] for s in SPECS]
        + ["get_open_cells", "get_nurse", "staffing_check", "what_if"]
    )
    for name in allowed:
        assert REGISTRY[name].cls in (ToolClass.READ, ToolClass.COMPUTE), name
    for name in (
        "send_message",
        "wait_for_reply",
        "request_approval",
        "rank_replacements",
        "run_resolve",
        "run_full",
        "evaluate_condition",
        "find_swap_partners",
        "escalate",
    ):
        assert name not in allowed


def test_command_routing() -> None:
    nxt = lambda goal, calls=None: fallback.next_call(  # noqa: E731
        "command", calls or [], {"goal": goal}
    )
    assert nxt("tonight looks thin") == ("staffing_check", {})
    assert nxt("publish the roster now") is None
    assert nxt("send Arun a message") is None
    assert nxt("show the impact if R-0007 goes ahead") == ("what_if", {"request_id": "R-0007"})
    assert nxt("what changes if leave R-0007 goes ahead") == ("what_if", {"request_id": "R-0007"})
    assert nxt("change the roster to grant R-0007") is None
    assert nxt("is the roster fair") == ("get_fairness_history", {})
    assert nxt("who is N03") == ("get_nurse", {"nurse": "N03"})
    assert nxt("how is the roster") == ("get_run_summary", {})
    done = [{"tool": "staffing_check", "call_id": "c1", "result": {"shortfall": 0}}]
    assert nxt("tonight looks thin", done) is None
    failed = [
        {"tool": "staffing_check", "call_id": "c1", "result": {"error": "nothing is published yet"}}
    ]
    # a failed check is never retried; the default summary still gets its round
    assert nxt("tonight looks thin", failed) == ("get_run_summary", {})


def test_command_refusal_names_the_action() -> None:
    text, claims = fallback.command_answer([], {"goal": "publish the roster now"})
    assert "can't publish" in text and claims == []
    text, _ = fallback.command_answer([], {"goal": "grant every leave"})
    assert "can't grant requests" in text


def test_command_answer_cites_the_staffing_result() -> None:
    calls = [
        {
            "tool": "staffing_check",
            "call_id": "c1",
            "result": {
                "status": "ok",
                "shortfall": 2,
                "headcount": 7,
                "hires_needed": 1,
                "standby_shifts": 3,
                "short_cells": [{"day": "2026-11-05", "shift": "N"}],
            },
        }
    ]
    text, claims = fallback.command_answer(calls, {"goal": "tonight looks thin"})
    assert "shortfall 2" in text and "2026-11-05 N" in text
    assert len(claims) == 4
    assert claimcheck.check(claims, {"c1": calls[0]["result"]}) == []


def _ctx(conn: sqlite3.Connection) -> AgentCtx:
    ward = repo.get_ward(conn)
    assert ward is not None
    task = {
        "id": "task-1",
        "workflow": "command",
        "mode": "agent",
        "state": {
            "calls": [],
            "trace": [],
            "goal": "publish the roster now",
            "counters": {
                "tool_calls": 0,
                "messages": 0,
                "declines": 0,
                "timeouts": 0,
                "model_ms": 0,
                "turns": 0,
                "failures": 0,
                "forbidden": 0,
                "approval_violations": 0,
            },
        },
    }
    return AgentCtx(conn, ward, task, None, None, DecisionLog(conn, ward.id))  # type: ignore[arg-type]


def test_command_rejects_every_write(conn: sqlite3.Connection) -> None:
    mini_baseline(conn)
    ctx = _ctx(conn)
    for name, args in [
        ("send_message", {"nurse_id": "N01", "kind": "replacement"}),
        ("request_approval", {"summary": "s"}),
        ("rank_replacements", {}),
        ("run_resolve", {}),
        ("run_full", {}),
    ]:
        result, waits = asyncio.run(loop._execute(ctx, name, args, "", None))
        assert "not allowed in the command workflow" in result["error"], name
        assert waits is False
    assert ctx.state["counters"]["forbidden"] == 5
    assert repo.list_nurse_messages(conn, "ward-mini") == []
    assert [
        e for e in repo.list_last_events(conn, "ward-mini", n=50) if e["kind"] == "agent.rejected"
    ] != []
