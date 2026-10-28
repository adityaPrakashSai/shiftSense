"""GET /api/llm/usage and the LLM error envelope."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.errors import llm_error_handler
from app.llm.gateway import get_gateway
from app.llm.types import LLMRequest, Role, SpendCapReached
from app.main import app


def test_usage_reports_mode_calls_and_cap(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEED", "mini")
    with TestClient(app) as client:
        before = client.get("/api/llm/usage").json()
        assert before["mode"] == "mock" and before["calls_today"] == 0
        asyncio.run(
            get_gateway().complete(
                LLMRequest(Role.INTAKE, [{"role": "user", "content": "Kal mujhe off chahiye"}])
            )
        )
        after = client.get("/api/llm/usage").json()
        assert after["calls_today"] == 1 and after["cache_hit_rate"] == 0.0
        assert after["by_provider"][0]["provider"] == "mock"
        assert after["last_call"]["role"] == "intake" and after["spend_cap_usd"] == 5.0


def test_spend_cap_maps_to_503_envelope() -> None:
    resp = asyncio.run(llm_error_handler(None, SpendCapReached("spent $5.00")))  # type: ignore[arg-type]
    assert resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"]["code"] == "LLM_SPEND_CAP"
