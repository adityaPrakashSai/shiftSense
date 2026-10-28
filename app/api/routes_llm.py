"""GET /api/llm/usage (design doc 9.4): today's calls, cache hit rate, cost by provider, mode."""

from typing import Any

from fastapi import APIRouter

from app.api.deps import Conn
from app.api.schemas import LLMUsage
from app.config import get_settings
from app.llm.gateway import get_gateway, role_provider
from app.llm.types import Role
from app.store.repo import now_iso_ms

router = APIRouter(prefix="/api/llm", tags=["llm"])


@router.get("/usage", response_model=LLMUsage)
def usage(conn: Conn) -> LLMUsage:
    s = get_settings()
    gw = get_gateway()
    today = now_iso_ms()[:10]
    rows = conn.execute(
        "SELECT provider, model, COUNT(*) AS calls, SUM(cache_hit) AS hits, SUM(cost_usd) AS cost, "
        "SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, SUM(1 - ok) AS errors "
        "FROM llm_call WHERE ts LIKE ? GROUP BY provider, model ORDER BY provider",
        (f"{today}%",),
    ).fetchall()
    by_provider: list[dict[str, Any]] = [
        {
            "provider": r["provider"],
            "model": r["model"],
            "calls": r["calls"],
            "cache_hits": r["hits"] or 0,
            "errors": r["errors"] or 0,
            "cost_usd": round(r["cost"] or 0.0, 6),
            "prompt_tokens": r["p"] or 0,
            "completion_tokens": r["c"] or 0,
        }
        for r in rows
    ]
    calls = sum(p["calls"] for p in by_provider)
    hits = sum(p["cache_hits"] for p in by_provider)
    last = conn.execute(
        "SELECT role, provider, model, cache_hit, ok, latency_ms, ts FROM llm_call "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return LLMUsage.model_validate(
        {
            "mode": s.llm_mode,
            "default_provider": role_provider(s, Role.INTAKE),
            "configured_providers": sorted(gw.providers),
            "calls_today": calls,
            "cache_hit_rate": round(hits / calls, 3) if calls else 0.0,
            "cost_today_usd": round(sum(p["cost_usd"] for p in by_provider), 6),
            "spent_this_process_usd": round(gw.spent_usd, 6),
            "spend_cap_usd": s.llm_spend_cap_usd,
            "json_protocol_roles": sorted(r.value for r in gw.json_protocol_roles),
            "by_provider": by_provider,
            "last_call": None
            if last is None
            else {
                "role": last["role"],
                "provider": last["provider"],
                "model": last["model"],
                "cache_hit": bool(last["cache_hit"]),
                "ok": bool(last["ok"]),
                "latency_ms": last["latency_ms"],
                "ts": last["ts"],
            },
        }
    )
