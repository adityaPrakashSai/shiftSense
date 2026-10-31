"""The replacement cascade through the API: approve one contact plan, decline the first
nurse, accept the second, publish once. Approving the plan publishes nothing."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import repo
from app.store.db import open_db


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


def test_cascade_end_to_end(client: TestClient, db_path: Path) -> None:
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
    assert t["waiting"]["ref"] == run_id
    order = t["waiting"]["cascade"]
    assert len(order) == 3
    p = client.get(f"/api/proposals/{run_id}").json()
    assert p["agent"]["cascade"] == order
    assert [c["nurse"] for c in p["candidates"]][:3] == order
    assert t["cascade"]["order"] == order and not t["cascade"]["approved"]

    # approving the plan publishes nothing: the run is still proposed
    r = client.post(f"/api/agent/tasks/{t['id']}/approve-cascade")
    assert r.status_code == 200, r.json()
    assert client.get(f"/api/proposals/{run_id}").json()["run"]["status"] == "proposed"
    assert client.get("/api/roster").json()["run"]["run_id"] != run_id

    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_reply")
    assert t["waiting_on"] == order[0] and t["cascade"]["approved"]
    first = client.get("/api/inbox", params={"nurse_id": order[0]}).json()
    assert len(first) == 1 and first[0]["status"] == "asked"

    # decline: the next nurse in the plan is asked, with no new approval
    r = client.post(f"/api/inbox/{first[0]['id']}/respond", json={"accept": False})
    assert r.status_code == 200
    t = wait_for(
        lambda: task(client),
        lambda t: t["status"] == "waiting_reply" and t["waiting_on"] == order[1],
    )
    assert t["cascade"]["asked"][order[0]] == "declined"
    assert t["cascade"]["asked"][order[1]] == "waiting"
    assert t["cascade"]["deadline_at"]
    second = client.get("/api/inbox", params={"nurse_id": order[1]}).json()
    assert len(second) == 1 and second[0]["status"] == "asked"

    # accept: one publish approval for the accepter's candidate run
    r = client.post(f"/api/inbox/{second[0]['id']}/respond", json={"accept": True})
    assert r.status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    cand = next(c for c in p["candidates"] if c["nurse"] == order[1])
    assert t["waiting"]["ref"] == cand["run_id"] and not t["waiting"].get("cascade")
    assert "accepted" in t["waiting"]["summary"]

    r = client.post(f"/api/runs/{cand['run_id']}/publish", json={})
    assert r.status_code == 200, r.json()
    t = wait_for(lambda: task(client), lambda t: t["status"] == "done")
    assert t["outcome"] == "published by the charge nurse"
    assert t["counters"]["messages"] == 2 and t["counters"]["declines"] == 1
    assert t["counters"]["forbidden"] == 0

    kinds = [e["kind"] for e in client.get("/api/events", params={"last": 400}).json()]
    assert "agent.cascade_approved" in kinds

    # the plan cannot be approved twice, nor for an unknown task
    again = client.post(f"/api/agent/tasks/{t['id']}/approve-cascade")
    assert again.status_code == 409 and again.json()["error"]["code"] == "CASCADE_NOT_PENDING"
    missing = client.post("/api/agent/tasks/task-9999/approve-cascade")
    assert missing.status_code == 404


def test_accept_withdraws_other_open_asks(client: TestClient, db_path: Path) -> None:
    ward_id = client.get("/api/ward").json()["id"]
    conn = open_db(db_path)
    try:
        for mid, nid in (("m-one", "N07"), ("m-two", "N08")):
            repo.insert_nurse_message(
                conn,
                {
                    "id": mid,
                    "ward_id": ward_id,
                    "nurse_id": nid,
                    "kind": "replacement",
                    "task_id": "task-x",
                    "day": "2026-11-03",
                    "shift": "M",
                    "text": "Cover?",
                    "status": "asked",
                },
            )
    finally:
        conn.close()
    r = client.post("/api/inbox/m-one/respond", json={"accept": True})
    assert r.status_code == 200
    conn = open_db(db_path)
    try:
        other = repo.get_nurse_message(conn, "m-two")
        assert other is not None and other["status"] == "withdrawn"
    finally:
        conn.close()
    kinds = [e["kind"] for e in client.get("/api/events", params={"last": 50}).json()]
    assert "replacement.withdrawn" in kinds
