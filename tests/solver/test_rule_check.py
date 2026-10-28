"""rule_check agrees with the full validator on 200 random single-cell edits (M2)."""

import random

import pytest

from app.domain.seeds import load_seed
from app.planner.build_input import build_input
from app.solver.input import SolveInput
from app.solver.rules import rule_check
from app.solver.solve import solve
from app.solver.validate import violations


@pytest.mark.parametrize("seed_name", ["default", "real_a"])
def test_rule_check_agrees_with_validator(seed_name: str) -> None:
    s = load_seed(seed_name)
    inp: SolveInput = build_input(s.ward, s.requests, s.history)
    roster = solve(inp, stage_seconds=5, workers=8).roster
    before = set(violations(inp, roster))
    rng = random.Random(7)
    nurses = [n.id for n in inp.nurses]
    for _ in range(200):
        n, d = rng.choice(nurses), rng.randrange(inp.num_days)
        shift = rng.choice(["M", "E", "N", "G", None])
        edited = dict(roster)
        edited[n, d] = shift
        expected = sorted(set(violations(inp, edited)) - before)
        assert rule_check(roster, inp, n, d, shift) == expected, (n, d, shift)


def test_rule_check_names_the_blocking_rule() -> None:
    s = load_seed("mini")
    inp = build_input(s.ward, s.requests, s.history)
    roster = solve(inp, stage_seconds=5, workers=4).roster
    # N05 ended last period with three nights: a day-0 morning breaks rest after night
    assert "rest_after_night:N05:d-1" in rule_check(roster, inp, "N05", 0, "M")
    assert rule_check(roster, inp, "N05", 0, roster[("N05", 0)]) == []
