"""Independent roster validator (design doc 5.2, 11.3). Pure Python, no CP-SAT.

Written separately from `model.py`: it re-derives every hard rule from the `SolveInput` and a
finished roster and returns the names of broken constraints, using the same `group:scope` names
the model registers (`rest_after_night:N07:d12`). Every roster the app produces must pass it.

Roster format: `{(nurse_id, day): shift code or None}`; a missing cell means off.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from app.solver.input import SHIFTS, NurseSpec, SolveInput

Roster = Mapping[tuple[str, int], str | None]

DAY_SHIFTS = frozenset({"M", "E", "G"})  # shifts that may not follow a night (rest_after_night)


def code(inp: SolveInput, roster: Roster, n: str, d: int) -> str | None:
    """Shift on day d; days -3..-1 come from the carry-in history; None = off."""
    if d >= 0:
        return roster.get((n, d)) if d < inp.num_days else None
    if d < -3:
        return None
    h = inp.history.get(n)
    return None if h is None else h.last_shifts[3 + d]


def _hours_x10(inp: SolveInput, s: str) -> int:
    return round(inp.shift_hours.get(s, 8.0) * 10)


def _run_breaks(
    flags: list[bool], carry: int, limit: int, group: str, n: str, waived: frozenset[str]
) -> list[str]:
    """One name per day on which the run (seeded with carry-in) exceeds the limit."""
    out, run = [], carry
    for d, f in enumerate(flags):
        run = run + 1 if f else 0
        name = f"{group}:{n}:d{d}"
        if run > limit and name not in waived:
            out.append(name)
    return out


def nurse_violations(inp: SolveInput, roster: Roster, n: str) -> list[str]:
    """Every per-nurse hard rule (all groups except the per-shift cover rules)."""
    spec = next(x for x in inp.nurses if x.id == n)
    w = inp.waived
    out: list[str] = []

    def add(name: str) -> None:
        if name not in w:
            out.append(name)

    days = range(inp.num_days)
    row = [roster.get((n, d)) for d in days]
    unavailable = {(d, s) for m, d, s in inp.unavailable if m == n}
    leave = {d for m, d in inp.leave if m == n}
    exempt = {d for m, d in inp.night_exempt if m == n}
    forced = [f for f in inp.forced if f.nurse == n]
    forced_g = {f.day for f in forced if f.shift == "G"}

    for d, s in enumerate(row):
        if s is None:
            continue
        if (d, None) in unavailable:
            add(f"unavailable:{n}:d{d}")
        if (d, s) in unavailable:
            add(f"unavailable:{n}:d{d}:{s}")
        if d in leave:
            add(f"leave:{n}:d{d}")
        if s == "N" and not spec.can_night:
            add(f"night_qualified:{n}:d{d}")
        if s == "N" and d in exempt:
            add(f"night_exempt:{n}:d{d}")
        if spec.standby and not inp.standby_allowed:
            add(f"standby_off_roster:{n}:d{d}")
        if spec.role == "ans" and s in SHIFTS:
            add(f"ans_general_duty:{n}:d{d}")
        if s == "G" and d not in forced_g:
            add(f"general_duty_forced_only:{n}:d{d}")

    for d in range(-1, inp.num_days - 1):
        if code(inp, roster, n, d) == "N" and code(inp, roster, n, d + 1) in DAY_SHIFTS:
            add(f"rest_after_night:{n}:d{d}")

    h = inp.history.get(n)
    out += _run_breaks(
        [s is not None for s in row],
        0 if h is None else h.consecutive_days,
        inp.max_consecutive_days,
        "max_consecutive_days",
        n,
        w,
    )
    out += _run_breaks(
        [s == "N" for s in row],
        0 if h is None else h.consecutive_nights,
        inp.night_block_max,
        "max_consecutive_nights",
        n,
        w,
    )

    if sum(s is not None for s in row) > spec.max_shifts:
        add(f"max_shifts:{n}")
    if sum(s == "N" for s in row) > inp.max_nights:
        add(f"max_nights:{n}")
    if spec.max_hours is not None:
        worked = sum(_hours_x10(inp, s) for s in row if s is not None)
        if worked > math.floor(round(spec.max_hours * 10, 6)):
            add(f"max_hours:{n}")

    if inp.previous is not None:
        for d in days:
            frozen = d < inp.frozen_before_day or (n, d) in inp.frozen_cells
            if frozen and row[d] != inp.previous.get((n, d)):
                add(f"frozen:{n}:d{d}")

    for f in forced:
        if row[f.day] != f.shift:
            if f.kind == "fixed_duty":
                add(f"fixed_duty:{n}:d{f.day}")
            else:
                tail = "" if f.shift is None else f":{f.shift}"
                add(f"forced:{f.kind}:{n}:d{f.day}{tail}")
    return out


def cover_violations(inp: SolveInput, roster: Roster, days: Iterable[int]) -> list[str]:
    """min_cover, senior_cover and max_cover for the given days (G never counts)."""
    seniors = {x.id for x in inp.nurses if x.grade == "senior"}
    out = []
    for d in days:
        for s in SHIFTS:
            spec = inp.demand.get((d, s))
            if spec is None:
                continue
            on = [x.id for x in inp.nurses if roster.get((x.id, d)) == s]
            if len(on) < spec.min_total:
                out.append(f"min_cover:d{d}:{s}")
            if spec.min_senior and sum(n in seniors for n in on) < spec.min_senior:
                out.append(f"senior_cover:d{d}:{s}")
            if len(on) > spec.max_total:
                out.append(f"max_cover:d{d}:{s}")
    return [v for v in out if v not in inp.waived]


def violations(inp: SolveInput, roster: Roster) -> list[str]:
    """Every broken hard constraint, by name. Empty list = valid roster."""
    out: list[str] = []
    for spec in inp.nurses:
        out += nurse_violations(inp, roster, spec.id)
    out += cover_violations(inp, roster, range(inp.num_days))
    known = {x.id for x in inp.nurses}
    out += [f"unknown_nurse:{n}:d{d}" for (n, d), s in roster.items() if n not in known and s]
    out += [
        f"unknown_shift:{n}:d{d}:{s}"
        for (n, d), s in roster.items()
        if s is not None and s not in (*SHIFTS, "G")
    ]
    return out


def display_codes(inp: SolveInput, roster: Roster, spec: NurseSpec) -> list[str]:
    """What the paper roster shows (4.1): the shift, N/O for the off day right after a night,
    L for leave or whole-day unavailability, D/O for any other day off."""
    n = spec.id
    away = {d for m, d in inp.leave if m == n} | {
        d for m, d, s in inp.unavailable if m == n and s is None
    }
    row = []
    for d in range(inp.num_days):
        s = roster.get((n, d))
        if s is not None:
            row.append(s)
        elif d in away:
            row.append("L")
        elif code(inp, roster, n, d - 1) == "N":
            row.append("N/O")
        else:
            row.append("D/O")
    return row
