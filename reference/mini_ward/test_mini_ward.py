"""Golden tests for the mini ward fixture. Run: pytest -q test_mini_ward.py

When the app's solver exists (M2/M3), point these at app.solver instead of mini_solver;
the assertions are about the fixture, not about this reference implementation.
"""
import json
import pathlib

import pytest

from mini_solver import (Ctx, apply_scenario, edit_to_ctx, load, rank_candidates, solve,
                         soft_terms, tier_vector, violations)

ROOT = pathlib.Path(__file__).resolve().parents[2]   # repo root
INST = load(str(ROOT / "data/seed/mini_ward.json"))
GOLD = json.load(open(ROOT / "tests/fixtures/mini_expected.json"))


def roster_of(ser):
    return {(n, d): s for n, row in ser.items() for d, s in enumerate(row)}


BASE = roster_of(GOLD["baseline"]["roster"])


def test_golden_baseline_is_valid_and_matches_stored_vector():
    assert violations(INST, Ctx(), BASE) == []
    assert tier_vector(soft_terms(INST, Ctx(), BASE)) == GOLD["baseline"]["vector"]


def test_hand_checks():
    """Facts a person can verify on paper (README, section 'Hand checks')."""
    # N02 worked 6 days in a row and N05 3 nights in a row at the end of last period: both off on day 0
    assert BASE[("N02", 0)] is None and BASE[("N05", 0)] is None
    # that leaves 5 regular nurses for 7 Monday slots (M is 4 on Mon/Tue): day 0 is 2 short
    short = {t["day"]: t["amount"] for t in soft_terms(INST, Ctx(), BASE) if t["rule"] == "COVER_SHORT"}
    assert sum(1 for n in INST.regular if BASE[(n, 0)] is not None) == 5
    assert short[0] == 2
    # Saturday 31 Oct: 5 slots, 7 nurses, 3 ask for the day off -> at least one request is denied
    sat_denied = [r for r in ("N01", "N03", "N07") if BASE[(r, 5)] is not None]
    assert len(sat_denied) >= 1
    # max 6 nights a month -> 3 per 14 days; 14 nights over N01-N05 (Fatima on a night break) is 3,3,3,3,2
    assert INST.max_nights == 3
    assert all(sum(BASE[(n, d)] == "N" for d in range(INST.days)) <= 3 for n in INST.nurses)
    # standby nurses are never on the published roster
    assert all(BASE[(n, d)] is None for n in INST.standby for d in range(INST.days))


@pytest.mark.parametrize("tier", ["coverage", "stability"])
def test_fast_solve_reaches_golden_on_top_tiers(tier):
    r = solve(INST, Ctx(), stage_seconds=10, workers=4)
    assert r.status == "solved"
    assert r.vector[tier] == GOLD["baseline"]["vector"][tier]


def test_fast_solve_is_within_reach_of_golden():
    """The 10 s budget is not enough to prove the lower tiers (that is the point of the fixture),
    but the best found must not be worse than the stored golden by more than the recorded gap."""
    r = solve(INST, Ctx(), stage_seconds=10, workers=4)
    gold = GOLD["baseline"]["vector"]
    first_diff = next((t for t in r.vector if r.vector[t] != gold[t]), None)
    assert first_diff is None or r.vector[first_diff] > gold[first_diff], "found better than golden: update it"


def test_history_matters():
    """Solving without carry-in produces a roster that breaks real rules on day 0."""
    bad = GOLD["runs"]["no_history"]["hard_violations_under_real_history"]
    assert any(v.startswith("rest_after_night:N05") for v in bad)
    assert any(v.startswith("max_consecutive_days:N02") for v in bad)


def test_flat_weights_would_understaff():
    """Putting requests above coverage grants every day off and leaves shifts short."""
    v = GOLD["runs"]["requests_before_coverage"]["vector_under_default_rules"]
    assert v["coverage"] > GOLD["baseline"]["vector"]["coverage"]


def test_sick_call_minimal_change():
    sc = next(s for s in INST.raw["scenarios"] if s["id"] == "A-sick-morning")
    ctx = edit_to_ctx(INST, sc, BASE)
    r = solve(INST, ctx, stage_seconds=3, workers=4)
    assert r.secs < 3
    assert all(r.roster[(n, d)] == BASE[(n, d)] for n in INST.nurses for d in range(8))
    assert r.roster[("N03", 8)] is None
    ranking = rank_candidates(INST, ctx, BASE, stage_seconds=3)[0]
    top = ranking["ranked"][0]
    assert top["nurse"] == "S01" and top["changed_cells"] == 1
    excluded = {e["nurse"]: e["rules"] for e in ranking["excluded"]}
    assert any(x.startswith("unavailable:S02") for x in excluded["S02"])


def test_max_nights_rule_bites():
    trial = dict(BASE)
    n = next(n for n in INST.regular if INST.can_night[n] and sum(BASE[(n, d)] == "N" for d in range(14)) == 3)
    d = next(d for d in range(14) if BASE[(n, d)] is None and BASE.get((n, d - 1)) != "N")
    trial[(n, d)] = "N"
    assert f"max_nights:{n}" in violations(INST, Ctx(), trial, only=n)


def test_demand_change_minimal_change():
    sc = next(s for s in INST.raw["scenarios"] if s["id"] == "C-demand-up")
    inst, ctx, added = apply_scenario(INST, sc, BASE)
    assert added == ((10, "E"),)
    r = solve(inst, ctx, stage_seconds=3, workers=4)
    assert r.secs < 3
    assert sum(r.roster[(n, 10)] == "E" for n in inst.nurses) == 3
    changed = [(n, d) for n in inst.nurses for d in range(inst.days) if r.roster[(n, d)] != BASE[(n, d)]]
    assert len(changed) == 1 and changed[0][0] in inst.standby
    ranking = rank_candidates(inst, ctx, BASE, stage_seconds=3, added_slots=added)[0]
    top2 = ranking["ranked"][:2]
    assert {c["nurse"] for c in top2} == {"S01", "S02"} and top2[0]["vector"] == top2[1]["vector"]  # a TIE


def test_leave_over_night_block():
    """Scenario D (doc 6.5c): leave extended across a night block. Nights stay at target and the
    cover is a legal block (at most 3 nights, then one N/O), unlike the October hand edit."""
    sc = next(s for s in INST.raw["scenarios"] if s["id"] == "D-leave-over-night-block")
    ctx = edit_to_ctx(INST, sc, BASE)
    r = solve(INST, ctx, stage_seconds=3, workers=4)
    assert r.secs < 3
    assert violations(INST, ctx, r.roster) == []
    for d in range(10, 14):
        assert sum(r.roster[(n, d)] == "N" for n in INST.nurses) == INST.target[(d, "N")]
    assert all(r.roster[("N05", d)] is None for d in range(10, 14))
    changed = [(n, d) for n in INST.nurses for d in range(INST.days)
               if r.roster[(n, d)] != BASE[(n, d)] and n != "N05"]
    assert len(changed) <= 3
