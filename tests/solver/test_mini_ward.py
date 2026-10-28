"""Port of reference/mini_ward/test_mini_ward.py for M2 (hard rules, coverage tier, day-0
carry-in). The tier, sick-call and demand-change goldens are ported in M3 and M7."""

import json
from pathlib import Path

import pytest

from app.domain.seeds import load_seed
from app.planner.build_input import build_input
from app.solver.input import SolveInput
from app.solver.solve import SolveResult, solve
from app.solver.validate import violations

ROOT = Path(__file__).resolve().parents[2]
GOLD = json.loads((ROOT / "tests/fixtures/mini_expected.json").read_text(encoding="utf-8"))


def roster_of(ser: dict[str, list[str | None]]) -> dict[tuple[str, int], str | None]:
    return {(n, d): s for n, row in ser.items() for d, s in enumerate(row)}


BASE = roster_of(GOLD["baseline"]["roster"])


@pytest.fixture(scope="module")
def inp() -> SolveInput:
    s = load_seed("mini")
    return build_input(s.ward, s.requests, s.history)


@pytest.fixture(scope="module")
def solved(inp: SolveInput) -> SolveResult:
    return solve(inp, stage_seconds=10, workers=8, total_seconds=4)


def test_golden_baseline_passes_our_validator(inp: SolveInput) -> None:
    assert violations(inp, BASE) == []


def test_coverage_matches_golden(solved: SolveResult) -> None:
    assert solved.status == "solved"
    assert solved.stages[0].tier == "coverage" and solved.stages[0].proven
    assert solved.stages[0].best == GOLD["baseline"]["vector"]["coverage"] == 3


def test_day0_carry_in(inp: SolveInput, solved: SolveResult) -> None:
    """N02 ended last period with 6 days in a row and N05 with 3 nights: both off on day 0,
    which leaves 5 regular nurses for 7 Monday slots, so day 0 is 2 short."""
    r = solved.roster
    assert r[("N02", 0)] is None and r[("N05", 0)] is None
    regular = [n.id for n in inp.nurses if not n.standby]
    assert sum(r[(n, 0)] is not None for n in regular) == 5
    short0 = sum(t.amount for t in solved.term_values if t.rule == "COVER_SHORT" and t.day == 0)
    assert short0 == 2


def test_hand_checks(inp: SolveInput, solved: SolveResult) -> None:
    r = solved.roster
    assert inp.max_nights == 3
    assert all(sum(r[(n.id, d)] == "N" for d in range(14)) <= 3 for n in inp.nurses)
    assert all(r[(n.id, d)] is None for n in inp.nurses if n.standby for d in range(14))
    assert r[("N04", 2)] is None and r[("N04", 3)] is None  # approved leave


def test_history_matters(inp: SolveInput) -> None:
    """The roster solved without carry-in breaks real rules on day 0 under the real history."""
    roster = roster_of(GOLD["runs"]["no_history"]["roster"])
    bad = violations(inp, roster)
    assert "rest_after_night:N05:d-1" in bad
    expected = GOLD["runs"]["no_history"]["hard_violations_under_real_history"]
    want = {v for v in expected if not v.startswith("rest_after_night")}
    got = {":".join(v.split(":")[:2]) for v in bad if v.startswith("max_consecutive_days")}
    assert got == want


def test_max_nights_rule_bites(inp: SolveInput) -> None:
    trial = dict(BASE)
    regular = [n.id for n in inp.nurses if not n.standby and n.can_night]
    n = next(n for n in regular if sum(BASE[(n, d)] == "N" for d in range(14)) == 3)
    d = next(d for d in range(14) if BASE[(n, d)] is None and BASE.get((n, d - 1)) != "N")
    trial[(n, d)] = "N"
    assert f"max_nights:{n}" in violations(inp, trial)


def test_other_golden_runs_are_valid(inp: SolveInput) -> None:
    for key, run in GOLD["runs"].items():
        if not run["hard_violations_under_real_history"]:
            assert violations(inp, roster_of(run["roster"])) == [], key
