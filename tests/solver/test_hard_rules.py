"""One test per hard-rule group (5.2, 11.3): on a tiny instance where the rule binds, the solved
roster passes the validator and respects the rule; dropping the group lets the violation appear,
which proves the test bites."""

from dataclasses import replace

import pytest

from app.solver.input import DemandSpec, Forcing, SolveInput
from app.solver.model import RosterModel
from app.solver.solve import new_solver, solve
from app.solver.validate import violations

from .tiny import hist, nurse, tiny


def solve_dropped(inp: SolveInput, group: str) -> tuple[str, dict[tuple[str, int], str | None]]:
    """Coverage-only solve with one hard group removed; returns status and roster."""
    rm = RosterModel(inp, drop_groups=frozenset({group}))
    rm.m.minimize(rm.tier_penalty("coverage"))
    s = new_solver(5, 1)
    st = s.solve(rm.m)
    return s.status_name(st), (rm.roster(s) if s.status_name(st) in ("OPTIMAL", "FEASIBLE") else {})


def check_bites(inp: SolveInput, group: str, expected: str) -> None:
    res = solve(inp, stage_seconds=5, workers=1)
    if res.status == "solved":
        assert violations(inp, res.roster) == []
    status, roster = solve_dropped(inp, group)
    assert status == "OPTIMAL"
    assert expected in violations(inp, roster), violations(inp, roster)


CASES = [
    ("unavailable", tiny(unavailable=frozenset({("A", 0, None)})), "unavailable:A:d0"),
    ("unavailable", tiny(unavailable=frozenset({("A", 0, "M")})), "unavailable:A:d0:M"),
    ("leave", tiny(leave=frozenset({("A", 0)})), "leave:A:d0"),
    (
        "night_qualified",
        tiny([nurse(can_night=False)], target={"M": 0, "N": 1}),
        "night_qualified:A:d0",
    ),
    (
        "night_exempt",
        tiny(target={"M": 0, "N": 1}, night_exempt=frozenset({("A", 0)})),
        "night_exempt:A:d0",
    ),
    ("standby_off_roster", tiny([nurse(standby=True)]), "standby_off_roster:A:d0"),
    ("ans_general_duty", tiny([nurse(role="ans")]), "ans_general_duty:A:d0"),
    (
        "rest_after_night",
        tiny(history={"A": hist(last_shifts=(None, None, "N"), consecutive_nights=1)}),
        "rest_after_night:A:d-1",
    ),
    (
        "rest_after_night",
        tiny(days=2, max_total={"N": 1}, forced=(Forcing("what_if", "A", 0, "N"),)),
        "rest_after_night:A:d0",
    ),
    ("max_consecutive_days", tiny(days=3, max_consecutive_days=2), "max_consecutive_days:A:d2"),
    (
        "max_consecutive_days",
        tiny(max_consecutive_days=2, history={"A": hist(consecutive_days=2)}),
        "max_consecutive_days:A:d0",
    ),
    (
        "max_consecutive_nights",
        tiny(days=3, target={"M": 0, "N": 1}, night_block_max=2),
        "max_consecutive_nights:A:d2",
    ),
    (
        "max_consecutive_nights",
        tiny(
            target={"M": 0, "N": 1},
            history={
                "A": hist(last_shifts=("N", "N", "N"), consecutive_days=3, consecutive_nights=3)
            },
        ),
        "max_consecutive_nights:A:d0",
    ),
    ("max_shifts", tiny([nurse(max_shifts=1)], days=2), "max_shifts:A"),
    ("max_nights", tiny(days=2, target={"M": 0, "N": 1}, max_nights=1), "max_nights:A"),
    ("max_hours", tiny([nurse(max_hours=8.0)], days=2), "max_hours:A"),
    (
        "max_cover",
        tiny([nurse("A"), nurse("B")], target={"M": 2}, max_total={"M": 1}),
        "max_cover:d0:M",
    ),
    ("frozen", tiny(previous={("A", 0): None}, frozen_before_day=1), "frozen:A:d0"),
    ("frozen", tiny(previous={("A", 0): None}, frozen_cells=frozenset({("A", 0)})), "frozen:A:d0"),
    ("forced", tiny(forced=(Forcing("what_if", "A", 0, None),)), "forced:what_if:A:d0"),
    ("fixed_duty", tiny(forced=(Forcing("fixed_duty", "A", 0, "G"),)), "fixed_duty:A:d0"),
]


@pytest.mark.parametrize(("group", "inp", "expected"), CASES, ids=[c[2] for c in CASES])
def test_rule_bites(group: str, inp: SolveInput, expected: str) -> None:
    check_bites(inp, group, expected)


def test_one_shift_per_day_bites() -> None:
    inp = tiny(target={"M": 1, "E": 1, "N": 1})
    res = solve(inp, stage_seconds=5, workers=1)
    assert res.stages[0].best == 2  # one nurse covers one of the three shifts
    rm = RosterModel(inp, drop_groups=frozenset({"one_shift_per_day"}))
    rm.m.minimize(rm.tier_penalty("coverage"))
    s = new_solver(5, 1)
    s.solve(rm.m)
    assert round(s.objective_value) == 0
    assert sum(s.value(v) for (n, d, _s), v in rm.x.items() if (n, d) == ("A", 0)) > 1


def test_min_cover_bites() -> None:
    inp = tiny(floor=1, unavailable=frozenset({("A", 0, None)}))
    assert solve(inp, stage_seconds=5, workers=1).status == "infeasible"
    status, roster = solve_dropped(inp, "min_cover")
    assert status == "OPTIMAL" and "min_cover:d0:M" in violations(inp, roster)


def test_senior_cover_bites() -> None:
    inp = tiny([nurse("A")])
    inp = replace(inp, demand={**inp.demand, (0, "M"): DemandSpec(1, 1, 1, 1)})
    assert solve(inp, stage_seconds=5, workers=1).status == "infeasible"
    status, roster = solve_dropped(inp, "senior_cover")
    assert status == "OPTIMAL" and "senior_cover:d0:M" in violations(inp, roster)


def test_waived_constraint_is_relaxed_and_named() -> None:
    inp = tiny(leave=frozenset({("A", 0)}), waived=frozenset({"leave:A:d0"}))
    res = solve(inp, stage_seconds=5, workers=1)
    assert res.roster[("A", 0)] == "M" and res.stages[0].best == 0
    assert "leave:A:d0" in RosterModel(inp).names


def test_guarded_mode_creates_one_literal_per_name() -> None:
    rm = RosterModel(tiny(days=2, leave=frozenset({("A", 1)})), guarded=True)
    assert set(rm.guards) == set(rm.names)
    assert "leave:A:d1" in rm.guards
