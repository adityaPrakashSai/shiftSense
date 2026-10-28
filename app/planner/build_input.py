"""Ward + active requests + history -> frozen `SolveInput` (design doc 4.1, 4.3).

Scaling rules (4.1, 5.2): weekly contract minimum is scaled to the horizon and rounded down, the
maximum rounded up; `max_nights_per_month` becomes ceil(per_month x days / 30); an optional weekly
hours limit is scaled linearly. Requests and leave outside the horizon are clipped (the part beyond
the end carries into the next period).
"""

import math
from collections.abc import Iterable
from datetime import date, timedelta

from app.domain.models import (
    Request,
    RequestKind,
    RequestStatus,
    Ward,
    WardHistory,
)
from app.solver.input import (
    SHIFTS,
    DemandSpec,
    Forcing,
    NurseHistory,
    NurseSpec,
    Pref,
    SolveInput,
)


def day_index(ward: Ward, d: date) -> int:
    return (d - ward.horizon_start).days


def horizon_dates(ward: Ward) -> list[date]:
    return [ward.horizon_start + timedelta(days=i) for i in range(ward.horizon_days)]


def scaled_contract(per_week_min: float, per_week_max: float, days: int) -> tuple[int, int]:
    weeks = days / 7
    # round before floor/ceil so 5.5 x 2.0 = 11.000000001 does not become 12
    return math.floor(round(per_week_min * weeks, 6)), math.ceil(round(per_week_max * weeks, 6))


def scaled_max_nights(per_month: int, days: int) -> int:
    return math.ceil(round(per_month * days / 30, 6))


def _nurse_specs(ward: Ward) -> tuple[NurseSpec, ...]:
    out = []
    for n in ward.nurses:
        lo, hi = scaled_contract(n.min_shifts_per_week, n.max_shifts_per_week, ward.horizon_days)
        hours = (
            None
            if n.max_hours_per_week is None
            else round(n.max_hours_per_week * ward.horizon_days / 7, 6)
        )
        out.append(
            NurseSpec(
                id=n.id,
                grade=n.grade.value,
                role=n.role.value,
                standby=n.standby,
                can_night=n.can_night,
                min_shifts=lo,
                max_shifts=hi,
                max_hours=hours,
            )
        )
    return tuple(out)


def _history(ward: Ward, history: WardHistory | None) -> dict[str, NurseHistory]:
    recs = {} if history is None else history.nurses
    out = {}
    for n in ward.nurses:
        h = recs.get(n.id)
        if h is None:
            out[n.id] = NurseHistory()
            continue
        tail: list[str | None] = [None if s is None else s.value for s in h.last_days[-3:]]
        pad: list[str | None] = [None] * (3 - len(tail))
        last = pad + tail
        out[n.id] = NurseHistory(
            last_shifts=(last[0], last[1], last[2]),
            consecutive_days=h.consecutive_days,
            consecutive_nights=h.consecutive_nights,
            nights=h.nights,
            mornings=h.mornings,
            evenings=h.evenings,
            weekends=h.weekends,
            denied=h.denied,
        )
    return out


def build_input(
    ward: Ward,
    requests: Iterable[Request],
    history: WardHistory | None = None,
    *,
    standby_allowed: bool = False,
) -> SolveInput:
    days = ward.horizon_days
    dates = horizon_dates(ward)
    in_horizon = set(range(days))

    demand: dict[tuple[int, str], DemandSpec] = {}
    for dm in ward.demand:
        d = day_index(ward, dm.day)
        if d in in_horizon and dm.shift.value in SHIFTS:
            demand[(d, dm.shift.value)] = DemandSpec(
                min_total=dm.min_total,
                target_total=dm.target_total,
                max_total=dm.target_total if dm.max_total is None else dm.max_total,
                min_senior=dm.min_senior,
            )

    unavailable: set[tuple[str, int, str | None]] = set()
    leave: set[tuple[str, int]] = set()
    day_off: list[Pref] = []
    wanted: list[Pref] = []
    avoids: list[Pref] = []
    for r in sorted(requests, key=lambda r: r.id):
        if r.status is not RequestStatus.ACTIVE:
            continue
        idx = sorted({day_index(ward, x) for x in r.dates} & in_horizon)
        shifts: list[str] | None = None if r.shifts is None else [s.value for s in r.shifts]
        for d in idx:
            match r.kind:
                case RequestKind.UNAVAILABLE:
                    if shifts is None:
                        unavailable.add((r.nurse_id, d, None))
                    else:
                        unavailable |= {(r.nurse_id, d, s) for s in shifts}
                case RequestKind.LEAVE:
                    leave.add((r.nurse_id, d))
                case RequestKind.DAY_OFF:
                    day_off.append(Pref(r.id, r.nurse_id, d, None, r.weight))
                case RequestKind.SHIFT_WANTED:
                    wanted += [Pref(r.id, r.nurse_id, d, s, r.weight) for s in shifts or []]
                case RequestKind.SHIFT_AVOID:
                    avoids += [Pref(r.id, r.nurse_id, d, s, r.weight) for s in shifts or []]
                case RequestKind.SWAP:
                    pass  # becomes forcings only after approval (M6+), never directly

    # a person who is away that day cannot be forced onto a fixed duty
    away = {(n, d) for n, d, s in unavailable if s is None} | leave
    night_exempt: set[tuple[str, int]] = set()
    forced: list[Forcing] = []
    for n in ward.nurses:
        if n.night_exempt_until is not None:
            night_exempt |= {(n.id, d) for d in range(days) if dates[d] <= n.night_exempt_until}
        if n.fixed_duty:  # ANS on G, in-charge on M (4.1 roles)
            for d in range(days):
                shift = n.fixed_duty.get(dates[d].weekday())
                if shift is not None and (n.id, d) not in away:
                    forced.append(Forcing("fixed_duty", n.id, d, shift.value))

    return SolveInput(
        nurses=_nurse_specs(ward),
        num_days=days,
        weekend_days=frozenset(d for d in range(days) if dates[d].weekday() >= 5),
        shift_hours={s.code.value: s.hours for s in ward.shifts},
        demand=demand,
        unavailable=frozenset(unavailable),
        leave=frozenset(leave),
        night_exempt=frozenset(night_exempt),
        day_off_prefs=tuple(day_off),
        shift_prefs=tuple(wanted),
        shift_avoids=tuple(avoids),
        forced=tuple(forced),
        waived=frozenset(),
        max_consecutive_days=ward.max_consecutive_days,
        night_block_min=ward.night_block_min,
        night_block_max=ward.night_block_max,
        max_nights=scaled_max_nights(ward.max_nights_per_month, days),
        standby_allowed=standby_allowed,
        history=_history(ward, history),
        priorities=tuple(ward.priorities),
    )
