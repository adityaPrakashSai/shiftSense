"""CP-SAT roster model (design doc 5.1-5.3). Imports only `ortools` and `app.solver`.

One boolean x[n,d,s] per nurse, day and shift. Every hard constraint goes through `hard(group,
scope, ...)`, which names it "{group}:{scope}" exactly as `validate.py` reports it. In diagnosis
mode (`guarded=True`, used by M9) each one is guarded by an assumption literal of the same name;
names in `SolveInput.waived` are added with `only_enforce_if(false)` so the model shape stays the
same and the waiver is visible.

Soft terms (5.3) live in `self.terms`, one list per tier, each a `SoftTerm` with a stable rule
code; `tier_penalty(tier)` is the objective of that tier's lexicographic stage (5.4).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

from app.solver.input import SHIFTS, Pref, SolveInput

LinearLike = Any  # cp_model.LinearExprT; ortools' stubs do not export a usable alias

DAY_SHIFTS = ("M", "E", "G")  # may not follow a night (rest_after_night)

# 5.3 weights and tiers. Sequence rules are weighted inside the requests tier
# ("wellbeing and requests"); request weights come from each request (PREF_OFF uses 2 x weight).
WEIGHTS: dict[str, int] = {
    "COVER_SHORT": 1,
    "CHANGED": 2,
    "STANDBY_CALLED": 1,
    "CONTRACT_MIN": 1,
    "QUICK_RETURN": 3,
    "SINGLE_NIGHT": 3,
    "ISOLATED_DAY": 1,
    "SPLIT_WEEKEND": 1,
    "FAIR_NIGHTS": 2,
    "FAIR_WEEKENDS": 2,
    "FAIR_MORNINGS": 2,
    "FAIR_EVENINGS": 2,
    "FAIR_DENIALS": 1,
}
TIER_OF: dict[str, str] = {
    "COVER_SHORT": "coverage",
    "CHANGED": "stability",
    "STANDBY_CALLED": "stability",
    "CONTRACT_MIN": "contract",
    "QUICK_RETURN": "requests",
    "SINGLE_NIGHT": "requests",
    "ISOLATED_DAY": "requests",
    "SPLIT_WEEKEND": "requests",
    "PREF_OFF": "requests",
    "PREF_SHIFT": "requests",
    "PREF_AVOID": "requests",
    "FAIR_NIGHTS": "fairness",
    "FAIR_WEEKENDS": "fairness",
    "FAIR_MORNINGS": "fairness",
    "FAIR_EVENINGS": "fairness",
    "FAIR_DENIALS": "fairness",
}
WHY: dict[str, str] = {
    "CHANGED": "duty changed from the published roster",
    "STANDBY_CALLED": "standby nurse called in",
    "QUICK_RETURN": "evening then morning (8 h rest)",
    "SINGLE_NIGHT": "single night, shorter than the minimum night block",
    "ISOLATED_DAY": "single working day between days off",
    "SPLIT_WEEKEND": "works only one day of the weekend",
    "PREF_OFF": "day off not granted",
    "PREF_SHIFT": "wanted shift not granted",
    "PREF_AVOID": "avoided shift assigned",
    "FAIR_NIGHTS": "spread of nights, history included",
    "FAIR_WEEKENDS": "spread of weekend shifts, history included",
    "FAIR_MORNINGS": "spread of mornings, history included",
    "FAIR_EVENINGS": "spread of evenings, history included",
    "FAIR_DENIALS": "spread of denied requests, history included",
}


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
        guard_groups: frozenset[str] | None = None,
        drop_groups: frozenset[str] = frozenset(),
    ) -> None:
        """`drop_groups` removes whole hard groups; tests use it to prove each rule bites.
        In guarded mode `guard_groups` limits the guards to those groups (None = all); the
        others stay plain constraints, so a core names only what a person could relax."""
        self.inp = inp
        self.guarded = guarded
        self.guard_groups = guard_groups
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

        self.denied: dict[str, tuple[str, cp_model.IntVar]] = {}  # request id -> (nurse, bool)
        self.spread_members: dict[str, list[tuple[str, LinearLike]]] = {}
        self._add_hard_rules()
        self._add_soft_terms()

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
        elif self.guarded and (self.guard_groups is None or group in self.guard_groups):
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

    # ---------------------------------------------------------------- 5.3 soft terms

    def lit(self, n: str, d: int, what: str) -> LinearLike:
        """0/1 expression (a constant for d < 0, from carry-in) for: a shift code, "W" worked,
        "OFF" off, or "D" a day shift (M or E)."""
        if d < 0:
            h = self.inp.history.get(n)
            c = None if h is None or d < -3 else h.last_shifts[3 + d]
            return int(
                {"W": c is not None, "OFF": c is None, "D": c in ("M", "E")}.get(what, c == what)
            )
        if what == "W":
            return self.works(n, d)
        if what == "OFF":
            return 1 - self.works(n, d)
        if what == "D":
            return self.xs(n, d, "M") + self.xs(n, d, "E")
        return self.xs(n, d, what)

    def _pattern(self, rule: str, n: str, d: int, lits: list[LinearLike]) -> None:
        """Penalty 1 when every 0/1 literal in `lits` is 1 (sequence rules)."""
        if any(isinstance(v, int) and v == 0 for v in lits):
            return
        var = [v for v in lits if not isinstance(v, int)]
        why = WHY[rule]
        if not var:  # fixed by the carry-in alone
            self.soft(SoftTerm(TIER_OF[rule], 1, WEIGHTS[rule], 1, rule, n, d, None, why))
            return
        p = self.m.new_bool_var(f"{rule}[{n},{d}]")
        self.m.add(p >= sum(var) - (len(var) - 1))
        self.soft(SoftTerm(TIER_OF[rule], p, WEIGHTS[rule], 1, rule, n, d, None, why))

    def _spread(self, rule: str, vals: list[tuple[str, LinearLike, int]]) -> None:
        """max - min over (nurse, expression, upper bound); minimised, so inequalities suffice."""
        if not vals:
            return
        top = max(ub for _n, _e, ub in vals)
        hi = self.m.new_int_var(0, top, f"hi[{rule}]")
        lo = self.m.new_int_var(0, top, f"lo[{rule}]")
        for _n, e, _ub in vals:
            self.m.add(hi >= e)
            self.m.add(lo <= e)
        self.spread_members[rule] = [(n, e) for n, e, _ub in vals]
        self.soft(SoftTerm("fairness", hi - lo, WEIGHTS[rule], top, rule, why=WHY[rule]))

    def _add_soft_terms(self) -> None:
        inp, xs, works, lit = self.inp, self.xs, self.works, self.lit
        D = inp.num_days
        days = self.days
        specs = {s.id: s for s in inp.nurses}
        # who the contract, sequence and fairness terms are about: rostered regular nurses.
        # Standby nurses are excluded (5.3); so is the ANS, who works G outside coverage and
        # fairness.
        regular = [s.id for s in inp.nurses if not s.standby and s.role != "ans"]

        # rank 1 coverage: short[d,s] with sum_n x + short >= target_total
        for (d, s), dm in sorted(inp.demand.items()):
            on = sum(xs(n, d, s) for n in self.nurse_ids)
            short = self.m.new_int_var(0, dm.target_total, f"short[{d},{s}]")
            self.m.add(on + short >= dm.target_total)
            self.soft(
                SoftTerm(
                    "coverage",
                    short,
                    WEIGHTS["COVER_SHORT"],
                    dm.target_total,
                    "COVER_SHORT",
                    None,
                    d,
                    s,
                    f"below target {dm.target_total}",
                )
            )

        # rank 2 stability (re-solve only): changed cell vs the published roster, d >= frozen day.
        # A cell the new request removes is "changed" only if the nurse still works it.
        if inp.previous is not None:
            away: dict[tuple[str, int], set[str] | None] = {}  # None = whole day
            for n, d, sh in inp.unavailable:
                cur = away.get((n, d), set())
                away[n, d] = None if sh is None or cur is None else cur | {sh}
            for k in inp.leave:
                away[k] = None
            for n in self.nurse_ids:
                for d in range(inp.frozen_before_day, D):
                    if (n, d) in inp.frozen_cells:
                        continue
                    p = inp.previous.get((n, d))
                    blocked = away.get((n, d), set())
                    removed = p is not None and (n, d) in away and (blocked is None or p in blocked)
                    if removed or p is None or (n, d, p) not in self.x:
                        changed = works(n, d)
                    else:
                        changed = 1 - self.x[n, d, p]
                    rule = "STANDBY_CALLED" if specs[n].standby else "CHANGED"
                    self.soft(
                        SoftTerm("stability", changed, WEIGHTS[rule], 1, rule, n, d, p, WHY[rule])
                    )

        # rank 3 contract: under[n] with sum_d works + credited leave/sick days + under >= min
        credit_cells = {(n, d) for n, d, _s in inp.unavailable} | set(inp.leave)
        for n in regular:
            lo = specs[n].min_shifts
            if lo <= 0:
                continue
            credit = {d for d in days if (n, d) in credit_cells}
            done = sum(works(n, d) for d in days if d not in credit) + len(credit)
            under = self.m.new_int_var(0, lo, f"under[{n}]")
            self.m.add(under >= lo - done)
            self.soft(
                SoftTerm(
                    "contract",
                    under,
                    WEIGHTS["CONTRACT_MIN"],
                    lo,
                    "CONTRACT_MIN",
                    n,
                    why=f"below contract minimum {lo}",
                )
            )

        # rank 4 wellbeing & requests: sequence rules (reach back over the period border)
        weekends = [(d, d + 1) for d in sorted(inp.weekend_days) if d + 1 in inp.weekend_days]
        for n in regular:
            for d in range(-1, D - 1):
                self._pattern("QUICK_RETURN", n, d, [lit(n, d, "E"), lit(n, d + 1, "M")])
                self._pattern(
                    "ISOLATED_DAY",
                    n,
                    d,
                    [lit(n, d, "W"), lit(n, d - 1, "OFF"), lit(n, d + 1, "OFF")],
                )
                self._pattern(
                    "SINGLE_NIGHT",
                    n,
                    d,
                    [lit(n, d, "N"), 1 - lit(n, d - 1, "N"), 1 - lit(n, d + 1, "N")],
                )
            for sat, sun in weekends:
                sw = self.m.new_bool_var(f"SPLIT_WEEKEND[{n},{sat}]")
                self.m.add(sw >= works(n, sat) - works(n, sun))
                self.m.add(sw >= works(n, sun) - works(n, sat))
                self.soft(
                    SoftTerm(
                        TIER_OF["SPLIT_WEEKEND"],
                        sw,
                        WEIGHTS["SPLIT_WEEKEND"],
                        1,
                        "SPLIT_WEEKEND",
                        n,
                        sat,
                        None,
                        WHY["SPLIT_WEEKEND"],
                    )
                )

        # rank 4 requests: one term per requested cell; denied[r] = 1 if any cell is not granted
        cells: dict[str, list[LinearLike]] = {}
        owner: dict[str, str] = {}
        prefs: list[tuple[str, Pref]] = [("PREF_OFF", q) for q in inp.day_off_prefs]
        prefs += [("PREF_SHIFT", q) for q in inp.shift_prefs]
        prefs += [("PREF_AVOID", q) for q in inp.shift_avoids]
        for rule, q in prefs:
            if rule == "PREF_OFF":
                v, w = works(q.nurse, q.day), 2 * q.weight
            elif rule == "PREF_SHIFT":
                v, w = 1 - xs(q.nurse, q.day, q.shift or ""), q.weight
            else:
                v, w = xs(q.nurse, q.day, q.shift or ""), q.weight
            self.soft(SoftTerm("requests", v, w, 1, rule, q.nurse, q.day, q.request_id, WHY[rule]))
            cells.setdefault(q.request_id, []).append(v)
            owner[q.request_id] = q.nurse
        for rid, vs in cells.items():
            den = self.m.new_bool_var(f"denied[{rid}]")
            for v in vs:
                self.m.add(den >= v)
            self.m.add(den <= sum(vs))
            self.denied[rid] = (owner[rid], den)

        # rank 5 fairness: spreads of running totals (history + this period) over regular nurses.
        # FAIR_NIGHTS covers night-qualified nurses only. Nurses on an announced night break stay
        # in, as in the reference solver and its golden values (docs/decisions.md).
        hist = inp.history

        def h(n: str, key: str) -> int:
            rec = hist.get(n)
            return 0 if rec is None else int(getattr(rec, key))

        def total(n: str, key: str, expr: LinearLike) -> tuple[str, LinearLike, int]:
            return (n, h(n, key) + expr, h(n, key) + D)

        # a nurse away for the whole period (e.g. a month of maternity leave) cannot be balanced
        # and would pin the minimum, so she is left out of the spreads
        away_all = {n for n in regular if all((n, d) in credit_cells for d in days)}
        regular = [n for n in regular if n not in away_all]
        night_ok = [n for n in regular if specs[n].can_night]
        self._spread(
            "FAIR_NIGHTS", [total(n, "nights", sum(xs(n, d, "N") for d in days)) for n in night_ok]
        )
        self._spread(
            "FAIR_WEEKENDS",
            [total(n, "weekends", sum(works(n, d) for d in inp.weekend_days)) for n in regular],
        )
        self._spread(
            "FAIR_MORNINGS",
            [total(n, "mornings", sum(xs(n, d, "M") for d in days)) for n in regular],
        )
        self._spread(
            "FAIR_EVENINGS",
            [total(n, "evenings", sum(xs(n, d, "E") for d in days)) for n in regular],
        )
        asked = sorted({n for n, _v in self.denied.values() if n in regular})
        self._spread(
            "FAIR_DENIALS",
            [
                (
                    n,
                    h(n, "denied") + sum(v for m, v in self.denied.values() if m == n),
                    h(n, "denied") + len(cells),
                )
                for n in asked
            ],
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
