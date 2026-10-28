"""Explainer agent loop (design doc 7.4): tools in, grounded JSON answer out.

Budgets: at most 6 tool calls, 3 model turns after the first, 20 s wall time. Works with native
tool calls and with the JSON tool protocol (the gateway reports which one answered). The final
answer is {"answer", "claims", "language"}; every claim is checked against the tool result it
cites (claims.py). One retry with the list of problems; if it still fails, or a budget runs out,
the answer is withheld and a deterministic `describe()` text from the same tool results is shown
instead ("model answer withheld: unverified claims").
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from app.domain.edits import CHARGE_ID
from app.domain.models import Ward
from app.explain import describe
from app.explain.claims import check
from app.explain.tools import SPECS, ToolContext, call
from app.llm.cache import PROMPTS
from app.llm.gateway import LLMGateway
from app.llm.jsonproto import loads_json
from app.llm.types import CacheMiss, LLMError, LLMRequest, LLMResponse, Role
from app.log.decision_log import DecisionLog
from app.store import repo

MAX_TOOL_CALLS = 6
MAX_TURNS = 4  # the first model turn and 3 more
WALL_S = 20.0
FINAL_TURN_S = 8.0  # with less time left than this, the next turn must be the answer


class Claim(BaseModel):
    text: str = ""
    call_id: str
    path: str
    value: Any = None


class Answer(BaseModel):
    answer: str
    claims: list[Claim] = []
    language: str = "en"


@dataclass
class ExplainResult:
    explanation_id: str
    answer: str
    claims: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]  # {"call_id", "tool", "args", "result"}
    grounded: bool  # the model's own answer passed the claim check (before any fallback)
    fallback_used: bool
    problems: list[str] = field(default_factory=list)
    language: str = "en"
    retried: bool = False
    model_turns: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def build_context(
    conn: sqlite3.Connection,
    ward: Ward,
    asker_id: str,
    run_id: str,
    cell: dict[str, Any] | None,
    today: str | None,
) -> dict[str, Any]:
    asker = next((n for n in ward.nurses if n.id == asker_id), None)
    opened = repo.list_proposals(conn, ward.id, open_only=True)
    prop = repo.proposal_for_run(conn, run_id)
    return {
        "asker": {
            "id": asker_id,
            "name": "Charge nurse"
            if asker_id == CHARGE_ID
            else (asker.name if asker else asker_id),
            "role": "charge" if asker_id == CHARGE_ID else "nurse",
        },
        "today": today,
        "run_id": run_id,
        "run_status": (repo.get_run(conn, run_id) or {}).get("status"),
        "proposal_run_id": prop["run_id"] if prop else (opened[0]["run_id"] if opened else None),
        "cell": cell,
        "roster_period": f"{ward.horizon_start.isoformat()} ({ward.horizon_days} days)",
        "nurses": [f"{n.id} | {n.name}" for n in ward.nurses],
    }


async def _turn(
    gateway: LLMGateway, messages: list[dict[str, Any]], last: bool, run_id: str
) -> LLMResponse:
    """One model turn; the last one has no tools and asks for the final answer."""
    msgs = messages + ([{"role": "user", "content": prompt("explain_final")}] if last else [])
    req = LLMRequest(Role.EXPLAIN, msgs, tools=None if last else SPECS)
    return await gateway.complete(req, run_id=run_id)


def _parse(text: str | None) -> tuple[Answer | None, list[str]]:
    try:
        return Answer.model_validate(loads_json(text)), []
    except (ValueError, json.JSONDecodeError, ValidationError) as exc:
        return None, [f"the final answer is not the required JSON object: {str(exc)[:200]}"]


async def ask(
    conn: sqlite3.Connection,
    ward: Ward,
    question: str,
    *,
    asker_id: str,
    gateway: LLMGateway,
    run_id: str | None = None,
    cell: dict[str, Any] | None = None,
    today: str | None = None,
    log: DecisionLog | None = None,
) -> ExplainResult:
    t0 = time.perf_counter()
    log = log or DecisionLog(conn, ward.id)
    if run_id is None:
        pub = repo.get_published_run(conn, ward.id)
        run_id = None if pub is None else str(pub["run_id"])
    if run_id is None:
        raise LookupError("no run to explain")
    log.emit("explain.asked", f"user:{asker_id}", run_id, None, question=question, cell=cell)
    ctx = ToolContext(conn, ward, run_id, asker_id)
    context = build_context(conn, ward, asker_id, run_id, cell, today)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt("explain")},
        {
            "role": "user",
            "content": f"<context>{json.dumps(context, ensure_ascii=False)}</context>\n\n"
            f"<question>{question.strip()}</question>",
        },
    ]
    calls: list[dict[str, Any]] = []
    responses: list[LLMResponse] = []
    final: str | None = None
    turns = 0
    out_of_time = False
    try:
        while turns < MAX_TURNS:
            if time.perf_counter() - t0 > WALL_S:
                out_of_time = True
                break
            late = time.perf_counter() - t0 > WALL_S - FINAL_TURN_S
            last = turns == MAX_TURNS - 1 or len(calls) >= MAX_TOOL_CALLS or late
            try:
                resp = await _turn(gateway, messages, last, run_id)
            except CacheMiss:
                # replay: the live run may have hit the time rule and asked for the answer here
                if last:
                    raise
                last = True
                resp = await _turn(gateway, messages, last, run_id)
            responses.append(resp)
            turns += 1
            if not resp.tool_calls or last:
                final = resp.text
                break
            room = MAX_TOOL_CALLS - len(calls)
            todo = resp.tool_calls[:room]
            if resp.json_protocol:
                tc = todo[0]
                messages.append(
                    {"role": "assistant",
                     "content": json.dumps({"tool": tc.name, "args": tc.arguments}, sort_keys=True)}
                )  # fmt: skip
            else:
                messages.append(
                    {
                        "role": "assistant",
                        "content": resp.text or "",
                        "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.name,
                                          "arguments": json.dumps(tc.arguments, sort_keys=True)}}
                            for tc in todo
                        ],
                    }
                )  # fmt: skip
            for tc in todo[:1] if resp.json_protocol else todo:
                cid = f"c{len(calls) + 1}"
                result = call(ctx, tc.name, tc.arguments)
                wrapped = {"call_id": cid, "tool": tc.name, "result": result}
                calls.append({**wrapped, "args": tc.arguments})
                body = json.dumps(wrapped, ensure_ascii=False, default=str)
                if resp.json_protocol:
                    messages.append({"role": "user", "content": f"Tool result:\n{body}"})
                else:
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": body})
    except LLMError as exc:
        final = None
        problems = [f"model call failed: {exc}"]
    else:
        problems = []

    results = {c["call_id"]: c["result"] for c in calls}
    answer, parse_problems = (None, ["no final answer within the budget"]) if (
        out_of_time or final is None
    ) else _parse(final)  # fmt: skip
    problems += parse_problems
    if answer is not None:
        problems += check([c.model_dump() for c in answer.claims], results)
        if calls and not answer.claims:
            problems.append("the answer states facts but cites no tool result")

    retried = False
    if problems and answer is not None and not out_of_time and time.perf_counter() - t0 < WALL_S:
        retried = True
        retry = messages + [
            {"role": "assistant", "content": final or ""},
            {"role": "user", "content": prompt("explain_retry").replace(
                "{problems}", "\n".join(problems))},
        ]  # fmt: skip
        try:
            resp = await gateway.complete(LLMRequest(Role.EXPLAIN, retry), run_id=run_id)
            responses.append(resp)
            answer, problems = _parse(resp.text)
            if answer is not None:
                problems += check([c.model_dump() for c in answer.claims], results)
                if calls and not answer.claims:
                    problems.append("the answer states facts but cites no tool result")
        except LLMError as exc:
            answer, problems = None, [f"model call failed: {exc}"]

    grounded = answer is not None and not problems
    if grounded:
        assert answer is not None
        text, claims, lang = answer.answer, [c.model_dump() for c in answer.claims], answer.language
    else:
        fb = describe.from_calls(calls)
        text, claims = fb.text, fb.claims
        lang = answer.language if answer is not None else "en"
    res = ExplainResult(
        explanation_id=f"exp-{uuid.uuid4().hex[:10]}",
        answer=text,
        claims=claims,
        tool_calls=calls,
        grounded=grounded,
        fallback_used=not grounded,
        problems=problems,
        language=lang,
        retried=retried,
        model_turns=len(responses),
        latency_ms=int((time.perf_counter() - t0) * 1000),
        cost_usd=sum(r.cost_usd for r in responses),
    )
    _store(conn, run_id, question, asker_id, res)
    log.emit(
        "explain.answered", "llm:explain", run_id, None,
        explanation_id=res.explanation_id, answer=res.answer,
        tool_calls=[c["tool"] for c in calls], grounded=res.grounded,
        fallback_used=res.fallback_used, problems=res.problems,
    )  # fmt: skip
    return res


def _store(
    conn: sqlite3.Connection, run_id: str, question: str, asker_id: str, r: ExplainResult
) -> None:
    conn.execute(
        "INSERT INTO explanation(id, run_id, question, answer, claims_json, tool_calls_json, "
        "grounded, problems_json, created_at, asker_id, fallback_used, latency_ms, language) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            r.explanation_id,
            run_id,
            question,
            r.answer,
            json.dumps(r.claims, ensure_ascii=False, default=str),
            json.dumps(r.tool_calls, ensure_ascii=False, default=str),
            int(r.grounded),
            json.dumps(r.problems, ensure_ascii=False),
            datetime.now().astimezone().isoformat(),
            asker_id,
            int(r.fallback_used),
            r.latency_ms,
            r.language,
        ),
    )
    conn.commit()


def get_explanation(conn: sqlite3.Connection, explanation_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM explanation WHERE id = ?", (explanation_id,)).fetchone()
    if row is None:
        return None
    return {
        "explanation_id": row["id"],
        "run_id": row["run_id"],
        "answer": row["answer"],
        "claims": json.loads(row["claims_json"] or "[]"),
        "tool_calls": json.loads(row["tool_calls_json"] or "[]"),
        "grounded": bool(row["grounded"]),
        "fallback_used": bool(row["fallback_used"]),
        "problems": json.loads(row["problems_json"] or "[]"),
        "language": row["language"] or "en",
    }
