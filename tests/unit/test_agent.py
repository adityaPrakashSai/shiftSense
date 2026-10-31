"""Roster agent (design doc 7.10) and message writer (7.7): registry guardrails, the loop's
allowlist, approval and budget rules, the 10-scenario eval in mock mode, and the 20 recorded
drafts replayed from the committed cache."""

import ast
import asyncio
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent import loop, runtime
from app.agent.tools import ALLOWLIST, REGISTRY, ToolClass
from app.agent.writer import check, context, template, write
from app.domain.seeds import load_seed
from app.explain.narrate import health_words
from app.llm.types import LLMRequest, LLMResponse, ToolCall
from evals.run import load, run_agent, run_writer, summarize_writer

AGENT_DIR = Path(__file__).resolve().parents[2] / "app" / "agent"


# ---------------------------------------------------------------- registry


def test_registry_has_no_publish_tool() -> None:
    assert not [n for n in REGISTRY if "publish" in n]
    for t in REGISTRY.values():
        assert "publish" not in t.fn.__name__
    # no agent module calls anything that writes run.status = published
    for path in AGENT_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                assert name not in ("publish", "set_published"), (path.name, name)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "status = 'published'" not in node.value.lower(), path.name


def test_allowlists_name_registered_tools_and_classes() -> None:
    for wf, names in ALLOWLIST.items():
        assert names <= set(REGISTRY), wf
        assert "escalate" in names and "request_approval" in names, wf
    assert REGISTRY["rank_replacements"].cls is ToolClass.COMPUTE
    assert REGISTRY["send_message"].cls is ToolClass.ACTION
    assert REGISTRY["get_proposal"].cls is ToolClass.READ
    assert {n for n, t in REGISTRY.items() if t.waits} == {"wait_for_reply", "request_approval"}


# ---------------------------------------------------------------- writer


def _ctx() -> dict[str, Any]:
    ward = load_seed("mini").ward
    return context(ward, "S01", date(2026, 11, 3), "M", "replacement", {"standby": True,
                   "changed_cells": 1, "absent": "N03", "reason": "fever"})  # fmt: skip


def test_writer_context_keeps_the_absent_reason_out() -> None:
    ctx = _ctx()
    assert "absent" not in ctx["facts"] and "reason" not in ctx["facts"]
    assert ctx["date"] == "3 Nov" and ctx["shift_name"] == "Morning"


def test_writer_checks_and_template() -> None:
    ctx = _ctx()
    good = template(ctx)
    assert check(good, ctx) == [] and not health_words(good)
    assert any("health" in p for p in check(good + " Get well, the fever will pass.", ctx))
    assert any("date" in p for p in check("Can you cover Morning? Accept or Decline.", ctx))
    assert any("shift" in p for p in check("Can you cover on 3 Nov? Accept or Decline.", ctx))
    assert any("characters" in p for p in check("3 Nov Morning " + "x" * 400, ctx))
    assert health_words("Will you come?") == []  # "ill" is a whole-word match only


class FakeGateway:
    """Returns scripted responses in order; counts calls."""

    def __init__(self, replies: list[LLMResponse]) -> None:
        self.replies = replies
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, **_: Any) -> LLMResponse:
        self.requests.append(req)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def resp(text: str | None = None, calls: list[ToolCall] | None = None,
         latency_ms: int = 5) -> LLMResponse:  # fmt: skip
    return LLMResponse(text=text, tool_calls=calls or [], parsed=None, reasoning=None,
                       provider="fake", model="fake", cache_hit=False, latency_ms=latency_ms,
                       prompt_tokens=0, completion_tokens=0, cost_usd=0.0)  # fmt: skip


