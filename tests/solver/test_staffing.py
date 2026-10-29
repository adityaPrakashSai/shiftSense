"""Staffing check (design doc 6.7, M14): the mini-ward runs A-F reproduce the shortfall, headcount
and standby values in reference/mini_ward/staffing/staffing_results.json (read from that file, so
they stay right if the mini ward changes and the prototype is rerun), and hires are used in order.
The balance stage is either proven or reported as best found with its bound."""

import json
from pathlib import Path

import pytest

from app.domain.models import RequestStatus
from app.domain.seeds import load_seed
from app.planner.build_input import build_input
from app.solver.input import SolveInput
from app.solver.staffing import StaffingResult, check, with_hires, without

RESULTS = Path(__file__).resolve().parents[2] / "reference" / "mini_ward" / "staffing"
EXPECTED = json.loads((RESULTS / "staffing_results.json").read_text(encoding="utf-8"))
SECS = 3.0


def mini() -> tuple[SolveInput, list[str]]:
    seed = load_seed("mini")
    active = [r for r in seed.requests if r.status is RequestStatus.ACTIVE]
    inp = build_input(seed.ward, active, seed.history)
    return inp, [n.id for n in inp.nurses if not n.standby]


def run(key: str) -> StaffingResult:
    inp, staff = mini()
    if key == "A":
        return check(inp, fixed=staff, seconds=SECS)
    if key == "B":
        return check(with_hires(inp, 3), fixed=staff, seconds=SECS)
    if key == "C":
        return check(with_hires(inp, 3), seconds=SECS)
    if key == "D":
        return check(with_hires(inp, 3), cover="floor", seconds=SECS)
    out = {"E": {"N04"}, "F": {"N04", "N07"}}[key]
    return check(without(inp, out), allow_standby=True,
                 fixed=[n for n in staff if n not in out], seconds=SECS)  # fmt: skip


@pytest.mark.parametrize("key", list("ABCDEF"))
def test_mini_ward_runs_match_the_prototype(key: str) -> None:
    want = next(v for k, v in EXPECTED.items() if k.startswith(f"{key}."))
    res = run(key)
    assert res.status == "solved"
    for stage in ("shortfall", "nurses_used", "standby_shifts"):
        exp = next(s for s in want["stages"] if s["stage"] == stage)
        got = next(s for s in res.stages if s.stage == stage)
        assert (got.value, got.proven) == (exp["value"], True), (key, stage)
    bal = res.stages[-1]
    assert bal.stage == "balance" and (bal.proven or bal.bound is not None)
    assert len(res.used) == res.value("nurses_used")
    assert sum(t - g for _, _, g, t in res.short_cells) == res.value("shortfall") or key == "D"


def test_hires_are_used_in_order() -> None:
    res = run("B")  # two of three hires needed
    hires = [n for n in res.used if n.startswith("H")]
    assert hires == ["H1", "H2"]


def test_removed_nurses_never_work() -> None:
    res = run("F")
    assert not {"N04", "N07"} & set(res.load)
