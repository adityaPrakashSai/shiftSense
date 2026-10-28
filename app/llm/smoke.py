"""Smoke test against a live Meta provider (design doc 7.1, M5; answers Q3/Q4).

    uv run python -m app.llm.smoke --provider baseten

Sends one structured-output request and one tool-call request, falls back to the JSON protocol if
native tools fail, reports which capabilities worked and which response field carried the
reasoning, then repeats a request through the gateway to show the cache hit. Record the result in
docs/decisions.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from typing import Any

from app.config import get_settings
from app.llm.cache import PROMPTS
from app.llm.gateway import LLMGateway
from app.llm.jsonproto import loads_json, parse_reply
from app.llm.providers import OpenAICompatProvider, build_providers
from app.llm.types import LLMError, LLMRequest, ProviderCaps, Role
from app.store.db import open_db

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "shifts": {"type": "integer"}},
    "required": ["answer", "shifts"],
    "additionalProperties": False,
}
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_shift",
            "description": "Look up the shift a nurse works on a date.",
            "parameters": {
                "type": "object",
                "properties": {"nurse": {"type": "string"}, "date": {"type": "string"}},
                "required": ["nurse", "date"],
            },
        },
    }
]


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


async def run(provider: OpenAICompatProvider) -> dict[str, Any]:
    report: dict[str, Any] = {"provider": provider.name, "model": provider.model}
    structured = LLMRequest(
        Role.INTAKE,
        [{"role": "user", "content": prompt("smoke_structured")}],
        response_schema=SCHEMA,
    )
    t0 = time.perf_counter()
    try:
        r = await provider.complete(structured, json_protocol=False)
        obj = loads_json(r.text)
        report["json_schema"] = {
            "ok": isinstance(obj, dict) and obj.get("answer") == "ready",
            "answer": obj,
        }
    except Exception as exc:  # noqa: BLE001 - every failure is a finding to record
        report["json_schema"] = {"ok": False, "error": str(exc)[:300]}
        plain = replace(provider, caps=ProviderCaps(native_tools=True, json_schema=False))
        try:
            r = await plain.complete(structured, json_protocol=False)
            report["json_object_fallback"] = {"ok": True, "answer": loads_json(r.text)}
        except Exception as exc2:  # noqa: BLE001
            report["json_object_fallback"] = {"ok": False, "error": str(exc2)[:300]}
            r = None
    report["structured_latency_ms"] = int((time.perf_counter() - t0) * 1000)
    if r is not None:
        report["reasoning_field"] = r.reasoning_field
        report["reasoning_chars"] = len(r.reasoning or "")
        report["tokens"] = {
            "prompt": r.prompt_tokens,
            "completion": r.completion_tokens,
            "reasoning": r.reasoning_tokens,
        }

    tool_req = LLMRequest(
        Role.EXPLAIN, [{"role": "user", "content": prompt("smoke_tools")}], tools=TOOLS
    )
    try:
        r2 = await provider.complete(tool_req, json_protocol=False)
        call = r2.tool_calls[0] if r2.tool_calls else None
        report["native_tools"] = {
            "ok": call is not None
            and call.name == "get_shift"
            and call.arguments.get("nurse") == "N07",
            "call": None if call is None else {"name": call.name, "args": call.arguments},
            "text": (r2.text or "")[:200],
        }
    except Exception as exc:  # noqa: BLE001
        report["native_tools"] = {"ok": False, "error": str(exc)[:300]}
    if not report["native_tools"]["ok"]:
        try:
            r3 = await provider.complete(tool_req, json_protocol=True)
            calls, final = parse_reply(r3.text)
            report["json_protocol_tools"] = {
                "ok": bool(calls) and calls[0].name == "get_shift",
                "call": None if not calls else {"name": calls[0].name, "args": calls[0].arguments},
                "final": final,
            }
        except Exception as exc:  # noqa: BLE001
            report["json_protocol_tools"] = {"ok": False, "error": str(exc)[:300]}
    return report


async def cache_check(provider: OpenAICompatProvider, db: str) -> dict[str, Any]:
    """The same request twice through the gateway: one live call, then a cache hit."""
    s = get_settings().model_copy(
        update={"llm_mode": "live", "db_path": db, "llm_provider_intake": provider.name}
    )
    open_db(db).close()
    gw = LLMGateway(s, providers={provider.name: provider})
    req = LLMRequest(
        Role.INTAKE,
        [{"role": "user", "content": prompt("smoke_structured")}],
        response_schema=SCHEMA,
    )
    first = await gw.complete(req)
    second = await gw.complete(req)
    return {
        "first": {
            "cache_hit": first.cache_hit,
            "latency_ms": first.latency_ms,
            "cost_usd": first.cost_usd,
        },
        "second": {"cache_hit": second.cache_hit, "latency_ms": second.latency_ms},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Smoke-test a live Meta model provider.")
    ap.add_argument("--provider", default="baseten", choices=["baseten", "meta", "modal"])
    ap.add_argument("--db", default=get_settings().db_path)
    a = ap.parse_args(argv)
    providers = build_providers(get_settings())
    provider = providers.get(a.provider)
    if provider is None:
        print(f"{a.provider} is not configured: set its API key (and base URL / model) in .env")
        return 2
    try:
        report = asyncio.run(run(provider))
        report["cache"] = asyncio.run(cache_check(provider, a.db))
    except LLMError as exc:
        print(f"failed: {exc}")
        return 1
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