def test_writer_falls_back_to_template_on_a_health_word() -> None:
    ward = load_seed("mini").ward
    gw = FakeGateway([resp('{"message": "Arun, 3 Nov Morning please: Meena has bukhar."}')])
    w = asyncio.run(write(ward, nurse_id="S01", day=date(2026, 11, 3), shift="M",
                          facts={"standby": True, "changed_cells": 1}, gateway=gw))  # type: ignore[arg-type]  # fmt: skip
    assert w.source == "template" and any("health" in p for p in w.problems)
    assert "bukhar" not in w.text and "3 Nov" in w.text


def test_writer_replays_twenty_recorded_drafts_without_health_words() -> None:
    """The 20 drafts recorded live (evals/writer.jsonl) replay from data/llm_cache.jsonl."""
    cases = load("writer", None, None)
    assert len(cases) == 20
    with tempfile.TemporaryDirectory() as tmp:
        rows = asyncio.run(run_writer(cases, provider="baseten", mode="replay", max_usd=0.0,
                                      workers=4, db=Path(tmp) / "w.db"))  # fmt: skip
    s = summarize_writer(rows)
    assert s["cache_misses"] == 0, s
    assert s["final_with_health_words"] == 0 and s["final_pass"] == "20/20"
    for r in rows:
        assert not health_words(r["text"]), r["id"]


# ---------------------------------------------------------------- the eval in mock mode


def test_agent_eval_in_mock_mode() -> None:
    cases = load("agent", None, None)
    assert len(cases) == 15  # ag-01..ag-10 publish-first, ag-11..ag-15 cascade
    with tempfile.TemporaryDirectory() as tmp:
        rows = asyncio.run(run_agent(cases, provider="mock", mode="mock", max_usd=0.0, workers=1,
                                     db=Path(tmp) / "a.db"))  # fmt: skip
    for r in rows:
        assert r["pass"], (r["id"], r["reasons"])
        for mode in ("agent", "fallback"):
            assert r[mode]["forbidden"] == 0 and r[mode]["respected"]
    assert rows[2]["agent"]["escalated"]  # three declines escalate


# ---------------------------------------------------------------- loop guardrails


@pytest.fixture
def scenario_a(conn: Any) -> Any:
    from app.domain.models import Request, RequestKind, RequestStatus
    from app.store import repo
    from tests.helpers import NOW, TUE, mini_baseline

    mini_baseline(conn)
    r = Request(id="R-0100", nurse_id="N03", kind=RequestKind.UNAVAILABLE, dates=[TUE],
                shifts=["M"], hard=True, status=RequestStatus.ACTIVE, created_at=NOW)  # fmt: skip
    repo.insert_request(conn, "ward-mini", r)
    return conn


