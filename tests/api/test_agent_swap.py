"""Open swaps through the API: nobody named, an optional cell offered back. The agent
ranks partners, the charge nurse approves the plan once, partners are asked in order,
and the first accept (or granted condition) comes back for one Publish."""

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


def wait_for(get: Callable[[], Any], ok: Callable[[Any], bool], secs: float = 90.0) -> Any:
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


def ask(client: TestClient, nurse_id: str) -> dict[str, Any]:
    rows = [m for m in client.get("/api/inbox", params={"nurse_id": nurse_id}).json()
            if m["kind"] != "note"]  # fmt: skip
    assert len(rows) == 1, rows
    return dict(rows[0])


def start_swap(client: TestClient, text: str) -> tuple[dict[str, Any], list[str]]:
    """Send an open swap, confirm it, approve the contact plan; the first nurse is asked."""
    body = client.post("/api/messages", data={"sender_id": "N07", "text": text}).json()
    assert body["reply_kind"] == "confirm", body
    out = client.post(f"/api/requests/{body['request_id']}/confirm").json()
    assert out["request"]["status"] == "active", out
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    order = list(t["waiting"]["cascade"])
    assert 1 <= len(order) <= 3, order
    r = client.post(f"/api/agent/tasks/{t['id']}/approve-cascade")
    assert r.status_code == 200, r.json()
    wait_for(lambda: task(client), lambda t: t["status"] == "waiting_reply")
    return out, order


def published_cell(client: TestClient, nurse_id: str, day: str) -> str | None:
    roster = client.get("/api/roster").json()
    return next(c["shift"] for c in roster["grid"]
                if c["nurse_id"] == nurse_id and c["day"] == day)  # fmt: skip


def test_open_swap_confirm_names_the_offer(client: TestClient) -> None:
    body = client.post(
        "/api/messages",
        data={
            "sender_id": "N07",
            "text": "can someone take my Saturday morning? I'll do Friday morning",
        },
    ).json()
    assert body["reply_kind"] == "confirm", body
    assert body["reply"].startswith("Open swap: you give"), body["reply"]
    assert "Fri 6 Nov" in body["reply"] and "ask" in body["reply"].lower(), body["reply"]
    out = client.post(f"/api/requests/{body['request_id']}/confirm").json()
    assert out["request"]["status"] == "active", out
    assert out["run_id"] is None  # nothing to solve until a partner accepts
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    assert t["workflow"] == "swap" and t["waiting"]["cascade"]
    calls = client.get(f"/api/agent/tasks/{t['id']}").json()["calls"]
    found = next(c for c in calls if c["tool"] == "find_swap_partners")
    assert [p["nurse"] for p in found["result"]["pairs"]] == ["N01", "N03", "N04"]
    assert found["result"]["excluded"], "someone is always excluded"


def test_open_swap_without_offer(client: TestClient) -> None:
    body = client.post(
        "/api/messages",
        data={"sender_id": "N07", "text": "can someone take my Saturday morning?"},
    ).json()
    assert body["reply_kind"] == "confirm", body
    assert "Open swap: you give" in body["reply"] and "take" not in body["reply"].lower(), body
    out = client.post(f"/api/requests/{body['request_id']}/confirm").json()
    assert out["request"]["status"] == "active", out
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    assert len(t["waiting"]["cascade"]) == 3


def test_open_swap_rejects_a_busy_offer(client: TestClient) -> None:
    body = client.post(
        "/api/messages",
        data={
            "sender_id": "N07",
            "text": "can someone take my Saturday morning? I'll do Sunday evening",
        },
    ).json()
    assert body["reply_kind"] == "clarify", body
    assert "already work" in body["reply"], body["reply"]


