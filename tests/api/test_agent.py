"""Roster agent through the API (design doc 7.10, 6.5): a confirmed sick call is ranked, waits
for the charge nurse, messages the approved nurse, and on a decline ranks again without them."""

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


def sick_call(client: TestClient) -> tuple[str, str]:
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
    out = client.post(f"/api/requests/{body['request_id']}/confirm").json()
    return nurse, out["run_id"]


def test_sick_call_agent_end_to_end(client: TestClient) -> None:
    _nurse, run_id = sick_call(client)
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    assert t["workflow"] == "sick_call" and t["mode"] == "agent"
    assert [s["tool"] for s in t["trace"] if s["kind"] in ("tool", "wait")] == [
        "get_open_cells", "rank_replacements", "request_approval",
    ]  # fmt: skip
    assert t["waiting"]["approval_kind"] == "run" and t["waiting"]["ref"] == run_id
    assert t["waiting"]["summary_source"] == "agent"  # the mock's claims pass the check
    # the proposal card shows the agent's summary; no message before approval
    p = client.get(f"/api/proposals/{run_id}").json()
    assert p["agent"]["task_id"] == t["id"] and p["agent"]["summary"]
    assert client.get("/api/inbox").json() == []

    top = p["candidates"][0]["nurse"]
    client.post(f"/api/proposals/{run_id}/choose", json={"nurse_id": top})
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_reply")
    assert t["waiting_on"] == top
    inbox = client.get("/api/inbox", params={"nurse_id": top}).json()
    assert len(inbox) == 1 and inbox[0]["status"] == "asked" and inbox[0]["kind"] == "replacement"
    assert "3 Nov" in inbox[0]["text"] and "Morning" in inbox[0]["text"]
    assert "bukhar" not in inbox[0]["text"].lower()
    assert client.get(f"/api/proposals/{run_id}").json()["replacement_status"] == "asked"

    # decline: the agent ranks again without the decliner and asks for approval
    r = client.post(f"/api/inbox/{inbox[0]['id']}/respond", json={"accept": False})
    assert r.status_code == 200 and r.json()["status"] == "declined"
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    second = t["waiting"]["ref"]
    assert second != run_id
    p2 = client.get(f"/api/proposals/{second}").json()
    assert top not in [c["nurse"] for c in p2["candidates"]]
    nxt = p2["candidates"][0]["nurse"]
    client.post(f"/api/proposals/{second}/choose", json={"nurse_id": nxt})
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_reply")
    r = client.post(f"/api/proposals/{second}/respond", json={"nurse_id": nxt, "accept": True})
    assert r.status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "done")
    assert t["outcome"].endswith("accepted") and t["counters"]["forbidden"] == 0
    assert t["counters"]["declines"] == 1 and t["counters"]["messages"] == 2

    kinds = [e["kind"] for e in client.get("/api/events", params={"last": 400}).json()]
    for k in ("agent.started", "agent.step", "agent.waiting", "agent.resumed", "agent.done",
              "message.drafted", "replacement.declined", "replacement.accepted"):  # fmt: skip
        assert k in kinds, k
    detail = client.get(f"/api/agent/tasks/{t['id']}").json()
    assert detail["calls"] and detail["calls"][0]["tool"] == "get_open_cells"


def test_inbox_errors(client: TestClient) -> None:
    r = client.post("/api/inbox/NM-9999/respond", json={"accept": True})
    assert r.status_code == 404 and r.json()["error"]["code"] == "MESSAGE_NOT_FOUND"
    assert client.get("/api/agent/tasks/task-9999").status_code == 404
    r = client.post("/api/proposals/run-nope/respond", json={"nurse_id": "N01", "accept": True})
    assert r.status_code == 404


def test_priorities_reorder(client: TestClient) -> None:
    ward = client.get("/api/ward").json()
    new = list(reversed(ward["priorities"]))
    r = client.put("/api/ward/priorities", json={"priorities": new})
    assert r.status_code == 200 and r.json()["priorities"] == new
    bad = client.put("/api/ward/priorities", json={"priorities": ["coverage"]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "BAD_PRIORITIES"


def test_fairness_and_versions(client: TestClient) -> None:
    f = client.get("/api/fairness").json()
    assert f["run_id"] and f["earlier_periods"] >= 0 and f["nurses"]
    assert {"nights", "all_nights"} <= set(f["ward_average"])
    v = client.get("/api/versions").json()
    assert v["planned"] and v["versions"][0]["label"] == "current"
    assert v["versions"][0]["changes"] == []  # nothing published since the plan yet
    _nurse, run_id = sick_call(client)
    wait_for(lambda: client.get("/api/versions").json(), lambda v: len(v["versions"]) == 2)
    p = client.get(f"/api/proposals/{run_id}").json()
    client.post(f"/api/proposals/{run_id}/choose", json={"nurse_id": p["candidates"][0]["nurse"]})
    v = client.get("/api/versions").json()
    cur = v["versions"][0]
    assert cur["run"]["run_id"] == run_id and cur["changes"]
    assert {c["cause"] for c in cur["changes"]} >= {"request", "replacement"}
    assert all(c["approved_by"] == "charge" for c in cur["changes"])