def _run(conn: Any, gw: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from app.config import get_settings
    from app.log.decision_log import DecisionLog
    from app.store import repo
    from evals.agent_eval import NOW as PINNED
    from evals.agent_eval import LocalEnv
    from tests.helpers import NOW

    monkeypatch.setenv("DEMO_NOW", NOW.isoformat())
    get_settings.cache_clear()
    PINNED[0] = NOW
    ward = repo.get_ward(conn)
    env = LocalEnv(Path(get_settings().db_path))
    log = DecisionLog(conn, ward.id)
    tid = asyncio.run(
        runtime.start(conn, ward, "sick_call", env=env, gateway=gw, log=log, request_id="R-0100")
    )
    task = repo.get_agent_task(conn, tid)
    assert task is not None
    return task


def test_forbidden_calls_are_refused_then_fallback(
    scenario_a: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.store import repo

    bad = resp("Publish it.", [ToolCall("x1", "publish_run", {"run_id": "run-1"})])
    gw = FakeGateway([bad])
    task = _run(scenario_a, gw, monkeypatch)
    c = task["state"]["counters"]
    assert c["forbidden"] == 2 and task["mode"] == "fallback"
    assert "failures in a row" in task["state"]["fallback_reason"]
    assert task["status"] == "waiting_approval"  # the fixed code carried on to the approval
    kinds = [e["kind"] for e in repo.list_events(scenario_a, "ward-mini", after_seq=0, limit=2000)]
    assert kinds.count("agent.rejected") == 2 and "agent.fallback" in kinds


def test_replacement_message_before_approval_is_refused(
    scenario_a: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.store import repo

    early = resp("Ask Arun now.", [ToolCall("x1", "send_message",
                                            {"nurse_id": "S01", "kind": "replacement",
                                             "run_id": "run-x"})])  # fmt: skip
    gw = FakeGateway([early])
    task = _run(scenario_a, gw, monkeypatch)
    c = task["state"]["counters"]
    assert c["approval_violations"] == 2 and task["mode"] == "fallback"
    assert repo.list_nurse_messages(scenario_a, "ward-mini") == []


def test_model_time_budget_switches_to_fallback(
    scenario_a: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = resp("Find the open cells.", [ToolCall("x1", "get_open_cells", {})],
                latency_ms=int(loop.MODEL_S * 1000) + 1)  # fmt: skip
    task = _run(scenario_a, FakeGateway([slow]), monkeypatch)
    assert task["mode"] == "fallback" and "model time" in task["state"]["fallback_reason"]
    assert task["status"] == "waiting_approval"
    assert [s["tool"] for s in task["state"]["trace"] if s["kind"] == "tool"][:2] == [
        "get_open_cells", "rank_replacements"]  # fmt: skip


def test_stopping_early_counts_as_a_failure(
    scenario_a: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _run(scenario_a, FakeGateway([resp("All done.")]), monkeypatch)
    assert task["mode"] == "fallback" and task["status"] == "waiting_approval"
    assert "stopped before" in task["state"]["fallback_reason"]


def test_people_quicker_than_the_agent(scenario_a: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The charge nurse approves before the agent asks, and the nurse accepts before the agent
    waits: both answers are taken at once (seen live: Approve and Accept beat a 5 s model turn)."""
    from app.agent.tools import AgentCtx, wait_for_reply
    from app.config import get_settings
    from app.log.decision_log import DecisionLog
    from app.planner.runs import publish
    from app.store import repo
    from evals.agent_eval import NOW as PINNED
    from evals.agent_eval import LocalEnv
    from tests.helpers import NOW

    conn = scenario_a
    monkeypatch.setenv("DEMO_NOW", NOW.isoformat())
    get_settings.cache_clear()
    PINNED[0] = NOW
    ward = repo.get_ward(conn)
    env = LocalEnv(Path(get_settings().db_path))
    run_id = asyncio.run(env.start_resolve(conn, ward.id, trigger="request:R-0100",
                                           affected=[NOW.date()], request_id="R-0100"))  # fmt: skip
    publish(conn, run_id, by="charge")  # approved before the task even starts
    log = DecisionLog(conn, ward.id)
    note = resp('{"message": "Arun, can you cover 3 Nov Morning? Please tap Accept or Decline."}')
    tid = asyncio.run(runtime.start(conn, ward, "sick_call", env=env, gateway=FakeGateway([note]),
                                    log=log, prefetch=run_id, mode="fallback",
                                    request_id="R-0100"))  # fmt: skip
    task = repo.get_agent_task(conn, tid)
    assert task is not None and task["status"] == "waiting_reply"
    appr = next(c for c in task["state"]["calls"] if c["tool"] == "request_approval")
    assert appr["result"]["decision"] == "approved" and run_id in task["state"]["approved_runs"]
    mid = task["state"]["waiting"]["message_id"]
    repo.set_nurse_message_status(conn, mid, "accepted")  # answered while nobody waited
    ctx = AgentCtx(conn, ward, task, env, FakeGateway([]), log)  # type: ignore[arg-type]
    got = asyncio.run(wait_for_reply(ctx, nurse_id="S01", message_id=mid))
    assert got == {"answered": {"reply": "accepted", "nurse_id": "S01", "message_id": mid}}
