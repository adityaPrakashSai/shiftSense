"""Conflict routes (design doc 10.1) on the night-crunch scenario script."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from scripts.scenarios import night_crunch


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


def test_crunch_conflict_resolve(client: TestClient) -> None:
    assert night_crunch.main([]) == 0
    open_ = client.get("/api/conflicts").json()
    assert len(open_) == 1
    c = open_[0]
    assert "min_cover:d8:N" in c["core"] and 2 <= len(c["core"]) <= 6
    assert 1 <= len(c["options"]) <= 4 and c["context"]
    assert client.get(f"/api/conflicts/{c['conflict_id']}").json()["options"] == c["options"]

    with client.websocket_connect("/ws/ward/ward-mini") as ws:
        r = client.post(f"/api/conflicts/{c['conflict_id']}/resolve", json={"option_id": "O1"})
        assert r.status_code == 200
        run_id = r.json()["run_id"]
        done = until(ws, "run_finished")
    assert done == {"type": "run_finished", "run_id": run_id, "status": "proposed"}
    after = client.get(f"/api/conflicts/{c['conflict_id']}").json()
    assert after["chosen_option"] == "O1" and after["resolved_run"] == run_id
    assert client.get("/api/conflicts").json() == []
    run = client.get(f"/api/runs/{run_id}").json()["run"]
    assert run["trigger"] == f"conflict:{c['conflict_id']}"
    kinds = [e["kind"] for e in client.get("/api/events", params={"last": 200}).json()]
    assert {"conflict.created", "conflict.resolved", "run.proposed"} <= set(kinds)

    again = client.post(f"/api/conflicts/{c['conflict_id']}/resolve", json={"option_id": "O2"})
    assert again.status_code == 409


def test_conflict_errors(client: TestClient) -> None:
    assert client.get("/api/conflicts/C-9999").status_code == 404
    r = client.post("/api/conflicts/C-9999/resolve", json={"option_id": "O1"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "CONFLICT_NOT_FOUND"
