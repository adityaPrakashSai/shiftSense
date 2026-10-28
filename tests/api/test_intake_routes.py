"""Intake and request routes (design doc 10.1): happy paths and one error path each."""

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


def say(client: TestClient, text: str, sender: str = "N03") -> dict[str, Any]:
    r = client.post("/api/messages", data={"sender_id": sender, "text": text})
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_message_confirm_runs_a_solve(client: TestClient) -> None:
    body = say(client, "Kal mujhe off chahiye")
    assert body["reply_kind"] == "confirm" and body["reply"].endswith("Confirm karein?")
    assert body["parse"]["edits"][0]["op"] == "request_day_off"
    rid = body["request_id"]
    with client.websocket_connect("/ws/ward/ward-mini") as ws:
        r = client.post(f"/api/requests/{rid}/confirm")
        assert r.status_code == 200
        out = r.json()
        assert out["request"]["status"] == "active" and out["run_id"]
        assert until(ws, "run_started")["run_id"] == out["run_id"]
        done = until(ws, "run_finished")
    assert done == {"type": "run_finished", "run_id": out["run_id"], "status": "proposed"}
    run = client.get(f"/api/runs/{out['run_id']}").json()["run"]
    assert run["trigger"] == f"request:{rid}" and run["status"] == "proposed"
    reqs = client.get("/api/requests", params={"nurse_id": "N03", "status": "active"}).json()
    mine = next(x for x in reqs if x["id"] == rid)
    assert mine["outcome"]["status"] in ("GRANTED", "DENIED")
    again = client.post(f"/api/requests/{rid}/confirm")
    assert again.status_code == 409 and again.json()["error"]["code"] == "REQUEST_STATE"


def test_chat_history_and_reject(client: TestClient) -> None:
    first = say(client, "Thursday morning chahiye")
    say(client, "namaste")
    r = client.post(f"/api/requests/{first['request_id']}/reject")
    assert r.status_code == 200 and r.json()["request"]["status"] == "rejected"
    hist = client.get("/api/messages", params={"nurse_id": "N03"}).json()
    assert [m["body"] for m in hist] == ["Thursday morning chahiye", "namaste"]
    assert hist[0]["requests"][0]["status"] == "rejected" and hist[1]["reply_kind"] == "answer"
    assert client.post("/api/requests/R-9999/reject").status_code == 404


def test_leave_waits_for_charge_approval(client: TestClient) -> None:
    body = say(client, "I need leave from 6 to 7 Nov for my sister's wedding")
    rid = body["request_id"]
    out = client.post(f"/api/requests/{rid}/confirm").json()
    assert out["request"]["status"] == "pending_approval" and out["run_id"] is None
    pending = client.get("/api/requests", params={"status": "pending_approval"}).json()
    assert [x["id"] for x in pending] == [rid]
    ok = client.post(f"/api/requests/{rid}/approve").json()
    assert ok["request"]["status"] == "active" and ok["run_id"]


def test_message_errors(client: TestClient) -> None:
    r = client.post("/api/messages", data={"sender_id": "N99", "text": "hi"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NURSE_NOT_FOUND"
    r = client.post("/api/messages", data={"sender_id": "N03", "text": "  "})
    assert r.status_code == 400 and r.json()["error"]["code"] == "EMPTY_MESSAGE"
    r = client.post(
        "/api/messages", data={"sender_id": "N03"}, files={"audio": ("a.ogg", b"x", "audio/ogg")}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "MEDIA_NOT_SUPPORTED"
    assert client.post("/api/messages", data={"text": "hi"}).status_code == 422
