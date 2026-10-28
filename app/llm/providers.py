"""Meta model providers (design doc 7.1, 7.9).

`OpenAICompatProvider` talks to any OpenAI-compatible chat-completions endpoint that serves a Meta
model: Baseten (Muse Spark Open, `meta-models/muse-spark-open-1026`), the Meta Model API (Muse
Spark) and an optional Modal endpoint. The `openai` Python SDK is used only as the HTTP client for
those endpoints; no OpenAI model is ever called. The only non-Meta provider is the mock (mock.py).
Rule 5 of 11.4: never add another provider here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

import openai
from openai import AsyncOpenAI

from app.config import Settings
from app.llm.jsonproto import schema_prompt, strip_reasoning, with_tools_prompt
from app.llm.types import (
    LLMRequest,
    MalformedToolCall,
    ProviderCaps,
    ProviderError,
    ProviderResult,
    ToolCall,
)

TIMEOUT_S = 30.0
RETRIES = 2  # on 429, 5xx and timeouts; never on other 4xx
BACKOFF_S = 0.5


class Provider(Protocol):
    name: str
    model: str
    caps: ProviderCaps

    async def complete(self, req: LLMRequest, *, json_protocol: bool) -> ProviderResult: ...


def retryable(exc: Exception) -> bool:
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


@dataclass
class OpenAICompatProvider:
    name: str
    base_url: str
    api_key: str
    model: str
    caps: ProviderCaps
    client: Any = None  # AsyncOpenAI; tests inject a fake

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = AsyncOpenAI(
                base_url=self.base_url, api_key=self.api_key, timeout=TIMEOUT_S, max_retries=0
            )

    def _kwargs(self, req: LLMRequest, json_protocol: bool) -> dict[str, Any]:
        messages = req.messages
        kw: dict[str, Any] = {
            "model": self.model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.tools and (json_protocol or not self.caps.native_tools):
            messages = with_tools_prompt(messages, req.tools)
        elif req.tools:
            kw["tools"] = req.tools
        if req.response_schema is not None and not (req.tools and json_protocol):
            # the schema always goes in the prompt too: with strict decoding alone, Muse Spark Open
            # reasons about a format it cannot see and the grammar then forces a wrong answer
            messages = [
                {"role": "system", "content": schema_prompt(req.response_schema)},
                *messages,
            ]
            if self.caps.json_schema:
                kw["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": f"{req.role.value}_output",
                        "schema": req.response_schema,
                        "strict": True,
                    },
                }
            else:
                kw["response_format"] = {"type": "json_object"}
        kw["messages"] = messages
        return kw

    async def _create(self, kw: dict[str, Any]) -> Any:
        for attempt in range(RETRIES + 1):
            try:
                return await self.client.chat.completions.create(**kw)
            except Exception as exc:
                if attempt == RETRIES or not retryable(exc):
                    raise ProviderError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
                await asyncio.sleep(BACKOFF_S * 2**attempt)
        raise AssertionError("unreachable")

    async def complete(self, req: LLMRequest, *, json_protocol: bool) -> ProviderResult:
        resp = await self._create(self._kwargs(req, json_protocol))
        msg = resp.choices[0].message
        extra = getattr(msg, "model_extra", None) or {}
        reasoning, field = None, None
        for key in ("reasoning_content", "reasoning"):
            if extra.get(key):
                reasoning, field = str(extra[key]), key
                break
        text, inline = strip_reasoning(msg.content)
        if inline:
            reasoning, field = (
                (reasoning + "\n" + inline) if reasoning else inline,
                field or "content<think>",
            )

        calls = []
        for i, tc in enumerate(getattr(msg, "tool_calls", None) or [], 1):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise MalformedToolCall(f"tool {tc.function.name}: arguments are not JSON") from exc
            if not isinstance(args, dict):
                raise MalformedToolCall(f"tool {tc.function.name}: arguments are not an object")
            calls.append(ToolCall(tc.id or f"c{i}", tc.function.name, args))

        usage = getattr(resp, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        return ProviderResult(
            text=text,
            tool_calls=calls,
            reasoning=reasoning,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            reasoning_tokens=int(getattr(details, "reasoning_tokens", 0) or 0),
            reasoning_field=field,
        )


def build_providers(s: Settings) -> dict[str, OpenAICompatProvider]:
    """The configured Meta providers. Endpoints and model ids come from the environment."""
    out: dict[str, OpenAICompatProvider] = {}
    if s.baseten_api_key:
        out["baseten"] = OpenAICompatProvider(
            "baseten",
            s.baseten_base_url,
            s.baseten_api_key,
            s.baseten_model,
            # strict json_schema works on toy schemas but gave wrong ConstraintEdit parses and one
            # run-away answer in M6 probes; json_object + the schema in the prompt is reliable
            ProviderCaps(native_tools=True, json_schema=False, vision=True, audio=True),
        )
    if s.meta_api_key and s.meta_base_url and s.meta_model:
        out["meta"] = OpenAICompatProvider(
            "meta",
            s.meta_base_url,
            s.meta_api_key,
            s.meta_model,
            ProviderCaps(native_tools=True, json_schema=True, vision=True, audio=False),
        )
    if s.modal_api_key and s.modal_base_url and s.modal_model:
        out["modal"] = OpenAICompatProvider(
            "modal",
            s.modal_base_url,
            s.modal_api_key,
            s.modal_model,
            ProviderCaps(native_tools=False, json_schema=False),
        )
    return out
