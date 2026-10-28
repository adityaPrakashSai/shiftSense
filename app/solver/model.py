"""CP-SAT roster model (design doc 5.1-5.3). Imports only `ortools` and `app.solver`.

One boolean x[n,d,s] per nurse, day and shift. Every hard constraint goes through `hard(group,
scope, ...)`, which names it "{group}:{scope}" exactly as `validate.py` reports it. In diagnosis
mode (`guarded=True`, used by M9) each one is guarded by an assumption literal of the same name;
names in `SolveInput.waived` are added with `only_enforce_if(false)` so the model shape stays the
same and the waiver is visible.

Soft terms live in `self.terms`. M2 has one tier: coverage shortfall (COVER_SHORT); the other
tiers of 5.3 arrive in M3.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

from app.solver.input import SHIFTS, SolveInput

LinearLike = Any  # cp_model.LinearExprT; ortools' stubs do not export a usable alias

DAY_SHIFTS = ("M", "E", "G")  # may not follow a night (rest_after_night)


@dataclass(frozen=True)
class SoftTerm:
    tier: str
    expr: LinearLike  # non-negative linear expression
    weight: int
    ub: int  # maximum value of expr (dominance weights, sanity checks)
    rule: str  # stable reason code (COVER_SHORT, ...)
    nurse: str | None = None
    day: int | None = None
    ref: str | None = None  # shift, request id, ...
    why: str = ""


class RosterModel:
    def __init__(
        self,
        inp: SolveInput,
        *,
        guarded: bool = False,
        drop_groups: frozenset[str] = frozenset(),
    ) -> None:
        """`drop_groups` removes whole hard groups; tests use it to prove each rule bites."""
        self.inp = inp
        self.guarded = guarded
        self.drop_groups = drop_groups
        self.m = cp_model.CpModel()
        self.names: list[str] = []  # every hard constraint registered, in order
        self.guards: dict[str, cp_model.IntVar] = {}
        self.terms: dict[str, list[SoftTerm]] = {t: [] for t in inp.priorities}
        self._false = self.m.new_bool_var("waived")
        self.m.add(self._false == 0)

        D = inp.num_days
        self.days = range(D)
        self.nurse_ids = tuple(n.id for n in inp.nurses)
        forced_g = {(f.nurse, f.day) for f in inp.forced if f.shift == "G"}
        prev_g = {k for k, s in (inp.previous or {}).items() if s == "G"}

        # 5.1 decision variables. G (general duty) exists only where it is forced or published.
        self.x: dict[tuple[str, int, str], cp_model.IntVar] = {}
        self._cell: dict[tuple[str, int], list[cp_model.IntVar]] = {}
        for n in self.nurse_ids:
            for d in self.days:
                for s in SHIFTS:
                    self.x[n, d, s] = self.m.new_bool_var(f"x[{n},{d},{s}]")
                if (n, d) in forced_g | prev_g:
                    self.x[n, d, "G"] = self.m.new_bool_var(f"x[{n},{d},G]")
                self._cell[n, d] = [self.x[k] for s in (*SHIFTS, "G") if (k := (n, d, s)) in self.x]

        self._add_hard_rules()
        self._add_coverage_terms()

    # ---------------------------------------------------------------- helpers

    def xs(self, n: str, d: int, s: str) -> LinearLike:
        """x[n,d,s], or 0 when that variable does not exist (G on an ordinary day)."""
        return self.x.get((n, d, s), 0)

    def works(self, n: str, d: int) -> LinearLike:
        """works(n,d) = sum_s x[n,d,s], a linear expression (5.1)."""
        return sum(self._cell[n, d])

    def hard(self, group: str, scope: str, ct: Any) -> None:
        """Register one hard constraint `ct` (a bounded linear expression) as "{group}:{scope}"."""
        if group in self.drop_groups:
            return
        name = f"{group}:{scope}"
        if isinstance(ct, bool):  # every term was a constant
            if ct:
                return
            ct = self._false == 1  # a constant contradiction stays visible by name
        c = self.m.add(ct).with_name(name)
        self.names.append(name)
        if name in self.inp.waived:
            c.only_enforce_if(self._false)
        elif self.guarded:
            g = self.m.new_bool_var(name)
            self.guards[name] = g
            c.only_enforce_if(g)

    def hard_counts(self) -> dict[str, int]:
        return dict(Counter(name.split(":", 1)[0] for name in self.names))

    def soft(self, term: SoftTerm) -> None:
        if term.tier in self.terms:
            self.terms[term.tier].append(term)

    def tier_penalty(self, tier: str) -> LinearLike:
        return sum(t.weight * t.expr for t in self.terms.get(tier, []))

    # ---------------------------------------------------------------- 5.2 hard constraints

    def _add_hard_rules(self) -> None:
        inp, hard, xs = self.inp, self.hard, self.xs
        D = inp.num_days
        hist = inp.history
        unavailable = sorted(inp.unavailable, key=repr)

        for spec in inp.nurses:
            n = spec.id
            h = hist.get(n)
            for d in self.days:
                # one_shift_per_day  works(n,d) <= 1
                hard("one_shift_per_day", f"{n}:d{d}", self.works(n, d) <= 1)
                # night_qualified  x[n,d,N] = 0 if not can_night
                if not spec.can_night:
                    hard("night_qualified", f"{n}:d{d}", xs(n, d, "N") == 0)
                # standby_off_roster  standby nurses are never rostered in a full run (4.1)
                if spec.standby and not inp.standby_allowed:
                    hard("standby_off_roster", f"{n}:d{d}", self.works(n, d) == 0)
                # ans_general_duty  the ANS works only G, outside ward coverage (4.1 roles)
                if spec.role == "ans":
                    hard(
                        "ans_general_duty",
                        f"{n}:d{d}",
                        sum(xs(n, d, s) for s in SHIFTS) == 0,
                    )

            # rest_after_night  x[n,d,N] + x[n,d+1,M] + x[n,d+1,E] <= 1; d = -1 from carry-in
            last = None if h is None else h.last_shifts[-1]
            for d in range(-1, D - 1):
                night = int(last == "N") if d < 0 else xs(n, d, "N")
                if isinstance(night, int) and night == 0:
                    continue
                day_after = sum(xs(n, d + 1, s) for s in DAY_SHIFTS)
                hard("rest_after_night", f"{n}:d{d}", night + day_after <= 1)

            # max_consecutive_days / max_consecutive_nights: every window of K+1 days ending on
            # day e has at most K worked days (nights); the days before 0 come from the carry-in run
            self._max_run(
                n,
                "max_consecutive_days",
                inp.max_consecutive_days,
                0 if h is None else h.consecutive_days,
                lambda d, n=n: self.works(n, d),
            )
            self._max_run(
                n,
                "max_consecutive_nights",
                inp.night_block_max,
                0 if h is None else h.consecutive_nights,
                lambda d, n=n: xs(n, d, "N"),
            )

            # max_shifts  sum_d works(n,d) <= max_shifts
            hard("max_shifts", n, sum(self.works(n, d) for d in self.days) <= spec.max_shifts)
            # max_nights  sum_d x[n,d,N] <= max_nights (6 a month, scaled; 5.2 night limits)
            hard("max_nights", n, sum(xs(n, d, "N") for d in self.days) <= inp.max_nights)
            # max_hours  sum of worked hours <= max_hours (only when set); hours in tenths
            if spec.max_hours is not None:
                tenths = sum(
                    round(inp.shift_hours.get(s, 8.0) * 10) * v
                    for (m, _d, s), v in self.x.items()
                    if m == n
                )
                hard("max_hours", n, tenths <= math.floor(round(spec.max_hours * 10, 6)))

        # unavailable  x[n,d,s] = 0 for the listed shifts, or works(n,d) = 0
        for n, d, s in unavailable:
            if s is None:
                hard("unavailable", f"{n}:d{d}", self.works(n, d) == 0)
            else:
                hard("unavailable", f"{n}:d{d}:{s}", xs(n, d, s) == 0)
        # leave  works(n,d) = 0
        for n, d in sorted(inp.leave):
            hard("leave", f"{n}:d{d}", self.works(n, d) == 0)
        # night_exempt  x[n,d,N] = 0 while night_exempt_until >= that date
        for n, d in sorted(inp.night_exempt):
            hard("night_exempt", f"{n}:d{d}", xs(n, d, "N") == 0)

        # min_cover / senior_cover / max_cover per (day, shift); G never counts
        seniors = [x.id for x in inp.nurses if x.grade == "senior"]
        for (d, s), dm in sorted(inp.demand.items()):
            on = sum(xs(n, d, s) for n in self.nurse_ids)
            hard("min_cover", f"d{d}:{s}", on >= dm.min_total)
            if dm.min_senior:
                hard(
                    "senior_cover", f"d{d}:{s}", sum(xs(n, d, s) for n in seniors) >= dm.min_senior
                )
            hard("max_cover", f"d{d}:{s}", on <= dm.max_total)

        # frozen  x[n,d,s] = [previous(n,d) = s] for d < frozen_before_day or a frozen cell
        if inp.previous is not None:
            for n in self.nurse_ids:
                for d in self.days:
                    if d < inp.frozen_before_day or (n, d) in inp.frozen_cells:
                        self._fix(n, d, inp.previous.get((n, d)), "frozen", f"{n}:d{d}")

        # fixed_duty (ANS on G, in-charge on M) and forced (approved swaps, what-if forcings)
        for f in inp.forced:
            if f.kind == "fixed_duty":
                self._fix(f.nurse, f.day, f.shift, "fixed_duty", f"{f.nurse}:d{f.day}")
            else:
                tail = "" if f.shift is None else f":{f.shift}"
                self._fix(f.nurse, f.day, f.shift, "forced", f"{f.kind}:{f.nurse}:d{f.day}{tail}")

    def _fix(self, n: str, d: int, shift: str | None, group: str, scope: str) -> None:
        """One constraint fixing cell (n,d) to `shift` (None = off)."""
        if shift is None:
            self.hard(group, scope, self.works(n, d) == 0)
        elif (n, d, shift) in self.x:
            self.hard(group, scope, self.x[n, d, shift] == 1)
        else:
            self.hard(group, scope, False)

    def _max_run(self, n: str, group: str, limit: int, carry: int, flag: Any) -> None:
        for e in self.days:
            start = e - limit
            in_horizon = range(max(0, start), e + 1)
            before = min(carry, -start) if start < 0 else 0  # carry-in days inside the window
            if len(in_horizon) + before <= limit:
                continue  # the window cannot exceed the limit
            self.hard(group, f"{n}:d{e}", sum(flag(d) for d in in_horizon) + before <= limit)

    # ---------------------------------------------------------------- 5.3 soft terms (M2: coverage)

    def _add_coverage_terms(self) -> None:
        for (d, s), dm in sorted(self.inp.demand.items()):
            on = sum(self.xs(n, d, s) for n in self.nurse_ids)
            short = self.m.new_int_var(0, dm.target_total, f"short[{d},{s}]")
            self.m.add(on + short >= dm.target_total)
            self.soft(
                SoftTerm(
                    "coverage",
                    short,
                    1,
                    dm.target_total,
                    "COVER_SHORT",
                    None,
                    d,
                    s,
                    f"day {d} {s} below target {dm.target_total}",
                )
            )

    # ---------------------------------------------------------------- results

    def roster(self, solver: cp_model.CpSolver) -> dict[tuple[str, int], str | None]:
        out: dict[tuple[str, int], str | None] = {
            (n, d): None for n in self.nurse_ids for d in self.days
        }
        for (n, d, s), v in self.x.items():
            if solver.value(v):
                out[n, d] = s
        return out

    def hint_from(self, solver: cp_model.CpSolver) -> None:
        self.m.clear_hints()  # type: ignore[no-untyped-call]  # untyped in ortools 9.15
        for v in self.x.values():
            self.m.add_hint(v, solver.value(v))

    def hint_roster(self, roster: dict[tuple[str, int], str | None]) -> None:
        self.m.clear_hints()  # type: ignore[no-untyped-call]  # untyped in ortools 9.15
        for (n, d, s), v in self.x.items():
            self.m.add_hint(v, int(roster.get((n, d)) == s))
