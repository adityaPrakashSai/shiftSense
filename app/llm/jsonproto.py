"""JSON protocol mode for tools (design doc 7.1): used when a provider has no native tool calls,
or a native call returned malformed arguments twice. The tool specs go into the system prompt and
the model must reply with exactly one JSON object: {"tool": name, "args": {...}} or
{"final": {...}}. Both modes produce the same ToolCall objects."""

import json
import re
from pathlib import Path
from typing import Any

from app.llm.types import MalformedToolCall, ToolCall

PROMPTS = Path(__file__).with_name("prompts")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def strip_reasoning(text: str | None) -> tuple[str | None, str | None]:
    """Split inline <think>...</think> reasoning from the final answer."""
    if text is None:
        return None, None
    thoughts = [m.group(0)[7:-8].strip() for m in _THINK.finditer(text)]
    answer = _THINK.sub("", text).strip()
    if "</think>" in answer:  # an opening tag the server already stripped
        head, _, answer = answer.partition("</think>")
        thoughts.append(head.strip())
        answer = answer.strip()
    return answer or None, ("\n".join(t for t in thoughts if t) or None)


def loads_json(text: str | None) -> Any:
    """Parse a JSON answer, tolerating a ```json fence or prose around one object."""
    if text is None:
        raise ValueError("empty answer")
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start : end + 1])
        raise


def tools_prompt(tools: list[dict[str, Any]]) -> str:
    specs = [
        {
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "parameters": t["function"].get("parameters", {}),
        }
        for t in tools
    ]
    template = (PROMPTS / "json_protocol.md").read_text(encoding="utf-8")
    return template.replace("{tools}", json.dumps(specs, ensure_ascii=False, indent=1))


def schema_prompt(schema: dict[str, Any]) -> str:
    template = (PROMPTS / "json_schema.md").read_text(encoding="utf-8")
    return template.replace("{schema}", json.dumps(schema, ensure_ascii=False))


def with_tools_prompt(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [{"role": "system", "content": tools_prompt(tools)}, *messages]


def parse_reply(text: str | None, call_no: int = 1) -> tuple[list[ToolCall], str | None]:
    """-> (tool calls, final text). Raises MalformedToolCall if the reply follows neither form."""
    try:
        obj = loads_json(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise MalformedToolCall(f"JSON protocol reply is not JSON: {exc}") from exc
    if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
        args = obj.get("args", {})
        if not isinstance(args, dict):
            raise MalformedToolCall("JSON protocol args must be an object")
        return [ToolCall(f"jp-{call_no}", obj["tool"], args)], None
    if isinstance(obj, dict) and "final" in obj:
        final = obj["final"]
        return [], final if isinstance(final, str) else json.dumps(final, ensure_ascii=False)
    raise MalformedToolCall('reply must be {"tool": ...} or {"final": ...}')
