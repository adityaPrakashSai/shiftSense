"""Every M4 route: happy path and one error path (design doc 10.1, 10.2, 11.3)."""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.store.db import connect


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    # these tests read the event stream around one solve; the new-period agent (M10) would add
    # its own events concurrently, so it is off here and tested in test_agent.py
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        yield c


def until(ws: Any, kind: str, limit: int = 2000) -> list[dict[str, Any]]:
    """Read socket messages until one of type `kind`; returns everything read."""
    seen = []
    for _ in range(limit):
        msg = ws.receive_json()
        seen.append(msg)
        if msg["type"] == kind:
            return seen
    raise AssertionError(f"no {kind} message")


def generate(client: TestClient) -> str:
    with client.websocket_connect("/ws/ward/ward-mini") as ws:
        r = client.post("/api/runs", json={"kind": "full"})
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        msgs = until(ws, "run_finished")
    assert msgs[0] == {"type": "run_started", "run_id": run_id}
    assert any(m["type"] == "event" for m in msgs)
    assert msgs[-1] == {"type": "run_finished", "run_id": run_id, "status": "proposed"}
    return run_id


def test_health_and_ward(client: TestClient) -> None:
    h = client.get("/api/health").json()
    assert h["ok"] and h["llm_mode"] == "mock" and h["db"] == "ok"
    ward = client.get("/api/ward").json()
    assert ward["id"] == "ward-mini" and len(ward["nurses"]) == 9


def test_roster_published(client: TestClient) -> None:
    body = client.get("/api/roster").json()
    assert body["run"]["status"] == "published" and body["run"]["kind"] == "full"
    assert len(body["days"]) == 14 and len(body["grid"]) == 9 * 14
    assert len(body["totals"]) == 14 * 3 and len(body["stages"]) == 5
    cells = {(c["nurse_id"], c["day"]): c for c in body["grid"]}
    assert cells[("N05", "2026-10-26")]["display"] == "N/O"  # carry-in nights
    assert cells[("N04", "2026-10-28")]["display"] == "L"  # approved leave, no type given
    for c in body["grid"]:
        assert c["display"] in ("M", "E", "N", "N/O", "D/O", "L") or c["leave_type"]
    # day 0 is 2 slots short (carry-in: 5 regular nurses for 7 slots); which shift is short is
    # the solver's choice
    day0 = [t for t in body["totals"] if t["day"] == "2026-10-26"]
    assert sum(t["target"] for t in day0) == 7
    assert sum(t["target"] - t["assigned"] for t in day0) == 2


def test_roster_filters_and_errors(client: TestClient) -> None:
    body = client.get("/api/roster", params={"from": "2026-10-27", "to": "2026-10-29"}).json()
    assert body["days"] == ["2026-10-27", "2026-10-28", "2026-10-29"]
    assert len(body["grid"]) == 27 and len(body["totals"]) == 9
    r = client.get("/api/roster", params={"run_id": "run-nope"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "RUN_NOT_FOUND"
    r = client.get("/api/roster", params={"from": "not-a-date"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_generate_inspect_publish_lineage(client: TestClient) -> None:
    base = client.get("/api/roster").json()["run"]["run_id"]
    run_id = generate(client)

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["status"] == "proposed" and detail["run"]["parent_run"] == base
    assert [s["tier"] for s in detail["stages"]][0] == "coverage"
    by_tier: dict[str, int] = {}
    for c in detail["costs"]:
        by_tier[c["tier"]] = by_tier.get(c["tier"], 0) + c["total"]
    for s in detail["stages"]:
        assert by_tier.get(s["tier"], 0) == s["best"]
    assert len(detail["request_outcomes"]) == 9
    assert client.get("/api/runs/run-nope").status_code == 404

    lin = client.get(f"/api/runs/{run_id}/lineage").json()
    assert [r["run_id"] for r in lin] == [base, run_id]

    with client.websocket_connect("/ws/ward/ward-mini") as ws:
        r = client.post(f"/api/runs/{run_id}/publish", json={"by": "charge"})
        assert r.status_code == 200 and r.json()["status"] == "published"
        assert until(ws, "roster_published")[-1]["run_id"] == run_id
    again = client.post(f"/api/runs/{run_id}/publish", json={"by": "charge"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "RUN_NOT_PROPOSED"
    assert client.get("/api/roster").json()["run"]["run_id"] == run_id
    assert client.get(f"/api/runs/{base}").json()["run"]["status"] == "superseded"


def test_events(client: TestClient) -> None:
    run_id = generate(client)
    evs = client.get("/api/events", params={"run_id": run_id}).json()
    kinds = [e["kind"] for e in evs]
    assert kinds[0] == "run.started" and "model.built" in kinds and kinds[-1] == "run.proposed"
    assert {"stage.start", "stage.improved", "stage.done"} <= set(kinds)
    tail = client.get("/api/events", params={"after_seq": evs[-2]["seq"]}).json()
    assert [e["seq"] for e in tail] == [evs[-1]["seq"]]
    assert client.get("/api/events", params={"limit": 0}).status_code == 422
    newest = client.get("/api/events", params={"last": 2}).json()
    assert [e["seq"] for e in newest] == [evs[-2]["seq"], evs[-1]["seq"]]


def test_demo_reset_restores_baseline_and_keeps_cache(client: TestClient, db_path: Path) -> None:
    base = client.get("/api/roster").json()["run"]["run_id"]
    run_id = generate(client)
    client.post(f"/api/runs/{run_id}/publish", json={})
    conn = connect(db_path)
    conn.execute("INSERT INTO llm_cache(cache_key, provider) VALUES('k1', 'baseten')")
    conn.commit()
    # the committed cache (data/llm_cache.jsonl) is imported at startup, so compare counts
    cached = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
    t0 = time.perf_counter()
    r = client.post("/api/demo/reset")
    assert r.status_code == 200 and time.perf_counter() - t0 < 3
    assert r.json()["run_id"] == base and r.json()["seed"] == "mini"
    assert client.get("/api/roster").json()["run"]["run_id"] == base
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == cached
    assert client.get("/api/events").json()[-1]["kind"] == "demo.reset"
    conn.close()


def test_ws_off_closes_and_refuses_sockets(client: TestClient) -> None:
    with client.websocket_connect("/ws/ward/ward-mini") as ws:
        assert client.post("/api/dev/ws-off", json={"seconds": 5}).json() == {"closed": 1}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/ward/ward-mini") as ws2:
        ws2.receive_json()


def test_unknown_api_route_uses_envelope(client: TestClient) -> None:
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert "text/html" in client.get("/charge").headers["content-type"]
