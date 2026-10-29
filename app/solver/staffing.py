"""Staffing check (design doc 6.7): minimum headcount and what-if staffing for one period.

Ported from reference/mini_ward/staffing/staffing_check.py onto the app's `RosterModel` (hard
rules, approved leave, the coverage terms). One bool `used[n]` per regular nurse with
works(n,d) <= used[n]; solved lexicographically:

1. coverage shortfall against target (or 0 when only the hard floor is asked for),
2. nurses used,
3. standby shifts,
4. balance = 2 spread(total shifts, leave credited) + 2 spread(nights) + 1 spread(evenings)
   + 2 spread(weekend shifts), over the nurses in use this period.

Hypothetical hires are interchangeable, so `used[H1] >= used[H2] >= ...` breaks their symmetry.
With hires in the pool the balance stage may still end "best found" (its bound comes from big-M
spreads over the nurses in use and stays weak); the first three stages prove in about a second.
Requests, sequence rules and history-based fairness are ignored, so every number is a lower bound
for a publishable roster. Pure function of a `SolveInput`; nothing is stored.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ortools.sat.python import cp_model

from app.solver.input import SHIFTS, NurseHistory, NurseSpec, SolveInput
from app.solver.model import RosterModel
from app.solver.solve import new_solver
from app.solver.validate import violations

Cover = Literal["target", "floor"]
STAGES = ("shortfall", "nurses_used", "standby_shifts", "balance")


@dataclass
class StaffingStage:
    stage: str
    value: int
    proven: bool
    bound: int | None = None


@dataclass
class StaffingResult:
    status: str  # solved | infeasible | unknown
    stages: list[StaffingStage]
    secs: float
    used: list[str] = field(default_factory=list)
    short_cells: list[tuple[int, str, int, int]] = field(default_factory=list)  # d, s, got, target
    load: dict[str, dict[str, int]] = field(default_factory=dict)  # nurse -> total M E N wkend

    def value(self, stage: str) -> int | None:
        return next((s.value for s in self.stages if s.stage == stage), None)


def hire_ids(k: int) -> tuple[str, ...]:
    return tuple(f"H{i}" for i in range(1, k + 1))


def with_hires(inp: SolveInput, k: int) -> SolveInput:
    """Add k hypothetical regular nurses H1..Hk: night-qualified, on the contract of the first
    regular nurse, clean history."""
    if k <= 0:
        return inp
    ref = next(n for n in inp.nurses if not n.standby and n.role != "ans")
    hires = tuple(
        NurseSpec(h, "staff", "staff", False, True, ref.min_shifts, ref.max_shifts, ref.max_hours)
        for h in hire_ids(k)
    )
    history = {**inp.history, **{h: NurseHistory() for h in hire_ids(k)}}
    return replace(inp, nurses=inp.nurses + hires, history=history)


def without(inp: SolveInput, gone: Iterable[str]) -> SolveInput:
    """Remove nurses from the pool for the whole period (long leave, transfer)."""
    out = set(gone)

    def keep(n: str) -> bool:
        return n not in out

    return replace(
        inp,
        nurses=tuple(n for n in inp.nurses if keep(n.id)),
        unavailable=frozenset(c for c in inp.unavailable if keep(c[0])),
        leave=frozenset(c for c in inp.leave if keep(c[0])),
        night_exempt=frozenset(c for c in inp.night_exempt if keep(c[0])),
        day_off_prefs=tuple(p for p in inp.day_off_prefs if keep(p.nurse)),
        shift_prefs=tuple(p for p in inp.shift_prefs if keep(p.nurse)),
        shift_avoids=tuple(p for p in inp.shift_avoids if keep(p.nurse)),
        forced=tuple(f for f in inp.forced if keep(f.nurse)),
        history={n: h for n, h in inp.history.items() if keep(n)},
    )


def check(
    inp: SolveInput,
    *,
    allow_standby: bool = False,
    fixed: Sequence[str] = (),
    cover: Cover = "target",
    seconds: float = 10.0,
    workers: int = 4,
) -> StaffingResult:
    """`fixed`: nurses that count as used regardless (today's staff, for "how many extra?")."""
    t0 = time.perf_counter()
    inp = replace(
        inp, standby_allowed=allow_standby, priorities=("coverage",), previous=None,
        frozen_before_day=0, frozen_cells=frozenset(),
    )  # fmt: skip
    rm = RosterModel(inp)
    m, D = rm.m, inp.num_days
    regular = [n.id for n in inp.nurses if not n.standby and n.role != "ans"]
    standby = [n.id for n in inp.nurses if n.standby]
    used = {n: m.new_bool_var(f"used[{n}]") for n in regular}
    for n in regular:
        for d in range(D):
            m.add(rm.works(n, d) <= used[n])
        if n in fixed:
            m.add(used[n] == 1)
    hires = [h for h in regular if h.startswith("H") and h[1:].isdigit()]
    for a, b in zip(hires, hires[1:], strict=False):
        m.add(used[a] >= used[b])  # interchangeable hires: use them in order

    away: dict[str, int] = {n: 0 for n in regular}
    whole_day = {(n, d) for n, d, s in inp.unavailable if s is None} | set(inp.leave)
    for n, _d in whole_day:
        if n in away:
            away[n] += 1
    big = 2 * D
    night_ok = {n.id for n in inp.nurses if n.can_night}
    days = range(D)
    total = {n: sum(rm.works(n, d) for d in days) + away[n] for n in regular}
    nights = {n: sum(rm.xs(n, d, "N") for d in days) for n in regular if n in night_ok}
    evenings = {n: sum(rm.xs(n, d, "E") for d in days) for n in regular}
    weekends = {n: sum(rm.works(n, d) for d in inp.weekend_days) for n in regular}
    spreads: list[Any] = []
    for w, vals in ((2, total), (2, nights), (1, evenings), (2, weekends)):
        hi, lo = m.new_int_var(0, big, ""), m.new_int_var(0, big, "")
        for n, v in vals.items():  # only nurses in use count toward the spread
            m.add(hi >= v - big * (1 - used[n]))
            m.add(lo <= v + big * (1 - used[n]))
        spreads.append(w * (hi - lo))

    objectives: dict[str, Any] = {
        "shortfall": rm.tier_penalty("coverage") if cover == "target" else 0,
        "nurses_used": sum(used.values()),
        "standby_shifts": sum(rm.works(n, d) for n in standby for d in range(D))
        if allow_standby
        else 0,
        "balance": sum(spreads),
    }
    stages: list[StaffingStage] = []
    solver: cp_model.CpSolver | None = None
    for name in STAGES:
        expr = objectives[name]
        if isinstance(expr, int):
            stages.append(StaffingStage(name, expr, True))
            continue
        m.minimize(expr)
        solver = new_solver(seconds, workers)
        st = solver.solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            status = "infeasible" if st == cp_model.INFEASIBLE else "unknown"
            return StaffingResult(status, stages, round(time.perf_counter() - t0, 2))
        best = round(solver.objective_value)
        stages.append(
            StaffingStage(name, best, st == cp_model.OPTIMAL, round(solver.best_objective_bound))
        )
        m.add(expr <= best)
        rm.hint_from(solver)
    if solver is None:  # every stage was constant; one solve for the roster itself
        solver = new_solver(seconds, workers)
        if solver.solve(m) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return StaffingResult("infeasible", stages, round(time.perf_counter() - t0, 2))

    roster = rm.roster(solver)
    bad = violations(inp, roster)
    assert not bad, bad  # the model and the independent validator must agree
    short = []
    for (d, s), dm in sorted(inp.demand.items()):
        got = sum(roster.get((n.id, d)) == s for n in inp.nurses)
        if got < dm.target_total:
            short.append((d, s, got, dm.target_total))
    load = {}
    for spec in inp.nurses:
        row = [roster.get((spec.id, d)) for d in range(D)]
        if any(row):
            load[spec.id] = {
                "total": sum(c is not None for c in row),
                **{s: sum(c == s for c in row) for s in SHIFTS},
                "weekend": sum(row[d] is not None for d in inp.weekend_days),
            }
    return StaffingResult(
        "solved",
        stages,
        round(time.perf_counter() - t0, 2),
        used=sorted(n for n in regular if solver.value(used[n])),
        short_cells=short,
        load=load,
    )
