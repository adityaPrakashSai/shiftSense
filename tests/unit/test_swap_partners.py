"""Open-swap partner ranking: offered and open-ended requests rank the legal takers,
explain who is excluded and why, and every returned pair validates as a roster."""

import asyncio
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from app.agent import tools
from app.agent.tools import AgentCtx
from app.config import get_settings
from app.domain.models import Request, RequestKind, RequestStatus, ShiftCode
from app.log.decision_log import DecisionLog
from app.planner import runs
from app.solver.input import SolveInput
from app.solver.validate import violations
from app.store import repo
from app.store.db import open_db
from app.store.seed import seed_db

ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE = json.loads((ROOT / "tests/fixtures/mini_expected.json").read_text())["baseline"]
SAT, FRI = date(2026, 11, 7), date(2026, 11, 6)
NOW = datetime.fromisoformat("2026-11-03T06:05:00+05:30")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    path = tmp_path / "swap.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    conn = open_db(path)
    seed_db(conn, "mini", solve=False)
    runs.evaluate_roster(conn, BASELINE["roster"], trigger="fixture")
    yield conn
    conn.close()
    get_settings.cache_clear()


def _ask(conn: sqlite3.Connection, **kw: Any) -> Request:
    r = Request(
        id=kw.pop("id"),
        nurse_id=kw.pop("nurse_id"),
        kind=RequestKind.SWAP,
        dates=kw.pop("dates"),
        shifts=kw.pop("shifts"),
        hard=True,
        status=RequestStatus.ACTIVE,
        created_at=NOW,
        **kw,
    )
    ward = repo.get_ward(conn)
    assert ward is not None
    repo.insert_request(conn, ward.id, r)
    conn.commit()
    return r


def _ctx(conn: sqlite3.Connection, rid: str) -> AgentCtx:
    ward = repo.get_ward(conn)
    assert ward is not None
    return AgentCtx(
        conn=conn,
        ward=ward,
        task={"id": "T1", "state": {"request_id": rid}},
        env=None,
        gateway=None,
        log=DecisionLog(conn, ward.id),
    )  # type: ignore[arg-type]


def _find(conn: sqlite3.Connection, rid: str) -> dict[str, Any]:
    return asyncio.run(tools.find_swap_partners(_ctx(conn, rid), rid))


def test_offered_swap_ranks_three_partners(world: sqlite3.Connection) -> None:
    _ask(
        world,
        id="R-O1",
        nurse_id="N07",
        dates=[SAT],
        shifts=[ShiftCode.M],
        counterpart_dates=[FRI],
        counterpart_shifts=[ShiftCode.M],
    )
    out = _find(world, "R-O1")
    assert [p["nurse"] for p in out["pairs"]] == ["N01", "N03", "N04"]
    for p in out["pairs"]:
        assert p["takes"] == {"day": "2026-11-07", "shift": "M"}
        assert p["gives"] == {"day": "2026-11-06", "shift": "M"}
        assert p["other_changes"] == 2 and p["reasons"] == ["offered_cell_match", "rules_pass"]
    assert out["offered"] == {"day": "2026-11-06", "shift": "M"}
    codes = {e["nurse"]: e["rules"] for e in out["excluded"]}
    assert set(codes) == {"N02", "N05", "N06"}
    assert codes["N02"] == ["no_cell_to_give"]  # Friday M is not hers
    assert codes["N05"] == ["no_cell_to_give"]
    assert codes["N06"] == ["no_cell_to_give"]


def test_open_ended_swap_considers_every_free_day(world: sqlite3.Connection) -> None:
    _ask(world, id="R-O2", nurse_id="N07", dates=[SAT], shifts=[ShiftCode.M])
    out = _find(world, "R-O2")
    assert [p["nurse"] for p in out["pairs"]] == ["N01", "N02", "N03"]
    assert out["offered"] is None
    assert all(p["reasons"][0] == "return_cell_match" for p in out["pairs"])


def test_every_pair_validates_as_a_roster(world: sqlite3.Connection) -> None:
    _ask(
        world,
        id="R-O1",
        nurse_id="N07",
        dates=[SAT],
        shifts=[ShiftCode.M],
        counterpart_dates=[FRI],
        counterpart_shifts=[ShiftCode.M],
    )
    _ask(world, id="R-O2", nurse_id="N07", dates=[SAT], shifts=[ShiftCode.M])
    ward = repo.get_ward(world)
    assert ward is not None
    pub = repo.get_published_run(world, ward.id)
    assert pub is not None
    run = repo.get_run(world, str(pub["run_id"]))
    assert run is not None
    inp = SolveInput.from_json(run["input_json"])
    grid = repo.get_roster(world, str(pub["run_id"]))
    base = {(n, d): s for n, row in grid.items() for d, s in enumerate(row)}
    assert violations(inp, base) == []
    for rid in ("R-O1", "R-O2"):
        for p in _find(world, rid)["pairs"]:
            after = dict(base)
            assert (p["takes"]["day"], p["takes"]["shift"]) == ("2026-11-07", "M")
            d2 = {"2026-11-06": 11}[p["gives"]["day"]]
            after[("N07", 12)], after[("N07", d2)] = None, p["gives"]["shift"]
            after[(p["nurse"], d2)], after[(p["nurse"], 12)] = None, "M"
            assert violations(inp, after) == [], (rid, p["nurse"])


def test_nobody_legal_lists_everyone_excluded(world: sqlite3.Connection) -> None:
    _ask(
        world,
        id="R-O3",
        nurse_id="N05",
        dates=[SAT],
        shifts=[ShiftCode.N],
        counterpart_dates=[date(2026, 11, 8)],
        counterpart_shifts=[ShiftCode.M],
    )
    out = _find(world, "R-O3")
    assert out["pairs"] == []
    codes = {e["nurse"]: e["rules"] for e in out["excluded"]}
    assert "max_nights:N01" in codes["N01"]
    assert any(r.startswith("max_") for r in codes["N03"])


def test_named_and_non_swap_requests_are_rejected(world: sqlite3.Connection) -> None:
    _ask(
        world,
        id="R-O4",
        nurse_id="N07",
        dates=[SAT],
        shifts=[ShiftCode.M],
        counterpart_id="N01",
        counterpart_dates=[FRI],
        counterpart_shifts=[ShiftCode.M],
    )
    with pytest.raises(tools.ToolError, match="not an open swap"):
        _find(world, "R-O4")
    ward = repo.get_ward(world)
    assert ward is not None
    repo.insert_request(
        world,
        ward.id,
        Request(
            id="R-O5",
            nurse_id="N03",
            kind=RequestKind.UNAVAILABLE,
            dates=[date(2026, 11, 3)],
            shifts=[ShiftCode.M],
            hard=True,
            status=RequestStatus.ACTIVE,
            created_at=NOW,
        ),
    )
    world.commit()
    with pytest.raises(tools.ToolError, match="not an open swap"):
        _find(world, "R-O5")
