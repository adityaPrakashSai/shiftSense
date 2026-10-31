"""Ask the agent: cover goals get suggestions with claim chips, impact goals analyse a
request, change goals are refused with zero tool calls, and nothing is ever written
(no messages, no runs, no approvals)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.explain import claims as claimcheck
from app.main import app


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    with TestClient(app) as c:
        yield c


def versions(client: TestClient) -> list[Any]:
    return list(client.get("/api/versions").json()["versions"])


def test_cover_goal_answers_with_chips_and_changes_nothing(client: TestClient) -> None:
    before = versions(client)
    r = client.post("/api/agent/command", json={"goal": "tonight looks thin"})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out["status"] == "done"
    assert "shortfall" in out["answer"]
    assert [c["tool"] for c in out["tool_calls"]] == ["staffing_check"]
    results = {c["call_id"]: c["result"] for c in out["tool_calls"]}
    assert out["claims"] and claimcheck.check(out["claims"], results) == []
    assert client.get("/api/inbox").json() == []
    assert versions(client) == before
    assert client.get("/api/agent/tasks").json()[0]["outcome"] == out["answer"]


def test_publish_goal_is_refused_without_tool_calls(client: TestClient) -> None:
    before = versions(client)
    r = client.post("/api/agent/command", json={"goal": "publish the roster now"})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out["status"] == "done" and out["tool_calls"] == [] and out["claims"] == []
    assert "can't publish" in out["answer"]
    assert client.get("/api/inbox").json() == []
    assert versions(client) == before


def test_impact_goal_analyses_the_request(client: TestClient) -> None:
    body = client.post(
        "/api/messages", data={"sender_id": "N05", "text": "I need leave on Thursday"}
    ).json()
    assert body["reply_kind"] == "confirm", body
    rid = body["request_id"]
    out = client.post(
        "/api/agent/command", json={"goal": f"show the impact if {rid} goes ahead"}
    ).json()
    assert "Granting" in out["answer"] and "leave" in out["answer"]
    assert [c["tool"] for c in out["tool_calls"]] == ["what_if"]
    results = {c["call_id"]: c["result"] for c in out["tool_calls"]}
    assert claimcheck.check(out["claims"], results) == []


def test_empty_goal_is_rejected(client: TestClient) -> None:
    r = client.post("/api/agent/command", json={"goal": "  "})
    assert r.status_code == 422, r.json()
    assert r.json()["error"]["code"] == "EMPTY_GOAL"


def test_command_before_publish_is_404(client: TestClient) -> None:
    r = client.post("/api/demo/reset", json={"mode": "empty"})
    assert r.status_code == 200, r.json()
    r = client.post("/api/agent/command", json={"goal": "tonight looks thin"})
    assert r.status_code == 404, r.json()
    assert r.json()["error"]["code"] == "NO_PUBLISHED_RUN"
