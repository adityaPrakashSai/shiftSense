from dataclasses import replace
from datetime import date

import pytest

from app.domain.models import Request, RequestKind, RequestStatus, ShiftCode
from app.planner.build_input import build_input, scaled_contract, scaled_max_nights
from app.solver.input import Forcing, SolveInput
from app.store.seed import load_seed

SEEDS = ["default", "mini", "real_a"]


def _inp(name: str, **kw: bool) -> SolveInput:
    s = load_seed(name)
    return build_input(s.ward, s.requests, s.history, **kw)


@pytest.mark.parametrize("name", SEEDS)
def test_solve_input_round_trips_through_json(name: str) -> None:
    inp = _inp(name)
    back = SolveInput.from_json(inp.to_json())
    assert back == inp
    assert back.to_json() == inp.to_json()
    assert back.input_hash() == inp.input_hash()


def test_round_trip_with_resolve_fields() -> None:
    inp = _inp("mini")
    inp = replace(
        inp,
        previous={("N01", 0): "M", ("N01", 1): None, ("N02", 0): "N"},
        frozen_before_day=8,
        frozen_cells=frozenset({("N02", 8)}),
        waived=frozenset({"min_cover:d12:N"}),
        forced=(*inp.forced, Forcing("what_if", "S01", 8, "M")),
        unavailable=inp.unavailable | {("N03", 8, "M")},
        standby_allowed=True,
    )
    assert SolveInput.from_json(inp.to_json()) == inp


@pytest.mark.parametrize("name", ["default", "mini"])
def test_supply_brackets_demand(name: str) -> None:
    """Σ min_shifts <= Σ target slots <= Σ max_shifts over the nurses a full run may use."""
    inp = _inp(name)
    regular = [n for n in inp.nurses if not n.standby]
    target = sum(d.target_total for d in inp.demand.values())
    assert sum(n.min_shifts for n in regular) <= target <= sum(n.max_shifts for n in regular)


def test_default_ward_numbers() -> None:
    inp = _inp("default")
    assert sum(d.target_total for d in inp.demand.values()) == 224  # 4.1
    assert inp.weekend_days == frozenset({5, 6, 12, 13})
    assert inp.max_nights == 3 and inp.night_block_max == 3 and inp.max_consecutive_days == 6
    regular = {(n.min_shifts, n.max_shifts) for n in inp.nurses if not n.standby}
    assert regular == {(11, 12)}
    assert {(n.min_shifts, n.max_shifts) for n in inp.nurses if n.standby} == {(0, 4)}
    assert inp.demand[(0, "M")].target_total == 8 and inp.demand[(2, "M")].target_total == 7
    assert inp.demand[(5, "M")].target_total == 6 and inp.demand[(5, "M")].max_total == 6
    assert {("N04", 2), ("N04", 3)} <= inp.leave and len(inp.leave) == 6
    assert not inp.standby_allowed and inp.forced == ()
    assert {(p.nurse, p.day, p.weight) for p in inp.day_off_prefs if p.nurse == "N01"} == {
        ("N01", 5, 2)
    }
    avoid = [p for p in inp.shift_avoids if p.nurse == "N09"]
    assert [p.day for p in avoid] == list(range(7, 14)) and {p.weight for p in avoid} == {5}
    assert inp.history["N08"].last_shifts == ("N", "N", "N")
    assert inp.history["N08"].consecutive_nights == 3


def test_mini_matches_reference_scaling() -> None:
    inp = _inp("mini")
    assert inp.max_nights == 3
    assert sum(d.target_total for d in inp.demand.values()) == 84
    assert ("S02", 8, None) in inp.unavailable
    assert inp.history["N05"].last_shifts == ("N", "N", "N")
    assert inp.history["N02"].consecutive_days == 6


def test_real_a_fixed_duty_and_leave() -> None:
    s = load_seed("real_a")
    inp = build_input(s.ward, s.requests, s.history)
    assert inp.max_nights == 7  # ceil(6 x 31 / 30)
    ans = {f.day for f in inp.forced if f.nurse == "ANS"}
    assert all(f.shift == "G" for f in inp.forced if f.nurse == "ANS")
    # 1 Oct 2026 is a Thursday; ANS works G Mon-Sat except on her leave days (2 and 20 Oct)
    mon_sat = {d for d in range(31) if date(2026, 10, 1 + d).weekday() < 6}
    assert ans == mon_sat - {1, 19}
    assert inp.shift_hours["G"] == 8
    assert ("N07", 0) in inp.leave and ("N07", 30) in inp.leave  # ML for the whole month


def test_scaling_helpers() -> None:
    assert scaled_contract(5.5, 6.0, 14) == (11, 12)
    assert scaled_contract(5.5, 6.0, 7) == (5, 6)
    assert scaled_contract(5.0, 6.0, 31) == (22, 27)
    assert [scaled_max_nights(6, d) for d in (7, 14, 28, 31)] == [2, 3, 6, 7]


def test_only_active_requests_and_horizon_clipping() -> None:
    s = load_seed("default")
    extra = [
        Request(
            id="R-0100",
            nurse_id="N03",
            kind=RequestKind.LEAVE,
            hard=True,
            dates=[date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10)],
            status=RequestStatus.ACTIVE,
            created_at="2026-10-27T00:00:00Z",
        ),
        Request(
            id="R-0101",
            nurse_id="N03",
            kind=RequestKind.UNAVAILABLE,
            hard=True,
            dates=[date(2026, 10, 27)],
            shifts=["M"],
            status=RequestStatus.PENDING_CONFIRM,
            created_at="2026-10-27T00:00:00Z",
        ),
    ]
    inp = build_input(s.ward, [*s.requests, *extra], s.history)
    assert {d for n, d in inp.leave if n == "N03"} == {13}  # 9-10 Nov carry into next period
    assert not any(n == "N03" for n, _, _ in inp.unavailable)  # pending is not used


def test_night_exempt_and_fixed_in_charge() -> None:
    s = load_seed("default")
    nurses = list(s.ward.nurses)
    nurses[0] = nurses[0].model_copy(
        update={
            "night_exempt_until": date(2026, 10, 30),
            "fixed_duty": {0: ShiftCode.M, 2: ShiftCode.M},
        }
    )
    ward = s.ward.model_copy(update={"nurses": nurses})
    inp = build_input(ward, s.requests, s.history)
    assert {d for n, d in inp.night_exempt if n == "N01"} == {0, 1, 2, 3, 4}
    assert [(f.day, f.shift) for f in inp.forced] == [(0, "M"), (2, "M"), (7, "M"), (9, "M")]
