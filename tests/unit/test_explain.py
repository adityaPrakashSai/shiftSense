"""Explainer agent (design doc 7.4, 11.3): grounded answers pass, wrong claims retry then fall
back, budgets hold, both tool modes work, and intake questions are routed here."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.explain import describe
from app.explain.agent import MAX_TOOL_CALLS, ExplainResult, ask
from app.explain.claims import check
from app.explain.tools import ToolContext, call
from app.intake.pipeline import handle_message
from app.llm.gateway import LLMGateway
from app.llm.types import LLMRequest, ProviderCaps, ProviderResult, ToolCall
from app.store import repo
from tests.helpers import NOW, scenario_a


class Scripted:
    """A fake Meta provider replaying the given results in order (the last one repeats)."""

    def __init__(self, *results: ProviderResult, caps: ProviderCaps | None = None) -> None:
        self.name, self.model = "baseten", "fake"
        self.caps = caps or ProviderCaps()
        self.results = list(results)
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, *, json_protocol: bool) -> ProviderResult:
        self.requests.append(req)
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return ProviderResult(**vars(r))


def gw(db: Path, provider: Scripted | None = None) -> LLMGateway:
    mode = "mock" if provider is None else "live"
    s = get_settings().model_copy(
        update={"llm_mode": mode, "db_path": str(db), "llm_provider_explain": None}
    )
    return LLMGateway(s, providers={} if provider is None else {"baseten": provider})


def run_ask(conn: sqlite3.Connection, g: LLMGateway, q: str, **kw: Any) -> ExplainResult:
    ward = repo.get_ward(conn)
    assert ward is not None
    return asyncio.run(ask(conn, ward, q, asker_id=kw.pop("asker_id", "charge"), gateway=g, **kw))


def final(answer: str, claims: list[dict[str, Any]]) -> ProviderResult:
    return ProviderResult(text=json.dumps({"answer": answer, "claims": claims, "language": "en"}))


def tool(name: str, **args: Any) -> ProviderResult:
    return ProviderResult(text=None, tool_calls=[ToolCall("t1", name, args)])


# ---------------------------------------------------------------- claim checker


def test_claim_checker() -> None:
    results = {"c1": {"candidates": [{"nurse": "S01", "changed_cells": 1}], "proven": True}}
    ok = {"text": "", "call_id": "c1", "path": "candidates.0.changed_cells", "value": 1.0}
    assert check([ok], results) == []
    bad = [
        {**ok, "value": 2},
        {**ok, "path": "candidates.3.changed_cells"},
        {**ok, "call_id": "c9"},
        {**ok, "path": "proven", "value": 1},  # a bool is not a number
    ]
    problems = check(bad, results)
    assert len(problems) == 4 and "is 1, not 2" in problems[0]


def test_describe_text_always_passes_the_check(conn: sqlite3.Connection) -> None:
    plan = scenario_a(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    ctx = ToolContext(conn, ward, plan.run_id, "charge")
    calls = [
        ("get_proposal", {}),
        ("get_run_summary", {}),
        ("compare_candidates", {"date": "2026-11-03", "shift": "M", "nurse_a": "Arun",
                                "nurse_b": "Ritu"}),
        ("get_fairness_history", {"nurse": "Priya"}),
        ("get_request_outcome", {"nurse": "N01"}),
        ("get_assignment_evidence", {"nurse": "N05", "date": "2026-11-04"}),
        ("find_nurse", {"name": "Kavya"}),
    ]  # fmt: skip
    wrapped = []
    for i, (name, args) in enumerate(calls, 1):
        res = call(ctx, name, args)
        assert "error" not in res, (name, res)
        wrapped.append({"call_id": f"c{i}", "tool": name, "result": res})
    s = describe.from_calls(wrapped, limit=50)
    assert s.claims and check(s.claims, {w["call_id"]: w["result"] for w in wrapped}) == []


def test_tool_errors_come_back_to_the_model(conn: sqlite3.Connection) -> None:
    plan = scenario_a(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    ctx = ToolContext(conn, ward, plan.run_id, "charge")
    assert "error" in call(ctx, "get_assignment_evidence", {"nurse": "Sunita", "date": "x"})
    assert "error" in call(ctx, "no_such_tool", {})
    assert "error" in call(ctx, "compare_candidates", {"date": "2026-11-03"})


# ---------------------------------------------------------------- agent


def test_mock_answer_is_grounded(conn: sqlite3.Connection, db_path: Path) -> None:
    plan = scenario_a(conn)
    r = run_ask(conn, gw(db_path), "Why Arun and not Anjali?", run_id=plan.run_id)
    assert r.grounded and not r.fallback_used and not r.problems
    assert [c["tool"] for c in r.tool_calls] == ["get_proposal"]
    assert "rest after night" in r.answer
    row = conn.execute("SELECT * FROM explanation WHERE id = ?", (r.explanation_id,)).fetchone()
    assert row["grounded"] == 1 and json.loads(row["claims_json"]) == r.claims
    kinds = [e["kind"] for e in repo.list_events(conn, "ward-mini")]
    assert kinds[-2:] == ["explain.asked", "explain.answered"]


def test_wrong_claim_retries_then_falls_back(conn: sqlite3.Connection, db_path: Path) -> None:
    plan = scenario_a(conn)
    wrong = final(
        "Arun changes 5 cells.",
        [{"text": "5 cells", "call_id": "c1", "path": "candidates.0.changed_cells", "value": 5}],
    )
    p = Scripted(tool("get_proposal"), wrong)
    r = run_ask(conn, gw(db_path, p), "Why Arun?", run_id=plan.run_id)
    assert r.retried and not r.grounded and r.fallback_used
    assert any("is 1, not 5" in x for x in r.problems)
    assert "ranked first: 1 changed cell" in r.answer  # the describe() fallback
    assert len(p.requests) == 3  # tool turn, answer, one retry


def test_retry_can_fix_a_claim(conn: sqlite3.Connection, db_path: Path) -> None:
    plan = scenario_a(conn)
    bad = final("x", [{"text": "", "call_id": "c1", "path": "open_cell.shift", "value": "N"}])
    claim = {"text": "", "call_id": "c1", "path": "open_cell.shift", "value": "M"}
    good = final("The open shift is a Morning.", [claim])
    r = run_ask(conn, gw(db_path, Scripted(tool("get_proposal"), bad, good)), "What?",
                run_id=plan.run_id)  # fmt: skip
    assert r.retried and r.grounded and not r.fallback_used and r.answer.startswith("The open")


def test_tool_budget_forces_a_final_turn(conn: sqlite3.Connection, db_path: Path) -> None:
    plan = scenario_a(conn)
    many = ProviderResult(
        text=None, tool_calls=[ToolCall(f"t{i}", "get_run_summary", {}) for i in range(4)]
    )
    p = Scripted(many, many, final("done", []))
    r = run_ask(conn, gw(db_path, p), "Tell me everything", run_id=plan.run_id)
    assert len(r.tool_calls) == MAX_TOOL_CALLS
    assert p.requests[-1].tools is None  # the last turn could not call tools
    assert r.fallback_used  # facts without claims are withheld


def test_json_protocol_mode(conn: sqlite3.Connection, db_path: Path) -> None:
    plan = scenario_a(conn)
    ok = {"answer": "Arun changes 1 cell.",
          "claims": [{"text": "1", "call_id": "c1", "path": "candidates.0.changed_cells",
                      "value": 1}], "language": "en"}  # fmt: skip
    p = Scripted(
        ProviderResult(text='{"tool": "get_proposal", "args": {}}'),
        ProviderResult(text=json.dumps({"final": ok})),
        caps=ProviderCaps(native_tools=False),
    )
    r = run_ask(conn, gw(db_path, p), "Why Arun?", run_id=plan.run_id)
    assert r.grounded and [c["tool"] for c in r.tool_calls] == ["get_proposal"]
    assert any(m["content"].startswith("Tool result:") for m in p.requests[1].messages
               if m["role"] == "user")  # fmt: skip


def test_off_topic_needs_no_tools(conn: sqlite3.Connection, db_path: Path) -> None:
    plan = scenario_a(conn)
    r = run_ask(conn, gw(db_path), "What medicine should Meena take for fever?",
                run_id=plan.run_id)  # fmt: skip
    assert r.grounded and r.tool_calls == [] and r.claims == [] and "fever" not in r.answer


def test_intake_question_goes_to_the_explainer(conn: sqlite3.Connection, db_path: Path) -> None:
    scenario_a(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    res = asyncio.run(
        handle_message(
            conn, ward, "N02", "mujhe har weekend kyun milta hai?", gateway=gw(db_path), now=NOW
        )  # fmt: skip
    )
    assert res.reply_kind == "answer" and res.explanation_id and res.claims
    assert "weekend shifts" in res.reply
    msg = repo.get_message(conn, res.message_id)
    assert msg is not None and msg["explanation_id"] == res.explanation_id
