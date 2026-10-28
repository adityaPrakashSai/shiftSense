"""Sick-call flow and replacement ranking (design doc 6.4, 6.5b, 11.3)."""

import json
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.models import Request, RequestKind, RequestStatus, RunKind, RunStatus
from app.planner import requests as lifecycle
from app.planner.build_input import build_input
from app.planner.runs import new_run_id, publish, start_resolve
from app.planner.sickcall import rank
from app.store import repo
from app.store.seed import seed_db

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = json.loads((ROOT / "tests/fixtures/mini_expected.json").read_text())
NOW = datetime(2026, 11, 3, 6, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
TUE = date(2026, 11, 3)


def publish_fixture_baseline(conn: sqlite3.Connection) -> str:
    """Mini ward with the reference baseline roster as the published run (deterministic)."""
    seed_db(conn, "mini", solve=False)
    ward = repo.get_ward(conn)
    assert ward is not None
    active = [r for r in repo.list_requests(conn, ward.id) if r.status is RequestStatus.ACTIVE]
    inp = build_input(ward, active, repo.get_history(conn, ward.id))
    run_id = new_run_id()
    repo.insert_run(conn, run_id=run_id, ward_id=ward.id, kind=RunKind.FULL,
                    status=RunStatus.PROPOSED, trigger="fixture", horizon_start=ward.horizon_start,
                    priorities=inp.priorities, input_json=inp.to_json(),
                    input_hash=inp.input_hash())  # fmt: skip
    repo.insert_assignments(conn, run_id, EXPECTED["baseline"]["roster"], ward.horizon_start)
    repo.insert_stages(conn, run_id, [(1, "coverage", "OPTIMAL", 3, 3, 1, 0.1, None)])
    repo.set_published(conn, ward.id, run_id, "seed")
    return run_id


def sick(conn: sqlite3.Connection, nurse: str, days: list[date], shifts: list[str] | None) -> str:
    rid = repo.next_request_id(conn)
    r = Request(id=rid, nurse_id=nurse, kind=RequestKind.UNAVAILABLE, dates=days, shifts=shifts,
                hard=True, status=RequestStatus.ACTIVE, created_at=NOW)  # fmt: skip
    repo.insert_request(conn, "ward-mini", r)
    return rid


def test_scenario_a_ranking_matches_reference(conn: sqlite3.Connection) -> None:
    base = publish_fixture_baseline(conn)
    rid = sick(conn, "N03", [TUE], ["M"])
    plan = start_resolve(conn, trigger=f"request:{rid}", affected=[TUE], request_id=rid, now=NOW)
    out = rank(conn, plan)
    assert out.status is RunStatus.PROPOSED
    p = repo.get_proposal(conn, plan.run_id)
    assert p is not None
    rk = p["ranking"]
    ref = EXPECTED["scenarios"]["A-sick-morning"]["ranking"][0]
    assert rk["open_cell"] == {"nurse": "N03", "day": "2026-11-03", "shift": "M"}
    assert [c["nurse"] for c in rk["candidates"]] == [c["nurse"] for c in ref["ranked"]]
    for got, want in zip(rk["candidates"], ref["ranked"], strict=True):
        assert got["tiers"] == want["vector"] and got["changed_cells"] == want["changed_cells"]
    assert rk["excluded"] == [{"nurse": e["nurse"], "rules": e["rules"]} for e in ref["excluded"]]
    # ranking order follows the tier vectors in priority order
    vecs = [tuple(c["tiers"][t] for t in plan.inp.priorities) for c in rk["candidates"]]
    assert vecs == sorted(vecs)
    assert rk["candidates"][1]["reason_code"] == "TIER:stability"

    run = repo.get_run(conn, plan.run_id)
    assert run is not None and run["parent_run"] == base and run["kind"] == "resolve"
    assert run["changed_cells"] == 1 and run["status"] == "proposed"
    causes = {
        (r["nurse_id"], r["day"]): r["cause"]
        for r in conn.execute(
            "SELECT * FROM assignment WHERE run_id = ? AND changed = 1", (plan.run_id,)
        )  # fmt: skip
    }
    assert causes == {("N03", "2026-11-03"): "request", ("S01", "2026-11-03"): "replacement"}
    kinds = [e["kind"] for e in repo.list_events(conn, "ward-mini", run_id=plan.run_id)]
    assert "candidates.ranked" in kinds and "run.proposed" in kinds

    # choosing the second candidate publishes exactly its stored run
    second = rk["candidates"][1]
    publish(conn, second["run_id"])
    assert repo.get_published_run(conn, "ward-mini")["run_id"] == second["run_id"]  # type: ignore[index]
    assert repo.get_proposal(conn, plan.run_id)["chosen_nurse"] == "N05"  # type: ignore[index]
    assert repo.get_run(conn, plan.run_id)["status"] == "superseded"  # type: ignore[index]


def test_demand_change_ranks_the_added_slot(conn: sqlite3.Connection) -> None:
    """Scenario C (6.5b): Thursday evening target 2 -> 3; a standby nurse is called in."""
    publish_fixture_baseline(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    thu = date(2026, 11, 5)
    req = Request(id="R-0200", nurse_id="charge", kind=RequestKind.DEMAND, dates=[thu],
                  shifts=["E"], target_total=3, hard=False,
                  status=RequestStatus.PENDING_CONFIRM, created_at=NOW)  # fmt: skip
    repo.insert_request(conn, ward.id, req)
    assert lifecycle.confirm(conn, ward, "R-0200").solve
    t0 = time.perf_counter()
    plan = start_resolve(conn, trigger="request:R-0200", affected=[thu], now=NOW)
    assert plan.added_slots == ((10, "E"),)
    rank(conn, plan)
    assert time.perf_counter() - t0 < 3
    rk = repo.get_proposal(conn, plan.run_id)["ranking"]  # type: ignore[index]
    ref = EXPECTED["scenarios"]["C-demand-up"]
    assert rk["open_cell"] == {"nurse": None, "day": "2026-11-05", "shift": "E"}
    top = rk["candidates"][0]
    assert top["tiers"] == ref["vector"] and top["changed_cells"] == ref["changed_cells"] == 1
    assert top["standby"] and top.get("tie")  # S01 and S02 are equally good
    assert repo.get_run(conn, plan.run_id)["frozen_before_day"] == 9  # type: ignore[index]


def test_no_open_cell_runs_a_plain_resolve(conn: sqlite3.Connection) -> None:
    publish_fixture_baseline(conn)
    rid = repo.next_request_id(conn)
    r = Request(id=rid, nurse_id="N02", kind=RequestKind.DAY_OFF, dates=[date(2026, 11, 7)],
                hard=False, status=RequestStatus.ACTIVE, created_at=NOW)  # fmt: skip
    repo.insert_request(conn, "ward-mini", r)
    plan = start_resolve(conn, trigger=f"request:{rid}", affected=r.dates, request_id=rid, now=NOW)
    rank(conn, plan)
    rk = repo.get_proposal(conn, plan.run_id)["ranking"]  # type: ignore[index]
    assert rk["open_cell"] is None and rk["candidates"] == []


def test_seed_sick_call_under_three_seconds(conn: sqlite3.Connection) -> None:
    """The demo story on the default 24-nurse ward: a Morning sick call on Tue 3 Nov at 06:05."""
    seed_db(conn, "default")
    ward = repo.get_ward(conn)
    pub = repo.get_published_run(conn, "ward-7")
    assert ward is not None and pub is not None
    grid = repo.get_roster(conn, pub["run_id"])
    d = 8  # 3 Nov; Meena (N07) if she works the Morning, else the first nurse who does
    nurse = next(n for n in ["N07", *sorted(grid)] if grid[n][d] == "M")
    rid = repo.next_request_id(conn)
    r = Request(id=rid, nurse_id=nurse, kind=RequestKind.UNAVAILABLE, dates=[TUE], shifts=["M"],
                hard=True, status=RequestStatus.ACTIVE, created_at=NOW)  # fmt: skip
    repo.insert_request(conn, ward.id, r)
    t0 = time.perf_counter()
    plan = start_resolve(conn, trigger=f"request:{rid}", affected=[TUE], request_id=rid, now=NOW)
    rank(conn, plan)
    secs = time.perf_counter() - t0
    run = repo.get_run(conn, plan.run_id)
    rk = repo.get_proposal(conn, plan.run_id)["ranking"]  # type: ignore[index]
    assert secs < 3, secs
    assert run is not None and run["changed_cells"] <= 4
    assert rk["candidates"] and all(e["rules"] for e in rk["excluded"])
