"""Builder for tiny hand-made SolveInputs used by the hard-rule tests."""

from dataclasses import replace
from typing import Any

from app.solver.input import SHIFTS, DemandSpec, NurseHistory, NurseSpec, SolveInput


def nurse(nid: str = "A", **kw: Any) -> NurseSpec:
    base: dict[str, Any] = dict(
        id=nid,
        grade="staff",
        role="staff",
        standby=False,
        can_night=True,
        min_shifts=0,
        max_shifts=99,
    )
    return NurseSpec(**{**base, **kw})


def tiny(
    nurses: list[NurseSpec] | None = None,
    days: int = 1,
    target: dict[str, int] | None = None,
    floor: int = 0,
    max_total: dict[str, int] | None = None,
    **kw: Any,
) -> SolveInput:
    """`target` per shift for every day (default M=1, E=0, N=0); hard floor `floor` everywhere."""
    nurses = nurses or [nurse()]
    t = {"M": 1, "E": 0, "N": 0, **(target or {})}
    mx = max_total or {}
    demand = {
        (d, s): DemandSpec(min(floor, t[s]), t[s], mx.get(s, max(t[s], 0)), 0)
        for d in range(days)
        for s in SHIFTS
    }
    inp = SolveInput(
        nurses=tuple(nurses),
        num_days=days,
        weekend_days=frozenset(),
        shift_hours={"M": 8.0, "E": 8.0, "N": 8.0, "G": 8.0},
        demand=demand,
        unavailable=frozenset(),
        leave=frozenset(),
        night_exempt=frozenset(),
        day_off_prefs=(),
        shift_prefs=(),
        shift_avoids=(),
        forced=(),
        waived=frozenset(),
        max_consecutive_days=6,
        night_block_min=2,
        night_block_max=3,
        max_nights=99,
        history={n.id: NurseHistory() for n in nurses},
    )
    return replace(inp, **kw)


def hist(**kw: Any) -> NurseHistory:
    return NurseHistory(**kw)
