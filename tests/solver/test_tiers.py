"""Priority tiers (5.3) and the lexicographic / weighted solves (5.4), M3."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.seeds import load_seed
from app.planner.build_input import build_input
from app.solver.input import DemandSpec, Pref, SolveInput
from app.solver.model import RosterModel
from app.solver.solve import SolveResult, solve, solve_lexicographic, solve_weighted

from .tiny import hist, nurse, tiny

ROOT = Path(__file__).resolve().parents[2]
GOLD = json.loads((ROOT / "tests/fixtures/mini_expected.json").read_text(encoding="utf-8"))
GOLDEN = GOLD["baseline"]["vector"]  # 3 / 0 / 0 / 19 / 55, every tier proven


@pytest.fixture(scope="module")
def mini() -> SolveInput:
    s = load_seed("mini")
    return build_input(s.ward, s.requests, s.history)


@pytest.fixture(scope="module")
def lex(mini: SolveInput) -> SolveResult:
    return solve_lexicographic(mini, stage_seconds=30, workers=8)


def test_mini_matches_golden_vector(lex: SolveResult) -> None:
    vec = lex.tier_vector()
    if lex.proven:
        assert vec == GOLDEN
    else:  # never better than the proven golden; "best found" may be worse
        first = next((t for t in vec if vec[t] != GOLDEN[t]), None)
        assert first is None or vec[first] > GOLDEN[first]
    assert {s.tier: s.proven for s in lex.stages}["coverage"]


def test_lexicographic_and_weighted_agree(mini: SolveInput, lex: SolveResult) -> None:
    w = solve_weighted(mini, seconds=60, workers=8)
    assert w.status == "solved" and w.proven and lex.proven
    assert w.tier_vector() == lex.tier_vector()


def test_cost_rows_sum_to_stage_best(lex: SolveResult) -> None:
    vec = lex.tier_vector()
    for st in lex.stages:
        assert vec[st.tier] == st.best
        if st.proven and st.status != "skip":
            assert st.best == st.bound == st.stage_best


def test_golden_requests_and_fairness_breakdown(lex: SolveResult) -> None:
    rules = {(t.tier, t.rule) for t in lex.term_values}
    assert ("fairness", "FAIR_NIGHTS") in rules and ("requests", "PREF_OFF") in rules
    spread = next(t for t in lex.term_values if t.rule == "FAIR_NIGHTS")
    assert "max" in spread.why and "min" in spread.why
    assert lex.denied["R-0002"] or lex.denied["R-0003"] or lex.denied["R-0004"]  # Saturday


def solved(inp: SolveInput) -> SolveResult:
    r = solve(inp, stage_seconds=5, workers=1)
    assert r.status == "solved"
    return r


def test_quick_return_is_penalised_across_the_border() -> None:
    inp = tiny(history={"A": hist(last_shifts=(None, None, "E"), consecutive_days=1)})
    r = solved(inp)  # one nurse, one morning: covering it costs a quick return (E -> M)
    assert r.roster[("A", 0)] == "M"
    assert [(t.rule, t.day) for t in r.term_values if t.tier == "requests"] == [
        ("QUICK_RETURN", -1)
    ]


def test_isolated_day_and_single_night() -> None:
    # only day 1 can be worked (one night): off, N, off
    inp = tiny(days=3, target={"M": 0, "N": 0})
    inp = replace(inp, demand={**inp.demand, (1, "N"): DemandSpec(0, 1, 1, 0)})
    r = solved(inp)
    assert [r.roster[("A", d)] for d in range(3)] == [None, "N", None]
    rules = sorted(t.rule for t in r.term_values if t.tier == "requests")
    assert rules == ["ISOLATED_DAY", "SINGLE_NIGHT"]


def test_split_weekend() -> None:
    # Saturday can be worked, Sunday cannot: one day of the weekend only
    inp = tiny(days=2, weekend_days=frozenset({0, 1}), target={"M": 0})
    inp = replace(inp, demand={**inp.demand, (0, "M"): DemandSpec(0, 1, 1, 0)})
    r = solved(inp)
    assert "SPLIT_WEEKEND" in {t.rule for t in r.term_values}


def test_contract_min_credits_leave() -> None:
    inp = tiny(
        [nurse(min_shifts=3)],
        days=3,
        target={"M": 0},
        max_total={"M": 1},
        leave=frozenset({("A", 0)}),
    )
    r = solved(inp)
    # with the leave day credited, working days 1 and 2 meets the minimum of 3
    assert r.tier_vector()["contract"] == 0
    assert r.roster[("A", 1)] and r.roster[("A", 2)]


def test_requests_and_denial() -> None:
    inp = tiny(
        [nurse("A"), nurse("B")],
        days=1,
        target={"M": 2},
        day_off_prefs=(Pref("R-1", "A", 0, None, 1),),
    )
    r = solved(inp)  # coverage outranks the day off
    assert r.tier_vector()["coverage"] == 0 and r.denied == {"R-1": True}
    assert [(t.rule, t.weight) for t in r.term_values if t.tier == "requests"] == [("PREF_OFF", 2)]


def test_stability_prefers_standby() -> None:
    a, s = nurse("A"), nurse("S", standby=True)
    inp = tiny(
        [a, s],
        days=1,
        previous={("A", 0): "M", ("S", 0): None},
        unavailable=frozenset({("A", 0, "M")}),
        standby_allowed=True,
    )
    r = solved(inp)
    assert r.roster[("S", 0)] == "M"
    assert [(t.rule, t.weight) for t in r.term_values if t.tier == "stability"] == [
        ("STANDBY_CALLED", 1)
    ]


def test_spreads_skip_standby_and_whole_period_leave() -> None:
    nurses = [nurse("A"), nurse("B"), nurse("L"), nurse("S", standby=True)]
    inp = tiny(
        nurses,
        days=2,
        target={"M": 1},
        leave=frozenset({("L", 0), ("L", 1)}),
        history={"A": hist(mornings=5), "B": hist(mornings=5), "L": hist(), "S": hist()},
    )
    rm = RosterModel(inp)
    for rule in ("FAIR_MORNINGS", "FAIR_NIGHTS", "FAIR_WEEKENDS", "FAIR_EVENINGS"):
        assert [n for n, _e in rm.spread_members.get(rule, [])] in (["A", "B"], []), rule
    assert solved(inp).status == "solved"
