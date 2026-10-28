"""Night block rules (5.2, Hospital A): N then M impossible, a 4th night in a row impossible,
the day after a block is off and labelled N/O."""

from dataclasses import replace

from app.solver.input import Forcing
from app.solver.solve import solve
from app.solver.validate import display_codes

from .tiny import nurse, tiny


def test_night_then_morning_is_impossible() -> None:
    inp = tiny(days=2, target={"M": 1, "N": 1}, forced=(Forcing("what_if", "A", 0, "N"),))
    res = solve(inp, stage_seconds=5, workers=1)
    assert res.roster[("A", 0)] == "N" and res.roster[("A", 1)] not in ("M", "E")
    forced_m = (Forcing("what_if", "A", 0, "N"), Forcing("what_if", "A", 1, "M"))
    inp = tiny(days=2, max_total={"N": 1}, forced=forced_m)
    assert solve(inp, stage_seconds=5, workers=1).status == "infeasible"


def test_fourth_night_in_a_row_is_impossible() -> None:
    four = tuple(Forcing("what_if", "A", d, "N") for d in range(4))
    assert (
        solve(tiny(days=4, target={"M": 0, "N": 1}, forced=four), stage_seconds=5, workers=1).status
        == "infeasible"
    )
    res = solve(tiny(days=4, target={"M": 0, "N": 1}), stage_seconds=5, workers=1)
    assert [res.roster[("A", d)] for d in range(4)].count("N") == 3
    assert res.stages[0].best == 1


def test_day_after_block_is_off_and_labelled_no() -> None:
    nurses = [nurse("A"), nurse("B")]
    block = tuple(Forcing("what_if", "A", d, "N") for d in range(3))
    inp = tiny(nurses, days=5, target={"M": 2, "N": 1}, forced=block)
    res = solve(inp, stage_seconds=5, workers=1)
    assert res.roster[("A", 3)] is None
    assert display_codes(inp, res.roster, inp.nurses[0])[:4] == ["N", "N", "N", "N/O"]
    # one N/O day is enough (Q15): a morning on the second day after the block is legal
    back = solve(
        replace(inp, forced=(*block, Forcing("what_if", "A", 4, "M"))), stage_seconds=5, workers=1
    )
    assert back.status == "solved" and back.roster[("A", 4)] == "M"


def test_ward_may_set_four_night_blocks() -> None:
    res = solve(
        tiny(days=4, target={"M": 0, "N": 1}, night_block_max=4), stage_seconds=5, workers=1
    )
    assert [res.roster[("A", d)] for d in range(4)] == ["N"] * 4
