"""Enforced reply timeouts: fake-clock unit tests for the tick core, the wait clamp,
and timeout counting in the loop and the fallback."""

import itertools
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from app.agent import fallback, timeouts
from app.store import repo
from app.store.seed import seed_db

FAKE_NOW = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)


def _past() -> str:
    return "2026-11-03T00:59:00.000Z"


def _future() -> str:
    return "2026-11-03T02:00:00.000Z"


def _seeded(conn: sqlite3.Connection) -> Any:
    seed_db(conn, "mini", solve=False)
    ward = repo.get_ward(conn)
    assert ward is not None
    return ward


def _waiting_task(
    conn: sqlite3.Connection, ward_id: str, task_id: str, message_id: str, deadline: str
) -> None:
    state = {
        "waiting": {
            "kind": "reply",
            "nurse_id": "N07",
            "message_id": message_id,
            "call_id": "a9",
            "since": "2026-11-03T00:58:00.000Z",
            "deadline_at": deadline,
        },
        "counters": {"tool_calls": 3, "messages": 1, "declines": 0, "model_ms": 0, "turns": 1},
    }
    conn.execute(
        "INSERT INTO agent_task(id, ward_id, workflow, status, waiting_on, state_json)"
        " VALUES(?, ?, 'sick_call', 'waiting_reply', 'N07', ?)",
        (task_id, ward_id, json.dumps(state)),
    )
    repo.insert_nurse_message(
        conn,
        {
            "id": message_id,
            "ward_id": ward_id,
            "nurse_id": "N07",
            "kind": "replacement",
            "task_id": task_id,
            "day": "2026-11-03",
            "shift": "M",
            "text": "Cover?",
            "status": "asked",
        },
    )
    conn.commit()


def test_due_timeout_expires_message_logs_and_answers_once(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import jobs

    ward = _seeded(conn)
    monkeypatch.setattr(timeouts, "utcnow", lambda: FAKE_NOW)
    _waiting_task(conn, ward.id, "task-due", "m-due", _past())
    _waiting_task(conn, ward.id, "task-early", "m-early", _future())

    due = jobs.collect_due_timeouts(conn, ward.id)
    assert due == [("task-due", {"reply": "timeout", "nurse_id": "N07", "message_id": "m-due"})]
    assert repo.get_nurse_message(conn, "m-due")["status"] == "expired"
    assert repo.get_nurse_message(conn, "m-early")["status"] == "asked"
    ev = next(e for e in repo.list_events(conn, ward.id) if e["kind"] == "agent.timeout")
    assert ev["actor"] == "system"
    assert ev["payload"]["task_id"] == "task-due"
    assert ev["payload"]["message_id"] == "m-due"

    # idempotent: nothing is due twice, so a resume can only happen once
    assert jobs.collect_due_timeouts(conn, ward.id) == []


def test_wait_without_deadline_never_fires(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import jobs

    ward = _seeded(conn)
    monkeypatch.setattr(timeouts, "utcnow", lambda: FAKE_NOW)
    state = {
        "waiting": {"kind": "reply", "nurse_id": "N07", "message_id": "m-old", "call_id": "a9"},
        "counters": {},
    }
    conn.execute(
        "INSERT INTO agent_task(id, ward_id, workflow, status, waiting_on, state_json)"
        " VALUES('task-old', ?, 'sick_call', 'waiting_reply', 'N07', ?)",
        (ward.id, json.dumps(state)),
    )
    repo.insert_nurse_message(
        conn,
        {
            "id": "m-old",
            "ward_id": ward.id,
            "nurse_id": "N07",
            "kind": "replacement",
            "text": "Cover?",
            "status": "asked",
        },
    )
    conn.commit()
    assert jobs.collect_due_timeouts(conn, ward.id) == []
    assert repo.get_nurse_message(conn, "m-old")["status"] == "asked"


def test_wait_for_reply_clamps_to_the_setting(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agent.tools import AgentCtx, wait_for_reply
    from app.config import get_settings

    ward = _seeded(conn)
    monkeypatch.setenv("AGENT_REPLY_TIMEOUT_SECONDS", "60")
    monkeypatch.setattr(timeouts, "utcnow", lambda: FAKE_NOW)
    get_settings.cache_clear()
    repo.insert_nurse_message(
        conn,
        {
            "id": "m-clamp",
            "ward_id": ward.id,
            "nurse_id": "N07",
            "kind": "replacement",
            "task_id": "t",
            "text": "Cover?",
            "status": "asked",
        },
    )
    ctx = AgentCtx(
        conn=conn,
        ward=ward,
        task={"id": "t", "request_id": "R-1"},
        env=None,
        gateway=None,
        log=None,
    )  # type: ignore[arg-type]
    import asyncio

    out = asyncio.run(wait_for_reply(ctx, nurse_id="N07", message_id="m-clamp", timeout_minutes=30))
    assert out["timeout_minutes"] == 1
    assert out["deadline_at"] == "2026-11-03T01:01:00.000Z"
    out = asyncio.run(wait_for_reply(ctx, nurse_id="N07", message_id="m-clamp", timeout_minutes=1))
    assert out["deadline_at"] == "2026-11-03T01:01:00.000Z"


def test_fallback_counts_timeout_like_a_decline_but_names_it() -> None:
    ids = itertools.count(1)

    def done(tool: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        # the loop stamps every recorded call with a call id; the fixture mirrors it
        return {"tool": tool, "args": {"run_id": "r"}, "ok": True,
                "call_id": f"c{next(ids)}", "result": result or {}}  # fmt: skip

    def round(nurse: str, reply: str, run: str) -> list[dict[str, Any]]:
        return [
            done("rank_replacements", {"status": "proposed", "run_id": run}),
            done("request_approval", {"decision": "approved", "nurse": nurse, "run_id": run}),
            done("send_message", {"nurse_id": nurse, "message_id": "m"}),
            done("wait_for_reply", {"reply": reply, "nurse_id": nurse, "message_id": "m"}),
        ]

    calls = (
        [done("get_open_cells")] + round("S01", "declined", "r1") + round("N11", "timeout", "r2")
    )
    assert fallback.declined(calls) == ["S01"]
    assert fallback.timed_out(calls) == ["N11"]
    # one slot left: exclude both, so the timed-out nurse is not asked again
    assert fallback.next_call("sick_call", calls, {}) == (
        "rank_replacements",
        {"exclude": ["S01", "N11"]},
    )
    name, args = fallback.next_call("sick_call", calls + round("N04", "timeout", "r3"), {})
    assert name == "escalate"
    assert "timed out" in args["reason"] and "declined" in args["reason"]
    # no timeouts: the old reason string is byte-identical (existing expectations)
    plain = (
        [done("get_open_cells")]
        + round("S01", "declined", "r1")
        + round("N11", "declined", "r2")
        + round("N04", "declined", "r3")
    )
    name, args = fallback.next_call("sick_call", plain, {})
    assert args["reason"] == "3 nurses declined the shift: S01, N11, N04"
