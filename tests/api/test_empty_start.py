"""Empty-start intake: a request sent before anything is published queues for the next
Generate instead of starting a silent full solve, and the Generate respects it."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        yield c


def wait_for(get: Callable[[], Any], ok: Callable[[Any], bool], secs: float = 60.0) -> Any:
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        val = get()
        if ok(val):
            return val
        time.sleep(0.05)
    raise AssertionError(f"timed out; last value: {val}")


def test_sick_call_before_publish_queues_for_generate(client: TestClient) -> None:
    assert client.post("/api/demo/reset", json={"mode": "empty"}).json()["mode"] == "empty"
    ward = client.get("/api/ward").json()
    nurse = next(n["id"] for n in ward["nurses"] if not n["id"].startswith("S"))
    body = client.post(
        "/api/messages",
        data={"sender_id": nurse, "text": "aaj subah ki duty nahi kar paungi, bukhar hai"},
    ).json()
    assert body["reply_kind"] == "confirm"
    assert "publish nahi hua" in body["reply"]
    # the note leads, but the understood date and shift are still shown below it
    assert "3 Nov" in body["reply"] and "hata diya jayega" in body["reply"]
    assert body["reply"].index("Note ho gaya") < body["reply"].index("3 Nov")
    out = client.post(f"/api/requests/{body['request_id']}/confirm").json()
    assert out["run_id"] is None
    assert out["request"]["status"] == "active"
    # the stored history shows the same reply, not the normal confirmation
    msgs = client.get("/api/messages", params={"nurse_id": nurse}).json()
    assert msgs[-1]["reply"] == body["reply"]

    run_id = client.post("/api/runs").json()["run_id"]
    detail = wait_for(
        lambda: client.get(f"/api/runs/{run_id}").json(), lambda d: d["run"]["status"] == "proposed"
    )
    outcome = next(o for o in detail["request_outcomes"] if o["request_id"] == body["request_id"])
    assert outcome["status"] == "GRANTED"
    client.post(f"/api/runs/{run_id}/publish", json={})
    roster = client.get("/api/roster").json()
    cell = next(c for c in roster["grid"] if c["nurse_id"] == nurse and c["day"] == "2026-11-03")
    assert cell["shift"] != "M"
