"""Minimal-change re-solve (design doc 6.1-6.3, 11.3) against the golden mini-ward scenarios.

Every scenario re-solves from the fixture's published baseline (tests/fixtures/mini_expected.json)
and must reproduce the reference tier vector and change set exactly (all proven).
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from app.domain.models import Request, RequestKind, RequestStatus
from app.domain.seeds import load_seed
from app.planner.build_input import build_input, day_index
from app.solver.changes import change_set, count_changed
from app.solver.input import SolveInput
from app.solver.solve import solve
from app.solver.validate import violations

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = json.loads((ROOT / "tests/fixtures/mini_expected.json").read_text())
SCENARIOS = {
    s["id"]: s
    for s in json.loads((ROOT / "data/seed/mini_ward.json").read_text(encoding="utf-8"))[
        "scenarios"
    ]
}
BASE = {(n, d): s for n, row in EXPECTED["baseline"]["roster"].items() for d, s in enumerate(row)}


@cache
def scenario_input(sid: str) -> tuple[SolveInput, tuple[tuple[int, str], ...]]:
    seed = load_seed("mini")
    ward = seed.ward.model_copy(deep=True)
    requests = list(seed.requests)
    e = SCENARIOS[sid]["edit"]
    added: list[tuple[int, str]] = []
    if e["op"] == "change_demand":
        for dm in ward.demand:
            if dm.day.isoformat() in e["dates"] and dm.shift.value in e["shifts"]:
                added += [(day_index(ward, dm.day), dm.shift.value)] * (
                    e["target_total"] - dm.target_total
                )
                dm.target_total = e["target_total"]
    else:
        kind = RequestKind.UNAVAILABLE if e["op"] == "mark_unavailable" else RequestKind.LEAVE
        requests.append(
            Request(
                id="R-9000",
                nurse_id=e["nurse"],
                kind=kind,
                dates=e["dates"],
                shifts=e["shifts"],
                hard=True,
                status=RequestStatus.ACTIVE,
                created_at=datetime.now(UTC),
            )  # fmt: skip
        )
    inp = build_input(ward, requests, seed.history, standby_allowed=True)
    inp = replace(inp, previous=BASE, frozen_before_day=SCENARIOS[sid]["frozen_before_day"])
    return inp, tuple(added)


def changes_json(inp: SolveInput, roster: Any, added: Any) -> list[dict[str, Any]]:
    return [
        {"nurse": c.nurse, "day": c.day, "from": c.before, "to": c.after, "cause": c.cause}
        for c in change_set(inp, roster, added)
    ]


def key(c: dict[str, Any]) -> tuple[Any, ...]:
    return (c["day"], c["nurse"])


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_scenario_matches_reference(sid: str) -> None:
    inp, added = scenario_input(sid)
    res = solve(inp, stage_seconds=5, workers=8)
    exp = EXPECTED["scenarios"][sid]
    assert res.status == "solved" and res.proven
    assert res.tier_vector() == exp["vector"]
    assert sorted(changes_json(inp, res.roster, added), key=key) == sorted(exp["changes"], key=key)
    assert count_changed(change_set(inp, res.roster, added)) == exp["changed_cells"]
    assert res.changed_cells == exp["changed_cells"]  # the solver counts the same way (6.3)
    assert violations(inp, res.roster) == []
    # frozen days are identical to the published roster
    assert all(res.roster[n, d] == BASE[n, d] for (n, d) in BASE if d < inp.frozen_before_day)


def test_removed_cell_is_off_and_standby_covers() -> None:
    inp, _ = scenario_input("A-sick-morning")
    res = solve(inp, stage_seconds=5, workers=8)
    assert res.roster["N03", 8] is None and res.roster["S01", 8] == "M"


def test_scenario_d_keeps_nights_at_target_and_blocks_legal() -> None:
    inp, _ = scenario_input("D-leave-over-night-block")
    res = solve(inp, stage_seconds=5, workers=8)
    for d in range(10, 14):
        on = sum(res.roster[n.id, d] == "N" for n in inp.nurses)
        assert on == inp.demand[d, "N"].target_total, d
    assert violations(inp, res.roster) == []  # max block and N/O rest are hard rules


def test_stability_tier_keeps_changes_down() -> None:
    """With vs without the stability tier on every scenario (the write-up number, decisions.md)."""
    for sid in SCENARIOS:
        inp, added = scenario_input(sid)
        with_s = solve(inp, stage_seconds=5, workers=8)
        no_tier = tuple(t for t in inp.priorities if t != "stability")
        without = solve(replace(inp, priorities=no_tier), stage_seconds=5, workers=8)
        a = count_changed(change_set(inp, with_s.roster, added))
        b = count_changed(change_set(inp, without.roster, added))
        assert a <= b, (sid, a, b)


def test_frozen_cells_and_started_shifts() -> None:
    from zoneinfo import ZoneInfo

    from app.planner.runs import frozen_point

    seed = load_seed("mini")
    inp, _ = scenario_input("A-sick-morning")
    at = datetime(2026, 11, 3, 15, 30, tzinfo=ZoneInfo("Asia/Kolkata"))  # M and E under way
    point, cells, blocked = frozen_point(seed.ward, inp, BASE, at, [date(2026, 11, 3)])
    assert point == 8
    assert cells == {(n, 8) for (n, d), s in BASE.items() if d == 8 and s in ("M", "E")}
    assert ("S01", 8, "M") in blocked and ("S01", 8, "N") not in blocked
    later, _, _ = frozen_point(seed.ward, inp, BASE, at, [date(2026, 11, 5)])
    assert later == 9  # the earliest affected shift is not today: freeze through today
