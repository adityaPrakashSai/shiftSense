"""Mock roster agent (design doc 9.2): what the MockProvider answers for Role.AGENT.

It rebuilds the task's history from the chat (the context block and every tool result, which
carries its call id, tool, args and result) and asks the fixed-code workflow (`fallback.next_call`)
for the next call, with a one-line plan. When nothing is left it answers with a closing sentence,
so mock mode runs the agent path end to end.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent import fallback
from app.llm.types import LLMRequest, ProviderResult, ToolCall

_CTX = re.compile(r"<context>(.*?)</context>", re.DOTALL)


def history(req: LLMRequest) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ctx: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    for m in req.messages:
        c = m.get("content")
        if not isinstance(c, str):
            continue
        if m.get("role") == "user" and not ctx and (hit := _CTX.search(c)):
            ctx = json.loads(hit.group(1))
        elif m.get("role") == "tool":
            calls.append(json.loads(c))
        elif m.get("role") == "user" and c.startswith("Tool result:"):
            calls.append(json.loads(c.split("\n", 1)[1]))
    return ctx, calls


def respond(req: LLMRequest) -> ProviderResult:
    ctx, calls = history(req)
    state = dict(ctx.get("trigger") or {})
    step = fallback.next_call(str(ctx.get("workflow")), calls, state)
    if step is None:
        return ProviderResult(text="The workflow is finished.")
    name, args = step
    args = {k: v for k, v in args.items() if v is not None}
    plan = fallback.PLAN.get(name, "")
    return ProviderResult(text=plan, tool_calls=[ToolCall(f"a{len(calls) + 1}", name, args)])
