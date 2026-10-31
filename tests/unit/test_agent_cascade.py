"""The replacement cascade: one approved contact plan, nurses asked in order, the first
accept coming back as one publish approval. Code enforces the order; the model only chooses
to follow the plan."""

import asyncio
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import fallback, loop
from app.agent.tools import AgentCtx, Forbidden, ToolError, request_approval, send_message
from app.config import get_settings
from app.domain.models import Ward
from app.explain.claims import check
from app.log.decision_log import DecisionLog
from app.store import repo
from tests.helpers import mini_baseline, scenario_a

CANDS = [
    {"nurse": "S01", "run_id": "C1", "changed_cells": 1, "name": "Arun", "rank": 1},
    {"nurse": "N05", "run_id": "C2", "changed_cells": 2, "name": "Kavya", "rank": 2},
    {"nurse": "N04", "run_id": "C3", "changed_cells": 2, "name": "Priya", "rank": 3},
]
ORDER = ["S01", "N05", "N04"]
CELL = {"nurse": "N03", "day": "2026-11-03", "shift": "M"}


class H:
    """A fabricated call history with call ids, in execution order."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> "H":
        self.calls.append({"call_id": f"c{len(self.calls) + 1}", "tool": tool, "args": args,
                           "result": result, "mode": "fallback"})  # fmt: skip
        return self

    def rank(self, run: str, cands: list[dict[str, Any]] = CANDS) -> "H":
        return self.add(
            "rank_replacements",
            {"exclude": []},
            {"status": "proposed", "run_id": run, "candidates": cands, "changed_cells": 3},
        )

    def cascade_ask(self, run: str, order: list[str] = ORDER) -> "H":
        return self.add(
            "request_approval", {"run_id": run, "cascade": order, "summary": "s", "claims": []}, {}
        )

    def cascade_ok(self, run: str, order: list[str] = ORDER) -> "H":
        c = self.calls[-1]
        assert c["tool"] == "request_approval"
        c["result"] = {"decision": "approved", "run_id": run, "cascade": order, "parent_run": "P0"}
        return self

    def asked(self, nurse: str, run: str = "R1") -> "H":
        m = f"m-{nurse}"
        return self.add(
            "send_message",
            {"nurse_id": nurse, "kind": "replacement", "run_id": run},
            {"message_id": m, "nurse_id": nurse},
        )

    def wait(self, nurse: str, reply: str) -> "H":
        return self.add(
            "wait_for_reply",
            {"nurse_id": nurse, "message_id": f"m-{nurse}"},
            {"reply": reply, "nurse_id": nurse, "message_id": f"m-{nurse}"},
        )


def cascade_state() -> dict[str, Any]:
    return {"approved_cascade": {"run_id": "R1", "order": list(ORDER), "open_cell": dict(CELL),
                                 "parent_run": "P0"},
            "published_now": "P0"}  # fmt: skip


def step(h: H, state: dict[str, Any] | None = None) -> Any:
    return fallback.next_call("sick_call", h.calls, state or {})


def test_cascade_decline_then_accept_asks_in_order() -> None:
    h = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    assert step(h) == ("rank_replacements", {"exclude": []})
    h.rank("R1")
    name, args = step(h)
    assert (name, args["run_id"], args["cascade"]) == ("request_approval", "R1", ORDER)
    h.cascade_ask("R1").cascade_ok("R1")
    st = cascade_state()
    assert step(h, st) == (
        "send_message",
        {"nurse_id": "S01", "kind": "replacement", "run_id": "R1"},
    )
    h.asked("S01")
    name, args = step(h, st)
    assert (name, args["nurse_id"]) == ("wait_for_reply", "S01")
    h.wait("S01", "declined")
    assert step(h, st)[1]["nurse_id"] == "N05"  # next in the plan, no new approval
    h.asked("N05")
    assert step(h, st)[0] == "wait_for_reply"
    h.wait("N05", "accepted")
    name, args = step(h, st)
    assert name == "request_approval" and args["run_id"] == "C2"  # the accepter's run
    assert "cascade" not in args and "accepted" in args["summary"]
    results = {c["call_id"]: c["result"] for c in h.calls}
    assert check(args["claims"], results) == []  # the summary survives the claim check
    h.add(
        "request_approval",
        {"run_id": "C2", "summary": args["summary"]},
        {"decision": "approved", "run_id": "C2", "nurse": "N05"},
    )
    assert step(h, st) is None  # published: done


def test_cascade_timeout_then_accept() -> None:
    h = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    h.rank("R1").cascade_ask("R1").cascade_ok("R1")
    st = cascade_state()
    h.asked("S01").wait("S01", "timeout")
    assert fallback.timed_out(h.calls) == ["S01"]
    assert step(h, st)[1]["nurse_id"] == "N05"
    h.asked("N05").wait("N05", "accepted")
    name, args = step(h, st)
    assert (name, args["run_id"]) == ("request_approval", "C2")


def test_cascade_exhausted_escalates_naming_each_answer() -> None:
    h = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    h.rank("R1").cascade_ask("R1").cascade_ok("R1")
    st = cascade_state()
    h.asked("S01").wait("S01", "declined")
    h.asked("N05").wait("N05", "timeout")
    assert step(h, st)[1]["nurse_id"] == "N04"
    h.asked("N04").wait("N04", "declined")
    name, args = step(h, st)
    assert name == "escalate"
    assert args["reason"] == "3 nurses unavailable: S01 declined, N04 declined, N05 timed out"


def test_cascade_publish_directly_or_reject_ends_cleanly() -> None:
    h = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    h.rank("R1").cascade_ask("R1")
    h.calls[-1]["result"] = {"decision": "approved", "run_id": "C2", "nurse": "N05"}
    assert step(h) is None  # the charge nurse published a candidate instead: done
    h.calls[-1]["result"] = {"decision": "rejected", "run_id": "R1"}
    assert step(h) is None


def test_cascade_decided_before_the_plan_still_asks_the_nurse() -> None:
    h = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    h.rank("R1").cascade_ask("R1")
    h.calls[-1]["result"] = {
        "decision": "approved",
        "run_id": "C1",
        "nurse": "S01",
        "cascade": ORDER,
    }  # pre-answered: published before the task started
    name, args = step(h)
    assert (name, args["nurse_id"], args["run_id"]) == ("send_message", "S01", "C1")


def test_cascade_stale_ranking_ranks_again_for_a_new_plan() -> None:
    h = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    h.rank("R1").cascade_ask("R1").cascade_ok("R1")
    h.asked("S01").wait("S01", "declined")
    st = cascade_state()
    st["published_now"] = "P1"  # someone published while the first nurse was asked
    name, args = step(h, st)
    assert (name, args["exclude"]) == ("rank_replacements", ["S01"])
    h.rank("R2", CANDS[1:])
    name, args = step(h, st)
    assert (name, args["run_id"], args["cascade"]) == ("request_approval", "R2", ["N05", "N04"])


def test_cascade_off_or_no_candidates_asks_for_one_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    h.rank("R1")
    monkeypatch.setenv("AGENT_CASCADE", "0")
    get_settings.cache_clear()
    try:
        name, args = step(h)
        assert name == "request_approval" and "cascade" not in args
    finally:
        get_settings.cache_clear()
    h2 = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    h2.rank("R1", [])
    name, args = step(h2)
    assert name == "request_approval" and "cascade" not in args


def test_plain_approval_in_cascade_mode_stays_publish_first() -> None:
    """A model that never proposes a plan still gets the old flow, one approval per round."""
    h = H().add("get_open_cells", {}, {"open_cells": [CELL]})
    h.rank("R1")
    h.add(
        "request_approval",
        {"run_id": "R1", "summary": "s"},
        {"decision": "approved", "run_id": "C1", "nurse": "S01"},
    )
    name, args = step(h)
    assert (name, args["nurse_id"], args["run_id"]) == ("send_message", "S01", "C1")


# ---------------------------------------------------------------- tool guardrails


class _Env:
    async def broadcast(self, ward_id: str, msg: dict[str, Any]) -> None:
        return None


class _Gateway:
    """Drafts that fail the post-check, so the template answers (still a valid send)."""

    async def complete(self, req: Any, **_: Any) -> Any:
        return SimpleNamespace(text='{"message": "please cover"}', parsed=None)


def _ctx(conn: sqlite3.Connection, ward: Ward, **trigger: Any) -> AgentCtx:
    task = {"id": "task-1", "state": {"calls": [], **trigger}}
    return AgentCtx(conn, ward, task, _Env(), _Gateway(), DecisionLog(conn, ward.id))  # type: ignore[arg-type]


def test_request_approval_validates_the_plan(conn: sqlite3.Connection) -> None:
    plan = scenario_a(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    prop = repo.proposal_for_run(conn, plan.run_id)
    assert prop is not None
    cands = [str(c["nurse"]) for c in prop["ranking"]["candidates"]]
    ctx = _ctx(conn, ward)

    async def ask(**kw: Any) -> dict[str, Any]:
        return await request_approval(ctx, summary="s", claims=[], **kw)

    with pytest.raises(ToolError, match="not candidates"):
        asyncio.run(ask(run_id=plan.run_id, cascade=[cands[0], "N99"]))
    with pytest.raises(ToolError, match="once"):
        asyncio.run(ask(run_id=plan.run_id, cascade=[cands[0], cands[0]]))
    with pytest.raises(ToolError, match="1 to 3"):
        asyncio.run(ask(run_id=plan.run_id, cascade=[]))
    with pytest.raises(ToolError, match="1 to 3"):
        asyncio.run(ask(run_id=plan.run_id, cascade=[*cands, *cands][:4]))
    with pytest.raises(ToolError, match="goes with run_id"):
        asyncio.run(ask(request_id="R-0100", cascade=[cands[0]]))
    out = asyncio.run(ask(run_id=plan.run_id, cascade=cands[:2]))
    assert out["approval"]["cascade"] == cands[:2]
    assert out["approval"]["parent_run"] == repo.get_run(conn, plan.run_id)["parent_run"]


def test_request_approval_rejects_a_plan_when_cascades_are_off(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = scenario_a(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    prop = repo.proposal_for_run(conn, plan.run_id)
    assert prop is not None
    cands = [str(c["nurse"]) for c in prop["ranking"]["candidates"]]
    monkeypatch.setenv("AGENT_CASCADE", "0")
    get_settings.cache_clear()
    try:
        with pytest.raises(ToolError, match="disabled"):
            asyncio.run(
                request_approval(
                    _ctx(conn, ward), summary="s", claims=[], run_id=plan.run_id, cascade=cands[:2]
                )
            )
    finally:
        get_settings.cache_clear()


def test_plan_approval_keeps_the_run_out_of_approved_runs(conn: sqlite3.Connection) -> None:
    """A plan approval must not open the publish-first path, or sends skip the order check
    (caught by adv-04: the out-of-order pin was silently allowed)."""
    mini_baseline(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    ctx = _ctx(conn, ward, approved_runs=[])
    loop.apply_answer(
        ctx, {"decision": "approved", "run_id": "R1", "cascade": ORDER, "parent_run": "P0"}
    )
    assert ctx.state["approved_cascade"]["order"] == ORDER
    assert ctx.state.get("approved_runs", []) == []
    loop.apply_answer(ctx, {"decision": "approved", "run_id": "C1", "nurse": "S01"})
    assert ctx.state["approved_runs"] == ["C1"]


def _cascade_ctx(conn: sqlite3.Connection) -> tuple[AgentCtx, str, list[str], Ward]:
    plan = scenario_a(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    prop = repo.proposal_for_run(conn, plan.run_id)
    assert prop is not None
    cands = [str(c["nurse"]) for c in prop["ranking"]["candidates"]]
    run = repo.get_run(conn, plan.run_id)
    assert run is not None
    ctx = _ctx(
        conn,
        ward,
        open_cell=dict(CELL),
        approved_cascade={
            "run_id": plan.run_id,
            "order": cands[:3],
            "open_cell": dict(CELL),
            "parent_run": run["parent_run"],
        },
    )
    return ctx, plan.run_id, cands[:3], ward


def test_send_message_enforces_the_order(conn: sqlite3.Connection) -> None:
    ctx, rid, order, _ward = _cascade_ctx(conn)
    with pytest.raises(Forbidden, match="not next"):
        asyncio.run(send_message(ctx, nurse_id=order[1], kind="replacement", run_id=rid))
    with pytest.raises(Forbidden, match="not approved"):
        asyncio.run(send_message(ctx, nurse_id=order[0], kind="replacement", run_id="run-nope"))
    first = asyncio.run(send_message(ctx, nurse_id=order[0], kind="replacement", run_id=rid))
    assert first["nurse_id"] == order[0] and first["message_id"]
    ctx.state["calls"].append(
        {
            "call_id": "c1",
            "tool": "send_message",
            "mode": "agent",
            "args": {"nurse_id": order[0], "kind": "replacement"},
            "result": {"nurse_id": order[0]},
        }
    )
    with pytest.raises(Forbidden, match="still open"):
        asyncio.run(send_message(ctx, nurse_id=order[1], kind="replacement", run_id=rid))


def test_send_message_refuses_a_stale_ranking(conn: sqlite3.Connection) -> None:
    ctx, rid, order, _ward = _cascade_ctx(conn)
    ctx.state["approved_cascade"]["parent_run"] = "run-older"
    with pytest.raises(ToolError, match="changed since"):
        asyncio.run(send_message(ctx, nurse_id=order[0], kind="replacement", run_id=rid))


def test_send_message_old_path_needs_no_cascade(conn: sqlite3.Connection) -> None:
    ctx, _rid, _order, _ward = _cascade_ctx(conn)
    ctx.state.pop("approved_cascade")
    cand = repo.proposal_for_run(conn, ctx.state["open_cell"] and _rid or "")
    assert cand is not None
    target = cand["ranking"]["candidates"][0]["run_id"]
    ctx.state["approved_runs"] = [target]
    out = asyncio.run(
        send_message(
            ctx,
            nurse_id=cand["ranking"]["candidates"][0]["nurse"],
            kind="replacement",
            run_id=target,
        )
    )
    assert out["nurse_id"] == cand["ranking"]["candidates"][0]["nurse"]


def test_cascade_progress_carries_the_plan_ref() -> None:
    from app.api.routes_agent import cascade_progress

    prog = cascade_progress(
        {
            "status": "waiting_reply",
            "state": {
                "waiting": {"nurse_id": "N05"},
                "approved_cascade": {"run_id": "run-plan", "order": ORDER},
                "calls": [
                    {
                        "tool": "wait_for_reply",
                        "result": {"reply": "declined", "nurse_id": "S01"},
                    }
                ],
            },
        }
    )
    assert prog is not None
    assert prog["ref"] == "run-plan" and prog["approved"] is True
    assert prog["asked"] == {"S01": "declined", "N05": "waiting"}
    assert cascade_progress({"status": "done", "state": {}}) is None
