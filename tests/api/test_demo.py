"""Demo page backend (M11, US-9): the pinned mini baseline, reset that tells every view, and reset
stopping running agent tasks before it restores the database."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import jobs
from app.domain.models import RequestStatus
from app.domain.seeds import load_seed
from app.main import app
from app.store import repo
from app.store import seed as seed_mod
from app.store.db import open_db
from tests.helpers import EXPECTED


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        yield c


def baseline_meta() -> dict[str, Any]:
    return dict(json.loads(seed_mod.baseline_path("mini").read_text(encoding="utf-8"))["meta"])


def test_pinned_baseline_is_current_and_golden() -> None:
    meta = baseline_meta()
    assert meta["seed_sha256"] == seed_mod.seed_sha("mini"), "run scripts.demo_baseline"
    golden = EXPECTED["baseline"]["vector"]  # the proven 3/0/0/19/55
    assert {tier: best for tier, (best, _status) in meta["tiers"].items()} == golden


def test_startup_loads_the_pinned_baseline(client: TestClient) -> None:
    roster = client.get("/api/roster").json()
    assert roster["run"]["run_id"] == baseline_meta()["run_id"]
    assert {s["tier"]: s["best"] for s in roster["stages"]}["fairness"] == 55
    r = client.post("/api/demo/reset")
    assert r.status_code == 200 and r.json()["run_id"] == baseline_meta()["run_id"]


def test_stale_baseline_is_ignored(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_mod, "seed_sha", lambda name: "edited")
    conn = open_db(db_path)
    assert seed_mod.load_baseline(conn, "mini") is False
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0
    assert seed_mod.load_baseline(conn, "real_a") is False  # no committed baseline
    conn.close()


def test_reset_tells_every_view(client: TestClient) -> None:
    with client.websocket_connect("/ws/ward/ward-mini") as ws:
        client.post("/api/demo/reset")
        for _ in range(200):
            msg = ws.receive_json()
            if msg["type"] == "demo_reset":
                assert msg["run_id"] == baseline_meta()["run_id"]
                break
        else:
            raise AssertionError("no demo_reset message")


@pytest.fixture
def default_client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "default")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        yield c


def test_reset_empty_keeps_nurses_history_and_requests(
    default_client: TestClient, db_path: Path
) -> None:
    r = default_client.post("/api/demo/reset", json={"mode": "empty"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "empty" and body["run_id"] is None
    missing = default_client.get("/api/roster")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NO_PUBLISHED_RUN"
    ward = default_client.get("/api/ward").json()
    assert len(ward["nurses"]) == 24
    conn = open_db(db_path)
    try:
        assert repo.get_history(conn, ward["id"]).periods == 2
        active = repo.list_requests(conn, ward["id"], status=RequestStatus.ACTIVE)
    finally:
        conn.close()
    seed_active = [x for x in load_seed("default").requests if x.status is RequestStatus.ACTIVE]
    assert len(active) == len(seed_active)


def test_reset_published_after_empty_restores_the_baseline(default_client: TestClient) -> None:
    assert default_client.post("/api/demo/reset", json={"mode": "empty"}).status_code == 200
    assert default_client.get("/api/roster").status_code == 404
    r = default_client.post("/api/demo/reset", json={"mode": "published"})
    assert r.status_code == 200
    assert r.json()["mode"] == "published" and r.json()["run_id"] is not None
    assert default_client.get("/api/roster").status_code == 200


def test_repeat_resets_are_fast(default_client: TestClient) -> None:
    default_client.post("/api/demo/reset", json={"mode": "empty"})
    again = default_client.post("/api/demo/reset", json={"mode": "empty"}).json()
    assert again["secs"] < 1
    default_client.post("/api/demo/reset", json={"mode": "published"})
    again = default_client.post("/api/demo/reset", json={"mode": "published"}).json()
    assert again["secs"] < 1


def test_reset_empty_unsupported_for_printed_plan_seeds(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEED", "real_a")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        r = c.post("/api/demo/reset", json={"mode": "empty"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "EMPTY_NOT_SUPPORTED"


def test_startup_respects_demo_start_empty(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("DEMO_START", "empty")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        missing = c.get("/api/roster")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NO_PUBLISHED_RUN"
        assert len(c.get("/api/ward").json()["nurses"]) == 9


def test_cancel_agents_stops_running_tasks() -> None:
    async def scenario() -> tuple[bool, bool]:
        started = asyncio.Event()

        async def agent() -> None:
            started.set()
            await asyncio.sleep(60)

        async def solve() -> None:
            await asyncio.sleep(0.05)

        jobs._spawn_agent(agent())
        other = asyncio.get_running_loop().create_task(solve())  # not an agent: left alone
        jobs.tasks.add(other)
        await started.wait()
        await jobs.cancel_agents()
        agent_left = any(t.get_name() == "agent" and not t.done() for t in jobs.tasks)
        await other
        return agent_left, other.cancelled()

    agent_left, solve_cancelled = asyncio.run(scenario())
    assert not agent_left and not solve_cancelled