def test_swap_cascade_decline_then_accept(client: TestClient) -> None:
    _, order = start_swap(client, "can someone take my Saturday morning? I'll do Friday morning")
    first = ask(client, order[0])
    assert first["can_condition"] and first["kind"] == "swap"
    r = client.post(f"/api/inbox/{first['id']}/respond", json={"accept": False})
    assert r.status_code == 200, r.json()
    wait_for(
        lambda: task(client),
        lambda t: t["status"] == "waiting_reply" and t["waiting_on"] == order[1],
    )
    second = ask(client, order[1])
    r = client.post(f"/api/inbox/{second['id']}/respond", json={"accept": True})
    assert r.status_code == 200, r.json()
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    new_id = t["waiting"]["ref"]
    assert client.post(f"/api/runs/{new_id}/publish").status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "done")
    assert "published" in t["outcome"]
    # both moves hold in the published roster
    assert published_cell(client, order[1], "2026-11-07") == "M"
    assert published_cell(client, "N07", "2026-11-06") == "M"
    assert published_cell(client, order[1], "2026-11-06") is None
    assert published_cell(client, "N07", "2026-11-07") is None


def test_conditional_swap_publishes_moves_and_condition(client: TestClient) -> None:
    _, order = start_swap(client, "can someone take my Saturday morning? I'll do Friday morning")
    m = ask(client, order[0])
    body = {"accept": True, "condition": {"kind": "day_off", "day": "2026-11-08"}}
    r = client.post(f"/api/inbox/{m['id']}/respond", json=body)
    assert r.status_code == 200, r.json()
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    assert "swaps with" in t["waiting"]["summary"] and "cells change" in t["waiting"]["summary"]
    new_id = t["waiting"]["ref"]
    assert client.post(f"/api/runs/{new_id}/publish").status_code == 200
    wait_for(lambda: task(client), lambda t: t["status"] == "done")
    assert published_cell(client, order[0], "2026-11-07") == "M"
    assert published_cell(client, "N07", "2026-11-06") == "M"
    assert published_cell(client, order[0], "2026-11-08") is None  # the granted condition


def test_exhausted_swap_notes_the_requester(client: TestClient) -> None:
    _, order = start_swap(client, "can someone take my Saturday morning? I'll do Friday morning")
    for nurse in order:
        m = ask(client, nurse)
        r = client.post(f"/api/inbox/{m['id']}/respond", json={"accept": False})
        assert r.status_code == 200, r.json()
        if nurse != order[-1]:
            nxt = order[order.index(nurse) + 1]
            wait_for(
                lambda: task(client),
                lambda t, nxt=nxt: t["status"] == "waiting_reply" and t["waiting_on"] == nxt,
            )
    t = wait_for(lambda: task(client), lambda t: t["status"] == "escalated")
    assert "nobody could swap" in t["outcome"]
    notes = [m for m in client.get("/api/inbox", params={"nurse_id": "N07"}).json()
             if m["kind"] == "note"]  # fmt: skip
    assert len(notes) == 1, notes
    assert notes[0]["status"] == "noted"
    assert "koi swap nahi kar paya" in notes[0]["text"]  # N07 reads Hindi
    # code published nothing: the baseline still stands
    assert published_cell(client, "N07", "2026-11-07") == "M"
    assert published_cell(client, "N01", "2026-11-06") == "M"


def test_bigger_swap_gets_its_own_clarification(client: TestClient) -> None:
    _, order = start_swap(client, "can someone take my Saturday morning? I'll do Friday morning")
    m = ask(client, order[0])
    r = client.post(
        f"/api/inbox/{m['id']}/condition/check",
        json={"text": "only if Meena takes my Wednesday night and I take her Friday morning"},
    )
    assert r.status_code == 422, r.json()
    assert r.json()["error"]["code"] == "CONDITION_UNCLEAR"
    assert "Bade swap ke liye Deepa" in r.json()["error"]["message"]


def test_named_swap_takes_no_conditions(client: TestClient) -> None:
    body = client.post(
        "/api/messages",
        data={
            "sender_id": "N07",
            "text": "swap: Meena, take my Saturday morning and I'll take her Friday morning",
        },
    ).json()
    assert body["reply_kind"] == "confirm", body
    out = client.post(f"/api/requests/{body['request_id']}/confirm").json()
    assert out["request"]["status"] == "pending_approval", out
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_reply")
    m = ask(client, t["waiting_on"])
    assert not m["can_condition"]
    body = {"accept": True, "condition": {"kind": "day_off", "day": "2026-11-08"}}
    r = client.post(f"/api/inbox/{m['id']}/respond", json=body)
    assert r.status_code == 422, r.json()
    assert r.json()["error"]["code"] == "CONDITION_UNCLEAR"
