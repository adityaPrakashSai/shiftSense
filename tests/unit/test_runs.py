"""run_full, evidence rows, decision log events, publish and replay (M3)."""

import sqlite3
import time
from collections import Counter
from pathlib import Path

import pytest

from app.config import get_settings
from app.domain.models import RequestStatus
from app.planner.build_input import build_input
from app.planner.runs import publish, run_full
from app.solver.input import SolveInput
from app.store import repo
from app.store.seed import seed_db
from app.tools.replay import main as replay_main


def published(conn: sqlite3.Connection, ward_id: str) -> dict[str, object]:
    run = repo.get_published_run(conn, ward_id)
    assert run is not None
    return run


def test_seed_publishes_a_solved_baseline(conn: sqlite3.Connection, db_path: Path) -> None:
    seed = seed_db(conn, "mini")
    run = published(conn, seed.ward.id)
    rid = str(run["run_id"])
    assert run["kind"] == "full" and run["trigger"] == "seed" and run["parent_run"] is None
    assert conn.execute("SELECT COUNT(*) FROM run WHERE status = 'published'").fetchone()[0] == 1

    stages = repo.get_stages(conn, rid)
    assert [s["tier"] for s in stages] == [
        "coverage",
        "stability",
        "contract",
        "requests",
        "fairness",
    ]
    assert all(s["proven"] for s in stages if s["tier"] in ("coverage", "stability", "contract"))

    costs = repo.get_costs(conn, rid)
    by_tier = Counter()
    for c in costs:
        by_tier[c["tier"]] += c["amount"] * c["weight"]
    for s in stages:  # cost rows sum to stage best, per tier
        assert by_tier.get(s["tier"], 0) == s["best"], s

    n_assign = conn.execute("SELECT COUNT(*) FROM assignment WHERE run_id = ?", (rid,)).fetchone()
    assert n_assign[0] == 9 * 14

    outcomes = {o["request_id"]: o for o in repo.get_request_outcomes(conn, rid)}
    active = repo.list_requests(conn, seed.ward.id, status=RequestStatus.ACTIVE)
    assert set(outcomes) == {r.id for r in active}
    assert outcomes["R-0001"]["status"] == "GRANTED" and outcomes["R-0001"]["reason_code"] == "HARD"
    for o in outcomes.values():
        if o["status"] == "DENIED":
            assert o["reason_code"].startswith("PREF_") and o["detail"].startswith("denied on")
    fair = repo.get_fairness(conn, rid)
    assert len(fair) == 9 and sum(f["total_shifts"] for f in fair) > 0

    events = repo.list_events(conn, seed.ward.id, run_id=rid)
    kinds = Counter(e["kind"] for e in events)
    assert kinds["model.built"] == 1 and kinds["run.started"] == 1
    assert kinds["run.proposed"] == 1 and kinds["run.published"] == 1
    solved_tiers = {s["tier"] for s in stages if s["status"] != "skip"}
    for kind in ("stage.start", "stage.improved", "stage.done"):
        tiers = {e["stage"].split(":", 1)[1] for e in events if e["kind"] == kind}
        assert solved_tiers <= tiers, kind
    done = [e for e in events if e["kind"] == "stage.done"]
    assert [e["stage"] for e in done] == [
        f"P{i}:{t}"
        for i, t in enumerate(["coverage", "stability", "contract", "requests", "fairness"], 1)
    ]

    logs = Path(get_settings().log_dir) / rid
    assert {p.name for p in logs.iterdir()} == {f"{t}.solver.log" for t in solved_tiers}

    # input_json round-trips to the SolveInput the planner built
    inp = build_input(seed.ward, active, repo.get_history(conn, seed.ward.id))
    assert SolveInput.from_json(str(run["input_json"])) == inp


def test_generate_then_publish_supersedes(conn: sqlite3.Connection) -> None:
    seed = seed_db(conn, "mini")
    base = str(published(conn, seed.ward.id)["run_id"])
    out = run_full(conn, seed.ward.id)
    run = repo.get_run(conn, out.run_id)
    assert run["status"] == "proposed" and run["parent_run"] == base
    assert publish(conn, out.run_id) == base
    assert repo.get_run(conn, base)["status"] == "superseded"
    assert str(published(conn, seed.ward.id)["run_id"]) == out.run_id
    with pytest.raises(ValueError):
        publish(conn, out.run_id)


def test_replay_reproduces_tier_values(conn: sqlite3.Connection, db_path: Path) -> None:
    seed = seed_db(conn, "mini")
    rid = str(published(conn, seed.ward.id)["run_id"])
    assert replay_main([rid, "--db", str(db_path), "--seconds", "10"]) == 0


def test_default_baseline_within_budget(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1 with the real budgets: coverage, stability and contract proven, total under 10 s."""
    monkeypatch.setenv("SOLVER_STAGE_SECONDS", "5")
    monkeypatch.setenv("SOLVER_TOTAL_SECONDS", "9")
    get_settings.cache_clear()
    t0 = time.perf_counter()
    seed = seed_db(conn, "default")
    elapsed = time.perf_counter() - t0
    run = published(conn, seed.ward.id)
    assert elapsed < 10 and float(run["total_secs"]) < 10
    stages = {s["tier"]: s for s in repo.get_stages(conn, str(run["run_id"]))}
    assert all(stages[t]["proven"] for t in ("coverage", "stability", "contract"))
    assert stages["coverage"]["best"] == 0
