"""LLMGateway (design doc 7.1, 9): every model call in the app goes through `complete`.

Modes (`LLM_MODE`): `live` reads and writes the cache and calls the provider on a miss; `replay`
reads only and raises CacheMiss on a miss; `mock` calls the MockProvider (pinned fixtures first)
and never touches the cache. Every call, cached or not, writes one `llm_call` row. Live calls stop
with SpendCapReached once the process has spent `LLM_SPEND_CAP_USD`. Reasoning text is stored in
`llm_call` and never returned to users.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.llm import cache
from app.llm.jsonproto import loads_json, parse_reply
from app.llm.mock import MockProvider
from app.llm.prices import cost_usd
from app.llm.providers import Provider, build_providers
from app.llm.types import (
    CacheMiss,
    LLMError,
    LLMRequest,
    LLMResponse,
    MalformedToolCall,
    ProviderError,
    ProviderResult,
    Role,
    SpendCapReached,
    ToolCall,
)
from app.store.db import connect
from app.store.repo import now_iso_ms

MALFORMED_LIMIT = 2  # native tool calls with bad arguments before a role switches to JSON protocol


def role_provider(s: Settings, role: Role) -> str:
    override = {
        Role.INTAKE: s.llm_provider_intake,
        Role.EXPLAIN: s.llm_provider_explain,
        Role.AGENT: s.llm_provider_explain,
        Role.MESSAGE: s.llm_provider_message,
        Role.NARRATE: s.llm_provider_message,
        Role.VISION: s.llm_provider_vision,
        Role.VOICE: s.llm_provider_vision,
    }.get(role)
    return override or s.llm_provider_default


class LLMGateway:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        providers: Mapping[str, Provider] | None = None,
        mock: MockProvider | None = None,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
        prompts_dir: Path = cache.PROMPTS,
    ) -> None:
        self.s = settings or get_settings()
        self.providers: dict[str, Provider] = dict(
            providers if providers is not None else build_providers(self.s)
        )
        self.mock = mock or MockProvider()
        self.conn_factory = conn_factory or (lambda: connect(self.s.db_path))
        self.prompts_dir = prompts_dir
        self.spent_usd = 0.0
        self.json_protocol_roles: set[Role] = set()
        self._malformed: dict[Role, int] = {}
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self.s.llm_mode

    def provider_for(self, role: Role) -> Provider:
        if self.mode == "mock":
            return self.mock
        name = role_provider(self.s, role)
        p = self.providers.get(name)
        if p is None:
            raise ProviderError(f"provider {name!r} is not configured (set its key in .env)")
        return p

    # ------------------------------------------------------------------ main entry

    async def complete(
        self, req: LLMRequest, *, run_id: str | None = None, request_id: str | None = None
    ) -> LLMResponse:
        t0 = time.perf_counter()
        call_id = f"llm-{uuid.uuid4().hex[:12]}"
        provider_name = self.mode if self.mode == "mock" else role_provider(self.s, req.role)
        provider: Provider | None = None
        model = "?"
        key = ""
        conn = self.conn_factory()
        try:
            provider = self.provider_for(req.role)
            model = provider.model
            key = cache.cache_key(provider.name, model, req, self.prompts_dir)

            if self.mode != "mock":
                hit = cache.get(conn, key)
                if hit is not None:
                    resp = self._from_cache(hit, req, call_id, t0)
                    self._log(conn, call_id, req, resp, key, run_id, request_id)
                    return resp
                if self.mode == "replay":
                    raise CacheMiss(f"no cached response for {req.role.value} ({key[:12]})")
                with self._lock:
                    spent, cap = self.spent_usd, self.s.llm_spend_cap_usd
                    if spent >= cap:
                        raise SpendCapReached(f"spent ${spent:.2f} of the ${cap:.2f} cap")

            pinned = self.mock.pinned_result(key) if self.mode == "mock" else None
            jp = req.role in self.json_protocol_roles
            result = pinned or await self._call(provider, req, jp)
            jp = jp or req.role in self.json_protocol_roles
            resp = self._build(result, req, provider, call_id, t0, jp)
            if self.mode == "live":
                with self._lock:
                    self.spent_usd += resp.cost_usd
                cache.put(conn, key, provider.name, model, req, self._cacheable(resp), now_iso_ms())
            self._log(conn, call_id, req, resp, key, run_id, request_id)
            return resp
        except LLMError as exc:
            self._log_error(
                conn, call_id, req, provider_name, model, key, exc, t0, run_id, request_id
            )
            raise
        finally:
            conn.close()

    async def _call(
        self, provider: Provider, req: LLMRequest, json_protocol: bool
    ) -> ProviderResult:
        """Native tools first; after MALFORMED_LIMIT bad tool calls the role uses JSON protocol."""
        while True:
            try:
                result = await provider.complete(req, json_protocol=json_protocol)
            except MalformedToolCall:
                if json_protocol:
                    raise
                n = self._malformed.get(req.role, 0) + 1
                self._malformed[req.role] = n
                if n >= MALFORMED_LIMIT:
                    self.json_protocol_roles.add(req.role)
                    json_protocol = True
                continue
            if req.tools and (json_protocol or not provider.caps.native_tools):
                if req.role not in self.json_protocol_roles and not provider.caps.native_tools:
                    self.json_protocol_roles.add(req.role)
                calls, final = parse_reply(result.text)
                result.tool_calls, result.text = calls, final
            return result

    # ------------------------------------------------------------------ helpers

    def _build(
        self,
        r: ProviderResult,
        req: LLMRequest,
        provider: Provider,
        call_id: str,
        t0: float,
        json_protocol: bool,
    ) -> LLMResponse:
        parsed = None
        if req.response_schema is not None and r.text and not r.tool_calls:
            try:
                obj = loads_json(r.text)
                parsed = obj if isinstance(obj, dict) else None
            except (ValueError, json.JSONDecodeError):
                parsed = None
        return LLMResponse(
            text=r.text,
            tool_calls=r.tool_calls,
            parsed=parsed,
            reasoning=r.reasoning,
            provider=provider.name,
            model=provider.model,
            cache_hit=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            cost_usd=cost_usd(provider.name, provider.model, r.prompt_tokens, r.completion_tokens),
            reasoning_tokens=r.reasoning_tokens,
            call_id=call_id,
            json_protocol=json_protocol,
        )

    @staticmethod
    def _cacheable(resp: LLMResponse) -> dict[str, Any]:
        d = asdict(resp)
        for k in ("cache_hit", "latency_ms", "call_id"):
            d.pop(k)
        return d

    @staticmethod
    def _from_cache(hit: dict[str, Any], req: LLMRequest, call_id: str, t0: float) -> LLMResponse:
        calls = [ToolCall(c["id"], c["name"], c["arguments"]) for c in hit.get("tool_calls", [])]
        return LLMResponse(
            text=hit.get("text"),
            tool_calls=calls,
            parsed=hit.get("parsed"),
            reasoning=hit.get("reasoning"),
            provider=hit["provider"],
            model=hit["model"],
            cache_hit=True,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            prompt_tokens=int(hit.get("prompt_tokens", 0)),
            completion_tokens=int(hit.get("completion_tokens", 0)),
            cost_usd=0.0,
            reasoning_tokens=int(hit.get("reasoning_tokens", 0)),
            call_id=call_id,
            json_protocol=bool(hit.get("json_protocol", False)),
        )

    def _log(
        self,
        conn: sqlite3.Connection,
        call_id: str,
        req: LLMRequest,
        resp: LLMResponse,
        key: str,
        run_id: str | None,
        request_id: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO llm_call(id, ts, role, provider, model, cache_key, cache_hit, "
            "prompt_tokens, completion_tokens, cost_usd, latency_ms, ok, error, run_id, "
            "request_id, reasoning, reasoning_tokens) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, "
            "NULL, ?, ?, ?, ?)",
            (
                call_id,
                now_iso_ms(),
                req.role.value,
                resp.provider,
                resp.model,
                key,
                int(resp.cache_hit),
                resp.prompt_tokens,
                resp.completion_tokens,
                round(resp.cost_usd, 6),
                resp.latency_ms,
                run_id,
                request_id,
                resp.reasoning,
                resp.reasoning_tokens,
            ),
        )
        conn.commit()

    @staticmethod
    def _log_error(
        conn: sqlite3.Connection,
        call_id: str,
        req: LLMRequest,
        provider: str,
        model: str,
        key: str,
        exc: Exception,
        t0: float,
        run_id: str | None,
        request_id: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO llm_call(id, ts, role, provider, model, cache_key, cache_hit, "
            "prompt_tokens, completion_tokens, cost_usd, latency_ms, ok, error, run_id, "
            "request_id) VALUES(?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, 0, ?, ?, ?)",
            (
                call_id,
                now_iso_ms(),
                req.role.value,
                provider,
                model,
                key,
                int((time.perf_counter() - t0) * 1000),
                f"{type(exc).__name__}: {exc}",
                run_id,
                request_id,
            ),
        )
        conn.commit()


_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    """Process-wide gateway (the spend cap is per process lifetime)."""
    global _gateway
    s = get_settings()
    if _gateway is None or _gateway.s is not s:
        _gateway = LLMGateway(s)
    return _gateway
