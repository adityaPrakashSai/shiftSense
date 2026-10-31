"""Steering a running sick-call task: Skip to next expires the open ask so the cascade
moves on; Cancel task withdraws the ask and ends without publishing anything."""

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
    with TestClient(app) as c:
        yield c


def wait_for(get: Callable[[], Any], ok: Callable[[Any], bool], secs: float = 30.0) -> Any:
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        val = get()
        if ok(val):
            return val
        time.sleep(0.05)
    raise AssertionError(f"timed out; last value: {val}")


def task(client: TestClient) -> dict[str, Any]:
    tasks = client.get("/api/agent/tasks").json()
    assert tasks, "no agent task"
    return dict(tasks[0])


def asking(client: TestClient) -> tuple[dict[str, Any], str]:
    """A sick call with the plan approved and the first nurse asked; returns task, run id."""
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
    run_id = client.post(f"/api/requests/{body['request_id']}/confirm").json()["run_id"]
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    assert client.post(f"/api/agent/tasks/{t['id']}/approve-cascade").status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_reply")
    return t, run_id


def kinds(client: TestClient, n: int = 400) -> list[str]:
    return [e["kind"] for e in client.get("/api/events", params={"last": n}).json()]


def test_skip_moves_to_the_next_nurse(client: TestClient) -> None:
    t, _run_id = asking(client)
    order = t["cascade"]["order"]
    first = client.get("/api/inbox", params={"nurse_id": order[0]}).json()
    assert len(first) == 1

    r = client.post(f"/api/agent/tasks/{t['id']}/skip")
    assert r.status_code == 200, r.json()
    assert client.get("/api/inbox", params={"nurse_id": order[0]}).json()[0]["status"] == "expired"

    t = wait_for(
        lambda: task(client),
        lambda t: t["status"] == "waiting_reply" and t["waiting_on"] == order[1],
    )
    assert t["cascade"]["asked"][order[0]] == "timeout"
    assert t["counters"]["timeouts"] == 1 and t["counters"]["declines"] == 1
    assert "agent.steered" in kinds(client)


def test_cancel_ends_without_publishing(client: TestClient) -> None:
    t, run_id = asking(client)
    r = client.post(f"/api/agent/tasks/{t['id']}/cancel")
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "done" and r.json()["outcome"] == "cancelled"

    # nothing published: the proposal is still proposed, the roster is the baseline
    assert client.get(f"/api/proposals/{run_id}").json()["run"]["status"] == "proposed"
    assert client.get("/api/roster").json()["run"]["run_id"] != run_id
    # no open asks left: the one open ask was withdrawn
    assert [m["status"] for m in client.get("/api/inbox").json()] == ["withdrawn"]
    assert "agent.steered" in kinds(client)

    # steering a finished task, or an unknown one, is rejected
    for route in ("skip", "cancel"):
        again = client.post(f"/api/agent/tasks/{t['id']}/{route}")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "NOT_WAITING_REPLY"
        missing = client.post(f"/api/agent/tasks/task-9999/{route}")
        assert missing.status_code == 404
