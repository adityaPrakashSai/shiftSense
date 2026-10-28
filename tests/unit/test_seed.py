import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain.models import RequestKind, RequestStatus, RunStatus
from app.main import app
from app.store import repo
from app.store.seed import SEED_DIR, load_seed, seed_db

DEVANAGARI = range(0x0900, 0x0980)


def test_default_seed_shape() -> None:
    seed = load_seed("default")
    nurses = seed.ward.nurses
    assert len(nurses) == 24 and len({n.id for n in nurses}) == 24
    assert sum(n.standby for n in nurses) == 4
    assert seed.ward.horizon_start.isoformat() == "2026-10-26" and seed.ward.horizon_days == 14
    assert seed.ward.horizon_start.weekday() == 0
    for n in nurses:  # aliases in Roman and Devanagari script
        assert any(a.isascii() for a in n.aliases)
        assert any(any(ord(ch) in DEVANAGARI for ch in a) for a in n.aliases), n.id
    kinds = [r.kind for r in seed.requests]
    assert kinds.count(RequestKind.LEAVE) == 3
    assert kinds.count(RequestKind.DAY_OFF) == 6
    assert kinds.count(RequestKind.SHIFT_WANTED) + kinds.count(RequestKind.SHIFT_AVOID) == 3
    assert all(
        r.hard == (r.kind in (RequestKind.LEAVE, RequestKind.UNAVAILABLE)) for r in seed.requests
    )
    assert seed.history.periods == 2 and set(seed.history.nurses) == {n.id for n in nurses}


@pytest.mark.parametrize("name", ["default", "mini", "real_a"])
def test_history_is_consistent(name: str) -> None:
    """Carry-in run lengths match the last three days, and no M/E follows a night."""
    seed = load_seed(name)
    # real_a's ANS works G (general duty), which the fixture's last_days records as off ("-")
    g_duty = {n.id for n in seed.ward.nurses if n.role == "ans"}
    for nid, h in seed.history.nurses.items():
        last = h.last_days
        if nid in g_duty:
            assert all(s is None for s in last), nid
            continue
        trail_work = next((i for i, s in enumerate(reversed(last)) if s is None), len(last))
        trail_n = next((i for i, s in enumerate(reversed(last)) if s != "N"), len(last))
        if trail_work < len(last):
            assert h.consecutive_days == trail_work, nid
        else:
            assert h.consecutive_days >= len(last), nid
        if trail_n < len(last):
            assert h.consecutive_nights == trail_n, nid
        for a, b in zip(last, last[1:], strict=False):
            assert not (a == "N" and b in ("M", "E")), nid


def test_mini_and_real_a_load() -> None:
    mini = load_seed("mini")
    assert len(mini.ward.nurses) == 9 and sum(n.standby for n in mini.ward.nurses) == 2
    assert mini.ward.night_block_max == 3
    real = load_seed("real_a")
    assert len(real.ward.nurses) == 22 and real.ward.horizon_days == 31
    assert real.ward.max_consecutive_days == 8 and real.ward.night_block_max == 4


def test_real_a_import_runs(conn: sqlite3.Connection) -> None:
    seed = seed_db(conn, "real_a")
    raw = json.loads((SEED_DIR / "real_ward_a.json").read_text(encoding="utf-8"))
    runs = {r["run_id"]: r for r in repo.list_runs(conn, seed.ward.id)}
    planned, updated = runs["run-ward-real-a-planned"], runs["run-ward-real-a-updated"]
    assert planned["status"] == RunStatus.SUPERSEDED and planned["kind"] == "import"
    assert updated["status"] == RunStatus.PUBLISHED and updated["parent_run"] == planned["run_id"]
    assert repo.get_published_run(conn, seed.ward.id)["run_id"] == updated["run_id"]
    for key, run_id in (("planned", planned["run_id"]), ("updated", updated["run_id"])):
        assert repo.get_roster(conn, run_id) == raw["runs"][key]["roster"]
    n = conn.execute(
        "SELECT COUNT(*) FROM assignment WHERE run_id = ?", (updated["run_id"],)
    ).fetchone()[0]
    assert n == 22 * 31
    changed = conn.execute(
        "SELECT COUNT(*) FROM assignment WHERE run_id = ? AND changed = 1", (updated["run_id"],)
    ).fetchone()[0]
    assert changed > 0


def test_reseed_replaces_everything(conn: sqlite3.Connection) -> None:
    seed_db(conn, "real_a")
    seed_db(conn, "mini")
    assert conn.execute("SELECT COUNT(*) FROM ward").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0
    assert len(repo.list_requests(conn, "ward-mini")) == 9


def test_request_crud(conn: sqlite3.Connection) -> None:
    seed = seed_db(conn, "default")
    r = seed.requests[0]
    assert repo.get_request(conn, r.id) == r
    assert repo.next_request_id(conn) == "R-0013"
    repo.set_request_status(conn, r.id, RequestStatus.WITHDRAWN)
    assert repo.get_request(conn, r.id).status is RequestStatus.WITHDRAWN
    active = repo.list_requests(conn, seed.ward.id, status=RequestStatus.ACTIVE)
    assert len(active) == 11
    assert [x.id for x in repo.list_requests(conn, seed.ward.id, nurse_id="N09")] == ["R-0012"]
    with pytest.raises(KeyError):
        repo.set_request_status(conn, "R-9999", RequestStatus.ACTIVE)


@pytest.mark.parametrize(("seed", "nurses", "standby"), [("default", 24, 4), ("mini", 9, 2)])
def test_get_ward_after_startup_seed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, seed: str, nurses: int, standby: int
) -> None:
    monkeypatch.setenv("SEED", seed)
    with TestClient(app) as client:
        body = client.get("/api/ward").json()
        assert len(body["nurses"]) == nurses
        assert sum(n["standby"] for n in body["nurses"]) == standby
        assert body["priorities"] == ["coverage", "stability", "contract", "requests", "fairness"]
        assert client.get("/api/health").json()["db"] == "ok"


def test_get_ward_404_envelope(conn: sqlite3.Connection) -> None:
    r = TestClient(app).get("/api/ward")  # no lifespan: migrated but empty database
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "WARD_NOT_FOUND"
