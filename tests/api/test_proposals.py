"""Proposal routes (design doc 10.1): sick call -> confirm -> ranked proposal -> choose."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    with TestClient(app) as c:
        yield c


def until(ws: Any, kind: str) -> dict[str, Any]:
    for _ in range(2000):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return dict(msg)
    raise AssertionError(f"no {kind} message")


def test_sick_call_proposal_and_choose(client: TestClient) -> None:
    roster = client.get("/api/roster").json()
    nurse = next(
        c["nurse_id"]
        for c in roster["grid"]
        if c["day"] == "2026-11-03" and c["shift"] == "M" and not c["nurse_id"].startswith("S")
    )
    body = client.post(
        "/api/messages",
        data={"sender_id": nurse, "text": "aaj subah ki duty nahi kar paungi, bukhar hai"},
    ).json()
    assert body["reply_kind"] == "confirm", body
    with client.websocket_connect("/ws/ward/ward-mini") as ws:
        out = client.post(f"/api/requests/{body['request_id']}/confirm").json()
        run_id = out["run_id"]
        assert until(ws, "run_finished") == {
            "type": "run_finished", "run_id": run_id, "status": "proposed",
        }  # fmt: skip
        created = until(ws, "proposal_created")
    assert created == {"type": "proposal_created", "run_id": run_id,
                       "request_id": body["request_id"]}  # fmt: skip

    p = client.get(f"/api/proposals/{run_id}").json()
    assert p["open_cell"] == {"nurse": nurse, "day": "2026-11-03", "shift": "M"}
    assert p["candidates"] and p["candidates"][0]["run_id"] == run_id
    assert p["request"]["id"] == body["request_id"] and p["run"]["kind"] == "resolve"
    causes = {(c["nurse_id"], c["day"]): c["cause"] for c in p["change_set"]}
    assert causes[(nurse, "2026-11-03")] == "request"
    assert all(e["rules"] for e in p["excluded"])
    grid = client.get("/api/roster", params={"run_id": run_id}).json()["grid"]
    assert any(c["cause"] == "replacement" for c in grid)
    assert [x["run_id"] for x in client.get("/api/proposals").json()] == [run_id]

    top = p["candidates"][0]["nurse"]
    r = client.post(f"/api/proposals/{run_id}/choose", json={"nurse_id": top})
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert client.get(f"/api/proposals/{run_id}").json()["chosen_nurse"] == top
    assert client.get("/api/proposals").json() == []
    again = client.post(f"/api/proposals/{run_id}/choose", json={"nurse_id": top})
    assert again.status_code == 409


def test_proposal_errors(client: TestClient) -> None:
    assert client.get("/api/proposals/run-nope").status_code == 404
    r = client.post("/api/proposals/run-nope/choose", json={"nurse_id": "N01"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "PROPOSAL_NOT_FOUND"
