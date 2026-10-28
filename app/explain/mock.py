"""Mock explainer (design doc 9.2): what the MockProvider answers for Role.EXPLAIN.

It calls the real tools in a fixed order chosen from question keywords ("why X not Y" ->
get_proposal, then compare_candidates if Y is not in the proposal; weekends / nights ->
get_fairness_history; requests -> get_request_outcome; a clicked cell ->
get_assignment_evidence; else get_run_summary), then builds the answer and claims from
`describe()`, so the grounding check passes.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.explain import describe
from app.llm.types import LLMRequest, ProviderResult, ToolCall

_CTX = re.compile(r"<context>(.*?)</context>", re.DOTALL)
_Q = re.compile(r"<question>(.*?)</question>", re.DOTALL)
OFF_TOPIC = ["medicine", "dawai", "dawa", "tablet", "fever", "bukhar", "doctor", "weather"]
COMPARE = [" not ", "instead", "jagah", "nahi", "rather", " over ", "kyun"]


def _context(req: LLMRequest) -> tuple[dict[str, Any], str]:
    for m in req.messages:
        c = m.get("content")
        if m.get("role") == "user" and isinstance(c, str) and (hit := _CTX.search(c)):
            q = _Q.search(c)
            return json.loads(hit.group(1)), (q.group(1).strip() if q else "")
    return {}, ""


def _results(req: LLMRequest) -> list[dict[str, Any]]:
    out = []
    for m in req.messages:
        c = m.get("content")
        if not isinstance(c, str):
            continue
        if m.get("role") == "tool":
            out.append(json.loads(c))
        elif m.get("role") == "user" and c.startswith("Tool result:"):
            out.append(json.loads(c.split("\n", 1)[1]))
    return out


def _names(question: str, ctx: dict[str, Any]) -> list[str]:
    low = f" {question.lower()} "
    found = []
    for row in ctx.get("nurses", []):
        nid, _, full = (p.strip() for p in str(row).partition("|"))
        first = full.split()[0].lower() if full else ""
        for key in (full.lower(), first, nid.lower()):
            if key and (pos := low.find(f" {key}")) >= 0:
                found.append((pos, nid))
                break
    return [n for _p, n in sorted(found)]


def _plan(
    question: str, ctx: dict[str, Any], done: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]] | None:
    low = f" {question.lower()} "
    names = _names(question, ctx)
    asker = ctx.get("asker", {})
    me = asker.get("id") if asker.get("role") == "nurse" else None
    tools = [d["tool"] for d in done]
    if len(names) >= 2 and any(w in low for w in COMPARE):
        a, b = names[0], names[1]
        if "get_proposal" not in tools and ctx.get("proposal_run_id"):
            return "get_proposal", {"run_id": ctx["proposal_run_id"]}
        prop = next((d["result"] for d in done if d["tool"] == "get_proposal"), None)
        listed = {c["nurse"] for c in (prop or {}).get("candidates", [])} | {
            e["nurse"] for e in (prop or {}).get("excluded", [])
        }
        cell = (prop or {}).get("open_cell") or ctx.get("cell")
        if b not in listed and cell and "compare_candidates" not in tools:
            day = cell.get("day")
            shift = cell.get("shift")
            return "compare_candidates", {"date": day, "shift": shift, "nurse_a": a, "nurse_b": b}
        return None
    if not tools:
        if any(w in low for w in ["weekend", "night", "raat", "always", "har "]):
            return "get_fairness_history", {"nurse": names[0] if names else me}
        wants = ["request", "denied", "day off", " off", "leave", "mila", "granted"]
        if any(w in low for w in wants) and (names or me):
            return "get_request_outcome", {"nurse": names[0] if names else me}
        cell = ctx.get("cell")
        if cell:
            return "get_assignment_evidence", {
                "nurse": cell.get("nurse_id"),
                "date": cell.get("day"),
            }
        if len(names) == 1 and ctx.get("proposal_run_id"):
            return "get_proposal", {"run_id": ctx["proposal_run_id"]}
        return "get_run_summary", {}
    return None


def respond(req: LLMRequest) -> ProviderResult:
    ctx, question = _context(req)
    done = _results(req)
    low = question.lower()
    if any(w in low for w in OFF_TOPIC):
        body: dict[str, Any] = {
            "answer": "I can only explain the roster.",
            "claims": [],
            "language": "en",
        }
        return ProviderResult(text=json.dumps(body))
    if req.tools:
        step = _plan(question, ctx, done)
        if step is not None:
            name, args = step
            args = {k: v for k, v in args.items() if v is not None}
            return ProviderResult(text=None, tool_calls=[ToolCall(f"m{len(done) + 1}", name, args)])
    names = _names(question, ctx)
    about = names[1] if len(names) >= 2 else (names[0] if names else None)
    s = describe.from_calls(done, about)
    body = {"answer": s.text, "claims": s.claims, "language": "en"}
    return ProviderResult(text=json.dumps(body, ensure_ascii=False, default=str))
