"""Reference CP-SAT model for the mini ward fixture (mini_ward.json).

Implements the design doc's model (hard rules 5.2, tiers 5.3, lexicographic solve 5.4,
minimal-change re-solve 6.2, sick-call ranking 6.4) plus the benchmark-style extension terms
listed in rules.soft with core=false. A pure-Python evaluator recomputes every hard rule and
every soft term from a roster, and every solve is cross-checked against it.

    python mini_solver.py mini_ward.json            # report to stdout
    python mini_solver.py mini_ward.json --write expected.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
from dataclasses import dataclass, field, replace

from ortools.sat.python import cp_model

SHIFTS = ("M", "E", "N")
TIERS = ("coverage", "stability", "contract", "requests", "fairness")
WORKERS = 8


# ---------------------------------------------------------------- instance

@dataclass(frozen=True)
class Inst:
    raw: dict
    days: int
    start: dt.date
    nurses: tuple[str, ...]
    regular: tuple[str, ...]
    standby: tuple[str, ...]
    can_night: dict[str, bool]
    names: dict[str, str]
    min_shifts: dict[str, int]
    max_shifts: dict[str, int]
    target: dict[tuple[int, str], int]
    floor: dict[tuple[int, str], int]
    weekends: tuple[tuple[int, int], ...]
    hist: dict[str, dict]
    requests: tuple[dict, ...]
    weights: dict[str, int]
    extensions: frozenset[str]
    max_cd: int
    max_cn: int
    night_recovery_hard: bool = False
    max_nights: int | None = None             # per horizon, scaled from max_nights_per_month
    tier_of: dict = field(default_factory=dict)

    def day(self, iso: str) -> int:
        return (dt.date.fromisoformat(iso) - self.start).days

    @property
    def weekend_days(self) -> frozenset[int]:
        return frozenset(d for w in self.weekends for d in w)


def load(path: str) -> Inst:
    raw = json.load(open(path))
    w = raw["ward"]
    start = dt.date.fromisoformat(w["horizon_start"])
    days = w["horizon_days"]
    weeks = days / 7
    nurses = tuple(n["id"] for n in w["nurses"])
    standby = tuple(n["id"] for n in w["nurses"] if n.get("standby"))
    regular = tuple(n for n in nurses if n not in standby)
    target, floor = {}, {}
    for dm in w["demand"]:
        k = ((dt.date.fromisoformat(dm["day"]) - start).days, dm["shift"])
        target[k], floor[k] = dm["target_total"], dm["min_total"]
    sats = [d for d in range(days) if (start + dt.timedelta(d)).weekday() == 5]
    weekends = tuple((d, d + 1) for d in sats if d + 1 < days)
    soft = raw["rules"]["soft"]
    return Inst(
        raw=raw, days=days, start=start, nurses=nurses, regular=regular, standby=standby,
        can_night={n["id"]: n["can_night"] for n in w["nurses"]},
        names={n["id"]: n["name"] for n in w["nurses"]},
        # design doc 4.1: scale per-week contract, min rounded down, max rounded up
        min_shifts={n["id"]: math.floor(n["min_shifts_per_week"] * weeks) for n in w["nurses"]},
        max_shifts={n["id"]: math.ceil(n["max_shifts_per_week"] * weeks) for n in w["nurses"]},
        target=target, floor=floor, weekends=weekends,
        hist=raw["history"]["nurses"], requests=tuple(raw["requests"]),
        weights={s["rule"]: s["weight"] for s in soft if isinstance(s["weight"], int)},
        tier_of={s["rule"]: s["tier"] for s in soft},
        extensions=frozenset(s["rule"] for s in soft if not s["core"]),
        max_cd=w["max_consecutive_days"], max_cn=w["max_consecutive_nights"],
        night_recovery_hard=raw["rules"]["hard"].get("night_recovery_hard", False),
        max_nights=(math.ceil(w["max_nights_per_month"] * days / 30) if "max_nights_per_month" in w else None),
    )


@dataclass(frozen=True)
class Ctx:
    """Everything about one solve that is not the ward itself."""
    previous: dict | None = None            # published roster (re-solve)
    frozen_before: int = 0
    extra_unavailable: tuple = ()           # (nurse, day, shift|None) from a confirmed edit
    forced: tuple = ()                      # (nurse, day, shift)
    allow_standby: bool = False             # False in a full run (standby_off_roster)
    priorities: tuple[str, ...] = TIERS
    use_history: bool = True
    extensions: bool = True

    def hist(self, inst: Inst, n: str) -> dict:
        if self.use_history:
            return inst.hist[n]
        return {"consecutive_days": 0, "consecutive_nights": 0, "last_days": ["-", "-", "-"],
                "nights": 0, "mornings": 0, "evenings": 0, "weekends": 0, "denied": 0}


def hard_unavailable(inst: Inst, ctx: Ctx) -> dict[tuple[str, int], set | None]:
    """(nurse, day) -> set of blocked shifts, or None for the whole day; with group name."""
    out: dict[tuple[str, int], tuple[str, set | None]] = {}
    def add(n, d, shifts, group):
        g, b = out.get((n, d), (group, set()))
        out[(n, d)] = (g, None if shifts is None or b is None else b | set(shifts))
    for r in inst.requests:
        if r["kind"] in ("unavailable", "leave") and r["status"] == "active":
            for iso in r["dates"]:
                add(r["nurse_id"], inst.day(iso), r["shifts"], r["kind"])
    for n, d, s in ctx.extra_unavailable:
        add(n, d, None if s is None else [s], "unavailable")
    return out


def credit_days(inst: Inst, ctx: Ctx, n: str) -> set[int]:
    """Leave and unavailable days count toward the contract minimum."""
    return {d for (m, d) in hard_unavailable(inst, ctx) if m == n}


def soft_requests(inst: Inst):
    """Yields (request, list of (day, shift|None, kind))."""
    for r in inst.requests:
        if r["status"] != "active" or r["kind"] not in ("day_off", "shift_wanted", "shift_avoid"):
            continue
        cells = []
        for iso in r["dates"]:
            d = inst.day(iso)
            if r["kind"] == "day_off":
                cells.append((d, None))
            else:
                for s in r["shifts"]:
                    cells.append((d, s))
        yield r, cells


# ---------------------------------------------------------------- pure-Python evaluator

def code(inst, ctx, roster, n, d):
    """Shift code for day d, using carry-in history for d < 0. None = off."""
    if d >= 0:
        return roster.get((n, d))
    last = ctx.hist(inst, n)["last_days"]
    c = last[d] if -d <= len(last) else None
    return None if c in ("-", None) else c


def runs_ok(seq_flags, carry, limit):
    run = carry
    for f in seq_flags:
        run = run + 1 if f else 0
        if run > limit:
            return False
    return True


def violations(inst: Inst, ctx: Ctx, roster: dict, only: str | None = None) -> list[str]:
    """Independent check of every hard rule in design doc 5.2. Returns named violations."""
    v = []
    un = hard_unavailable(inst, ctx)
    nurses = [only] if only else inst.nurses
    for n in nurses:
        h = ctx.hist(inst, n)
        work = [roster.get((n, d)) is not None for d in range(inst.days)]
        night = [roster.get((n, d)) == "N" for d in range(inst.days)]
        for d in range(inst.days):
            s = roster.get((n, d))
            if s is None:
                continue
            if (n, d) in un:
                group, blocked = un[(n, d)]
                if blocked is None or s in blocked:
                    v.append(f"{group}:{n}:d{d}")
            if s == "N" and not inst.can_night[n]:
                v.append(f"night_qualified:{n}:d{d}")
            if n in inst.standby and not ctx.allow_standby:
                v.append(f"standby_off_roster:{n}:d{d}")
        for d in range(-1, inst.days - 1):
            if code(inst, ctx, roster, n, d) == "N" and code(inst, ctx, roster, n, d + 1) in ("M", "E"):
                v.append(f"rest_after_night:{n}:d{d}")
        if inst.night_recovery_hard:
            for d in range(-2, inst.days - 2):
                if (code(inst, ctx, roster, n, d) == "N" and code(inst, ctx, roster, n, d + 1) is None
                        and code(inst, ctx, roster, n, d + 2) in ("M", "E")):
                    v.append(f"night_recovery:{n}:d{d}")
        if not runs_ok(work, h["consecutive_days"], inst.max_cd):
            v.append(f"max_consecutive_days:{n}")
        if not runs_ok(night, h["consecutive_nights"], inst.max_cn):
            v.append(f"max_consecutive_nights:{n}")
        if sum(work) > inst.max_shifts[n]:
            v.append(f"max_shifts:{n}")
        if inst.max_nights is not None and sum(night) > inst.max_nights:
            v.append(f"max_nights:{n}")
        if ctx.previous is not None:
            for d in range(ctx.frozen_before):
                if roster.get((n, d)) != ctx.previous.get((n, d)):
                    v.append(f"frozen:{n}:d{d}")
        for fn, fd, fs in ctx.forced:
            if fn == n and roster.get((n, fd)) != fs:
                v.append(f"forced:{n}:d{fd}:{fs}")
    if only is None:
        for d in range(inst.days):
            for s in SHIFTS:
                c = sum(roster.get((n, d)) == s for n in inst.nurses)
                if c < inst.floor[(d, s)]:
                    v.append(f"min_cover:d{d}:{s}")
                if c > inst.target[(d, s)]:
                    v.append(f"max_cover:d{d}:{s}")
    return v


def soft_terms(inst: Inst, ctx: Ctx, roster: dict) -> list[dict]:
    """Every soft term with a positive value: tier, rule, nurse, day, amount, weight, cost."""
    out = []
    def add(tier, rule, amount, weight, nurse=None, day=None, ref=None):
        if amount:
            out.append({"tier": tier, "rule": rule, "nurse": nurse, "day": day, "ref": ref,
                        "amount": amount, "weight": weight, "cost": amount * weight})
    W = inst.weights
    un = hard_unavailable(inst, ctx)
    # coverage
    for d in range(inst.days):
        for s in SHIFTS:
            c = sum(roster.get((n, d)) == s for n in inst.nurses)
            add("coverage", "COVER_SHORT", max(0, inst.target[(d, s)] - c), W["COVER_SHORT"], day=d, ref=s)
    # stability
    if ctx.previous is not None:
        for n in inst.nurses:
            for d in range(ctx.frozen_before, inst.days):
                p, s = ctx.previous.get((n, d)), roster.get((n, d))
                removed = p is not None and (n, d) in un and (un[(n, d)][1] is None or p in un[(n, d)][1])
                changed = (s is not None) if removed else (s != p)
                if n in inst.standby:
                    add("stability", "STANDBY_CALLED", int(changed), W["STANDBY_CALLED"], n, d)
                else:
                    add("stability", "CHANGED", int(changed), W["CHANGED"], n, d)
    # contract
    for n in inst.regular:
        credit = credit_days(inst, ctx, n)
        done = sum(1 for d in range(inst.days) if d in credit or roster.get((n, d)) is not None)
        add("contract", "CONTRACT_MIN", max(0, inst.min_shifts[n] - done), W["CONTRACT_MIN"], n)
        if not ctx.extensions:
            continue
        c = lambda d: code(inst, ctx, roster, n, d)
        for d in range(-1, inst.days - 1):
            add(inst.tier_of["QUICK_RETURN"], "QUICK_RETURN", int(c(d) == "E" and c(d + 1) == "M"), W["QUICK_RETURN"], n, d)
            add(inst.tier_of["ISOLATED_DAY"], "ISOLATED_DAY",
                int(c(d) is not None and c(d - 1) is None and c(d + 1) is None), W["ISOLATED_DAY"], n, d)
            add(inst.tier_of["SINGLE_NIGHT"], "SINGLE_NIGHT",
                int(c(d) == "N" and c(d - 1) != "N" and c(d + 1) != "N"), W["SINGLE_NIGHT"], n, d)
        for d in range(-2, inst.days - 2) if "NIGHT_RECOVERY" in W and not inst.night_recovery_hard else ():
            add(inst.tier_of["NIGHT_RECOVERY"], "NIGHT_RECOVERY",
                int(c(d) == "N" and c(d + 1) is None and c(d + 2) in ("M", "E")), W["NIGHT_RECOVERY"], n, d)
        for sat, sun in inst.weekends:
            add(inst.tier_of["SPLIT_WEEKEND"], "SPLIT_WEEKEND", int((c(sat) is None) != (c(sun) is None)),
                W["SPLIT_WEEKEND"], n, sat)
    # requests
    denied = {}
    for r, cells in soft_requests(inst):
        n, k = r["nurse_id"], r["kind"]
        any_denied = False
        for d, s in cells:
            got = roster.get((n, d))
            if k == "day_off":
                bad, rule, w = got is not None, "PREF_OFF", 2 * r["weight"]
            elif k == "shift_wanted":
                bad, rule, w = got != s, "PREF_SHIFT", r["weight"]
            else:
                bad, rule, w = got == s, "PREF_AVOID", r["weight"]
            add("requests", rule, int(bad), w, n, d, r["id"])
            any_denied |= bad
        denied[r["id"]] = any_denied
    # fairness (running totals: history + this period)
    def spread(vals):
        return max(vals) - min(vals) if vals else 0
    qual = [n for n in inst.regular if inst.can_night[n]]
    count = lambda n, pred: sum(1 for d in range(inst.days) if pred(d, roster.get((n, d))))
    add("fairness", "FAIR_NIGHTS",
        spread([ctx.hist(inst, n)["nights"] + count(n, lambda d, s: s == "N") for n in qual]), W["FAIR_NIGHTS"])
    add("fairness", "FAIR_WEEKENDS",
        spread([ctx.hist(inst, n)["weekends"] + count(n, lambda d, s: s and d in inst.weekend_days)
                for n in inst.regular]), W["FAIR_WEEKENDS"])
    if "FAIR_MORNINGS" in W:
        add("fairness", "FAIR_MORNINGS",
            spread([ctx.hist(inst, n).get("mornings", 0) + count(n, lambda d, s: s == "M") for n in inst.regular]),
            W["FAIR_MORNINGS"])
    add("fairness", "FAIR_EVENINGS",
        spread([ctx.hist(inst, n)["evenings"] + count(n, lambda d, s: s == "E") for n in inst.regular]),
        W["FAIR_EVENINGS"])
    req_nurses = sorted({r["nurse_id"] for r, _ in soft_requests(inst) if r["nurse_id"] in inst.regular})
    add("fairness", "FAIR_DENIALS",
        spread([ctx.hist(inst, n)["denied"] + sum(denied[r["id"]] for r, _ in soft_requests(inst)
                                                  if r["nurse_id"] == n) for n in req_nurses]),
        W["FAIR_DENIALS"])
    return out


def tier_vector(terms, priorities=TIERS) -> dict[str, int]:
    return {t: sum(x["cost"] for x in terms if x["tier"] == t) for t in priorities}


# ---------------------------------------------------------------- CP-SAT model

class RosterModel:
    def __init__(self, inst: Inst, ctx: Ctx):
        self.inst, self.ctx = inst, ctx
        m = self.m = cp_model.CpModel()
        D = inst.days
        self.x = {(n, d, s): m.new_bool_var(f"x[{n},{d},{s}]")
                  for n in inst.nurses for d in range(D) for s in SHIFTS}
        x = self.x
        self.terms: dict[str, list] = {t: [] for t in TIERS}   # tier -> [(weight, expr)]
        works = self.works = {(n, d): sum(x[n, d, s] for s in SHIFTS) for n in inst.nurses for d in range(D)}
        un = hard_unavailable(inst, ctx)

        def lit(n, d, what):
            """0/1 expression (or constant) for: shift code / 'W' worked / 'D' day shift / 'OFF'."""
            if d < 0:
                c = code(inst, ctx, {}, n, d)
                return int({"W": c is not None, "OFF": c is None, "D": c in ("M", "E")}.get(what, c == what))
            if what == "W":
                return works[n, d]
            if what == "OFF":
                return 1 - works[n, d]
            if what == "D":
                return x[n, d, "M"] + x[n, d, "E"]
            return x[n, d, what]
        self.lit = lit

        for n in inst.nurses:
            h = ctx.hist(inst, n)
            for d in range(D):
                m.add(works[n, d] <= 1)                                     # one_shift_per_day
                if (n, d) in un:                                            # unavailable / leave
                    blocked = un[(n, d)][1]
                    for s in (SHIFTS if blocked is None else blocked):
                        m.add(x[n, d, s] == 0)
                if not inst.can_night[n]:                                   # night_qualified
                    m.add(x[n, d, "N"] == 0)
                if n in inst.standby and not ctx.allow_standby:             # standby_off_roster
                    m.add(works[n, d] == 0)
            for d in range(-1, D - 1):                                      # rest_after_night
                a, b = lit(n, d, "N"), lit(n, d + 1, "D")
                if isinstance(a, int) and a == 0:
                    continue
                m.add(a + b <= 1)
            if inst.night_recovery_hard:                                    # night_recovery
                for d in range(-2, D - 2):
                    a, b, c = lit(n, d, "N"), lit(n, d + 1, "OFF"), lit(n, d + 2, "D")
                    if any(isinstance(t, int) and t == 0 for t in (a, b, c)):
                        continue
                    m.add(a + b + c <= 2)
            for K, carry, what in ((inst.max_cd, h["consecutive_days"], "W"),
                                   (inst.max_cn, h["consecutive_nights"], "N")):
                for t in range(-K, D - K):                                  # windows of K+1 days
                    hist_days = max(0, -max(t, -carry)) if t < 0 else 0
                    expr = sum(lit(n, d, what) for d in range(max(t, 0), t + K + 1))
                    m.add(expr + hist_days <= K)
            m.add(sum(works[n, d] for d in range(D)) <= inst.max_shifts[n])  # max_shifts
            if inst.max_nights is not None:                                 # max_nights
                m.add(sum(x[n, d, "N"] for d in range(D)) <= inst.max_nights)
        for d in range(D):
            for s in SHIFTS:
                c = sum(x[n, d, s] for n in inst.nurses)
                m.add(c >= inst.floor[d, s])                                # min_cover
                m.add(c <= inst.target[d, s])                               # max_cover
                self.soft("coverage", inst.weights["COVER_SHORT"], inst.target[d, s] - c)
        if ctx.previous is not None:
            for n in inst.nurses:
                for d in range(D):
                    p = ctx.previous.get((n, d))
                    if d < ctx.frozen_before:                               # frozen
                        for s in SHIFTS:
                            m.add(x[n, d, s] == int(p == s))
                        continue
                    for s in SHIFTS:
                        m.add_hint(x[n, d, s], int(p == s))
                    removed = p is not None and (n, d) in un and (un[(n, d)][1] is None or p in un[(n, d)][1])
                    changed = works[n, d] if (removed or p is None) else 1 - x[n, d, p]
                    w = inst.weights["STANDBY_CALLED" if n in inst.standby else "CHANGED"]
                    self.soft("stability", w, changed)
        for n, d, s in ctx.forced:
            m.add(x[n, d, s] == 1)

        # contract tier
        for n in inst.regular:
            credit = credit_days(inst, ctx, n)
            done = sum(works[n, d] for d in range(D) if d not in credit) + len(credit)
            under = m.new_int_var(0, inst.min_shifts[n], f"under[{n}]")
            m.add(under >= inst.min_shifts[n] - done)
            self.soft("contract", inst.weights["CONTRACT_MIN"], under)
            if not ctx.extensions:
                continue
            W = inst.weights
            for d in range(-1, D - 1):
                self.pattern("QUICK_RETURN", [lit(n, d, "E"), lit(n, d + 1, "M")])
                self.pattern("ISOLATED_DAY", [lit(n, d, "W"), lit(n, d - 1, "OFF"),
                                                 lit(n, d + 1, "OFF") if d + 1 < D else 1])
                self.pattern("SINGLE_NIGHT", [lit(n, d, "N"), 1 - lit(n, d - 1, "N"), 1 - lit(n, d + 1, "N")])
            for d in range(-2, D - 2) if "NIGHT_RECOVERY" in W and not inst.night_recovery_hard else ():
                self.pattern("NIGHT_RECOVERY", [lit(n, d, "N"), lit(n, d + 1, "OFF"), lit(n, d + 2, "D")])
            for sat, sun in inst.weekends:
                sw = m.new_bool_var("")
                m.add(sw >= works[n, sat] - works[n, sun])
                m.add(sw >= works[n, sun] - works[n, sat])
                self.soft(inst.tier_of["SPLIT_WEEKEND"], W["SPLIT_WEEKEND"], sw)

        # requests tier
        self.denied = {}
        for r, cells in soft_requests(inst):
            n = r["nurse_id"]
            viol = []
            for d, s in cells:
                if r["kind"] == "day_off":
                    v, w = works[n, d], 2 * r["weight"]
                elif r["kind"] == "shift_wanted":
                    v, w = 1 - x[n, d, s], r["weight"]
                else:
                    v, w = x[n, d, s], r["weight"]
                self.soft("requests", w, v)
                viol.append(v)
            den = m.new_bool_var(f"denied[{r['id']}]")
            for v in viol:
                m.add(den >= v)
            m.add(den <= sum(viol))
            self.denied[r["id"]] = (n, den)

        # fairness tier
        qual = [n for n in inst.regular if inst.can_night[n]]
        H = lambda n, k: ctx.hist(inst, n).get(k, 0)
        self.spread("FAIR_NIGHTS", [H(n, "nights") + sum(x[n, d, "N"] for d in range(D)) for n in qual])
        self.spread("FAIR_WEEKENDS", [H(n, "weekends") + sum(works[n, d] for d in inst.weekend_days)
                                      for n in inst.regular])
        if "FAIR_MORNINGS" in inst.weights:
            self.spread("FAIR_MORNINGS", [H(n, "mornings") + sum(x[n, d, "M"] for d in range(D))
                                          for n in inst.regular])
        self.spread("FAIR_EVENINGS", [H(n, "evenings") + sum(x[n, d, "E"] for d in range(D))
                                      for n in inst.regular])
        req_nurses = sorted({n for n, _ in self.denied.values() if n in inst.regular})
        self.spread("FAIR_DENIALS", [H(n, "denied") + sum(v for m_, v in self.denied.values() if m_ == n)
                                     for n in req_nurses])

    def soft(self, tier, weight, expr):
        self.terms[tier].append((weight, expr))

    def pattern(self, rule, lits):
        """Penalty bool that is 1 when every 0/1 literal in lits is 1."""
        weight, tier = self.inst.weights[rule], self.inst.tier_of[rule]
        if any(isinstance(l, int) and l == 0 for l in lits):
            return
        var = [l for l in lits if not isinstance(l, int)]
        if not var:
            self.soft(tier, weight, 1)
            return
        p = self.m.new_bool_var("")
        self.m.add(p >= sum(var) - (len(var) - 1))
        self.soft(tier, weight, p)

    def spread(self, rule, vals):
        if not vals:
            return
        hi, lo = self.m.new_int_var(0, 1000, ""), self.m.new_int_var(0, 1000, "")
        for v in vals:                      # minimised, so inequalities suffice
            self.m.add(hi >= v)
            self.m.add(lo <= v)
        self.soft("fairness", self.inst.weights[rule], hi - lo)

    def tier_expr(self, tier):
        return sum(w * e for w, e in self.terms[tier])

    def roster(self, solver) -> dict:
        r = {}
        for n in self.inst.nurses:
            for d in range(self.inst.days):
                r[(n, d)] = next((s for s in SHIFTS if solver.value(self.x[n, d, s])), None)
        return r


@dataclass
class Result:
    status: str
    roster: dict = field(default_factory=dict)
    stages: list = field(default_factory=list)
    vector: dict = field(default_factory=dict)
    secs: float = 0.0


def solve(inst: Inst, ctx: Ctx, *, stage_seconds=10.0, workers=None, weighted=False) -> Result:
    t0 = time.time()
    workers = workers or WORKERS
    rm = RosterModel(inst, ctx)
    m = rm.m
    stages = []
    solver = None
    if weighted:
        # dominance weights: each tier's weight exceeds the worst case of all lower tiers combined
        ub = {"coverage": 100, "stability": 300, "contract": 1000, "requests": 300, "fairness": 300}
        mult, obj = 1, 0
        for t in reversed(ctx.priorities):
            obj += mult * rm.tier_expr(t)
            mult *= ub[t] + 1
        m.minimize(obj)
        solver = _solver(stage_seconds, workers)
        st = solver.solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return Result("infeasible", secs=time.time() - t0)
        stages.append({"tier": "weighted", "proven": st == cp_model.OPTIMAL})
    else:
        for tier in ctx.priorities:
            if not rm.terms[tier]:
                stages.append({"tier": tier, "value": 0, "proven": True, "skipped": True})
                continue
            pen = rm.tier_expr(tier)
            m.minimize(pen)
            solver = _solver(stage_seconds, workers)
            st = solver.solve(m)
            if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return Result("infeasible", stages=stages + [{"tier": tier, "status": solver.status_name(st)}],
                              secs=time.time() - t0)
            best = int(round(solver.objective_value))
            stages.append({"tier": tier, "value": best, "proven": st == cp_model.OPTIMAL,
                           "bound": int(round(solver.best_objective_bound))})
            m.add(pen <= best)                                            # lock this tier
            # warm start the next stage from this solution
            m.clear_hints()
            for k, v in rm.x.items():
                m.add_hint(v, solver.value(v))
    roster = rm.roster(solver)
    bad = violations(inst, ctx, roster)
    assert not bad, f"solver roster breaks hard rules: {bad}"
    terms = soft_terms(inst, ctx, roster)
    vec = tier_vector(terms, ctx.priorities)
    for s in stages:                                                       # cross-check model vs evaluator
        if "value" in s:
            assert vec[s["tier"]] <= s["value"], (s, vec)   # a later stage may improve an unproven tier
    return Result("solved", roster, stages, vec, time.time() - t0)


def _solver(seconds, workers):
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = seconds
    s.parameters.num_workers = workers
    s.parameters.random_seed = 42
    return s


# ---------------------------------------------------------------- re-solve and sick-call ranking

def changed_cells(inst, ctx, prev, new, added_slots=()):
    """Design doc 6.3: counts replacement and knock-on changes; cells removed by the request are excluded."""
    un = hard_unavailable(inst, ctx)
    out = []
    for (n, d), p in prev.items():
        s = new.get((n, d))
        if s == p:
            continue
        removed = p is not None and (n, d) in un and (un[(n, d)][1] is None or p in un[(n, d)][1])
        if removed and s is None:
            cause = "request"
        elif p is None and (any(prev.get((m, d)) == s and m != n and (m, d) in un for m in inst.nurses)
                            or (d, s) in added_slots):
            cause = "replacement"
        else:
            cause = "knock_on"
        out.append({"nurse": n, "day": d, "from": p, "to": s, "cause": cause})
    return out


def edit_to_ctx(inst, scenario, baseline) -> Ctx:
    e = scenario["edit"]
    un = []
    for iso in e["dates"]:
        for s in (e["shifts"] or [None]):
            un.append((e["nurse"], inst.day(iso), s))
    return Ctx(previous=baseline, frozen_before=scenario["frozen_before_day"],
               extra_unavailable=tuple(un), allow_standby=True)


def apply_scenario(inst, scenario, baseline):
    """Returns (inst, ctx, added_slots). change_demand edits the target (and so max_cover);
    availability edits go through edit_to_ctx."""
    e = scenario["edit"]
    if e["op"] != "change_demand":
        return inst, edit_to_ctx(inst, scenario, baseline), ()
    target = dict(inst.target)
    added = []
    for iso in e["dates"]:
        for s in e["shifts"]:
            k = (inst.day(iso), s)
            if e["target_total"] > target[k]:
                added += [k] * (e["target_total"] - target[k])
            target[k] = e["target_total"]
    ctx = Ctx(previous=baseline, frozen_before=scenario["frozen_before_day"], allow_standby=True)
    return replace(inst, target=target), ctx, tuple(added)


def rank_candidates(inst, ctx, baseline, *, max_forced=8, stage_seconds=5.0, added_slots=()):
    """Design doc 6.4: rule-check filter, then forced re-solves, ranked lexicographically.

    Unlike the doc, nurses already working another shift that day are candidates too ("move"):
    in a small ward almost everyone works on a weekday, so the doc's filter leaves only standby.
    """
    un = hard_unavailable(inst, ctx)
    open_cells = [(n, d, p) for (n, d), p in baseline.items()
                  if p is not None and d >= ctx.frozen_before and (n, d) in un
                  and (un[(n, d)][1] is None or p in un[(n, d)][1])]
    open_cells += [(None, d, s) for d, s in added_slots]   # new slots from a demand change
    results = []
    for _, d, s in open_cells[:1]:          # rank for the first open cell (the demo case has one)
        excluded, survivors = [], []
        for c in inst.nurses:
            if any(c == n for n, _, _ in open_cells) or baseline.get((c, d)) == s:
                continue
            trial = dict(baseline)
            trial[(c, d)] = s
            broken = violations(inst, ctx, trial, only=c)
            (excluded if broken else survivors).append((c, broken))
        hist_shifts = lambda c: sum(1 for dd in range(inst.days) if baseline.get((c, dd)))
        moves = lambda c: baseline.get((c, d)) is not None
        survivors.sort(key=lambda t: (t[0] not in inst.standby, moves(t[0]), hist_shifts(t[0]), t[0]))
        base = solve(inst, ctx, stage_seconds=stage_seconds)
        cands = []
        for c, _ in survivors[:max_forced]:
            r = solve(inst, replace(ctx, forced=ctx.forced + ((c, d, s),)), stage_seconds=stage_seconds)
            if r.status != "solved":
                cands.append({"nurse": c, "status": r.status})
                continue
            ch = changed_cells(inst, ctx, baseline, r.roster, added_slots)
            n_changed = sum(1 for x in ch if x["cause"] != "request")
            cands.append({"nurse": c, "name": inst.names[c], "standby": c in inst.standby,
                          "kind": "move from " + baseline[(c, d)] if moves(c) else "call in",
                          "vector": r.vector, "delta": {t: r.vector[t] - base.vector[t] for t in TIERS},
                          "changed_cells": n_changed, "changes": ch,
                          "proven": all(st.get("proven", True) for st in r.stages)})
        ok = [c for c in cands if "vector" in c]
        ok.sort(key=lambda c: (tuple(c["vector"][t] for t in ctx.priorities), c["changed_cells"], c["nurse"]))
        results.append({"open_cell": {"day": d, "shift": s}, "baseline_vector": base.vector,
                        "ranked": ok, "excluded": [{"nurse": c, "name": inst.names[c], "rules": b}
                                                   for c, b in excluded]})
    return results


# ---------------------------------------------------------------- reporting

def grid(inst, roster, mark=None):
    head = "      " + " ".join((inst.start + dt.timedelta(d)).strftime("%a")[:2] for d in range(inst.days))
    lines = [head, "      " + " ".join(f"{d:>2}" for d in range(inst.days))]
    for n in inst.nurses:
        cells = []
        for d in range(inst.days):
            c = roster.get((n, d)) or "."
            cells.append((c + "*") if mark and (n, d) in mark else f" {c}")
        lines.append(f"{n:<5} " + " ".join(f"{c:>2}" for c in cells) + f"   {inst.names[n]}")
    return "\n".join(lines)


def counts(inst, ctx, roster):
    rows = []
    for n in inst.nurses:
        h = ctx.hist(inst, n)
        sh = [roster.get((n, d)) for d in range(inst.days)]
        rows.append({"nurse": n, "shifts": sum(s is not None for s in sh), "M": sh.count("M"),
                     "E": sh.count("E"), "N": sh.count("N"),
                     "weekend": sum(1 for d in inst.weekend_days if sh[d]),
                     "nights_total": h["nights"] + sh.count("N"),
                     "weekends_total": h["weekends"] + sum(1 for d in inst.weekend_days if sh[d]),
                     "mornings_total": h.get("mornings", 0) + sh.count("M"),
                     "evenings_total": h["evenings"] + sh.count("E")})
    return rows


def display_codes(inst, ctx, roster):
    """What the ward's paper roster shows: M/E/N, N/O for the off day right after a night
    block, L for leave or unavailability, D/O for any other day off (doc 4.1, 4.2)."""
    un = hard_unavailable(inst, ctx)
    out = {}
    for n in inst.nurses:
        row = []
        for d in range(inst.days):
            s = roster.get((n, d))
            if s is not None:
                row.append(s)
            elif (n, d) in un and un[(n, d)][1] is None:
                row.append("L")
            elif code(inst, ctx, roster, n, d - 1) == "N":
                row.append("N/O")
            else:
                row.append("D/O")
        out[n] = row
    return out


def ser_roster(inst, roster):
    return {n: [roster.get((n, d)) for d in range(inst.days)] for n in inst.nurses}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("instance")
    ap.add_argument("--write", help="write expected results JSON here")
    ap.add_argument("--seconds", type=float, default=10.0, help="time limit per lexicographic stage")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--variant-seconds", type=float, default=None, help="stage limit for trade-off runs")
    a = ap.parse_args()
    global WORKERS
    WORKERS = a.workers
    inst = load(a.instance)
    out = {"instance": a.instance, "runs": {}, "scenarios": {}}

    print(f"Mini ward: {len(inst.regular)} regular + {len(inst.standby)} standby, {inst.days} days\n")
    full = Ctx()
    base = solve(inst, full, stage_seconds=a.seconds)
    print("== Baseline (published) roster, default priorities")
    print(grid(inst, base.roster))
    print("stages:", [(s["tier"], s.get("value"), "proven" if s.get("proven") else "best found") for s in base.stages])
    terms = soft_terms(inst, full, base.roster)
    for t in terms:
        print(f"  {t['tier']:<9} {t['rule']:<15} {t['nurse'] or '':<4} d{'' if t['day'] is None else t['day']:<3}"
              f" {t['ref'] or '':<7} x{t['amount']} w{t['weight']} = {t['cost']}")
    for r in counts(inst, full, base.roster):
        print("  ", r)
    out["baseline"] = {"roster": ser_roster(inst, base.roster), "display": display_codes(inst, full, base.roster),
                       "stages": base.stages,
                       "vector": base.vector, "terms": terms, "counts": counts(inst, full, base.roster),
                       "secs": round(base.secs, 2)}

    # trade-off runs
    variants = {
        "fairness_before_requests": Ctx(priorities=("coverage", "stability", "contract", "fairness", "requests")),
        "requests_before_coverage": Ctx(priorities=("requests", "coverage", "stability", "contract", "fairness")),
        "no_history": Ctx(use_history=False),
        "core_rules_only": Ctx(extensions=False),
        "dominance_weighted": Ctx(),
        "sequence_rules_in_contract_tier": Ctx(),     # the doc's literal placement, above requests
    }
    seq_up = replace(inst, tier_of={**inst.tier_of, **{r: "contract" for r in inst.extensions}})
    print("\n== Trade-offs (each row: tier vector in default order, evaluated with full rules and real history)")
    print(f"  {'run':<26}" + "".join(f"{t:>11}" for t in TIERS))
    print(f"  {'baseline':<26}" + "".join(f"{base.vector[t]:>11}" for t in TIERS))
    for name, ctx in variants.items():
        vi = seq_up if name == "sequence_rules_in_contract_tier" else inst
        r = solve(vi, ctx, stage_seconds=a.variant_seconds or a.seconds, weighted=(name == "dominance_weighted"))
        ev = tier_vector(soft_terms(inst, full, r.roster))
        bad = violations(inst, full, r.roster)
        proven = all(s.get("proven", True) for s in r.stages)
        print(f"  {name:<26}" + "".join(f"{ev[t]:>11}" for t in TIERS) + ("" if proven else "   (best found)")
              + (f"   breaks: {bad}" if bad else ""))
        out["runs"][name] = {"vector_under_default_rules": ev, "own_vector": r.vector, "hard_violations_under_real_history": bad,
                             "stages": r.stages, "secs": round(r.secs, 2),
                             "roster": ser_roster(inst, r.roster),
                             "terms": soft_terms(inst, full, r.roster)}

    # sick-call and demand-change scenarios
    base_inst = inst
    for sc in base_inst.raw["scenarios"]:
        inst, ctx, added = apply_scenario(base_inst, sc, base.roster)
        print(f"\n== Scenario {sc['id']}: {sc['title']}")
        r = solve(inst, ctx, stage_seconds=a.seconds)
        ch = changed_cells(inst, ctx, base.roster, r.roster, added)
        print(grid(inst, r.roster, mark={(c["nurse"], c["day"]) for c in ch}))
        print("changes:", ch)
        print("vector:", r.vector, f"{r.secs:.2f}s")
        sc_out = {"roster": ser_roster(inst, r.roster), "vector": r.vector, "changes": ch,
                  "changed_cells": sum(1 for c in ch if c["cause"] != "request"),
                  "terms": soft_terms(inst, ctx, r.roster), "secs": round(r.secs, 2)}
        if sc["edit"]["shifts"]:
            ranking = rank_candidates(inst, ctx, base.roster, stage_seconds=min(a.seconds, 10),
                                      added_slots=added)
            for rk in ranking:
                print("ranked candidates for", rk["open_cell"])
                for c in rk["ranked"]:
                    print(f"   {c['nurse']} {c['name']:<14} standby={c['standby']!s:<5} "
                          f"{c['kind']:<12} delta={c['delta']} changed={c['changed_cells']} proven={c['proven']}")
                for e in rk["excluded"]:
                    print(f"   excluded {e['nurse']} {e['name']}: {e['rules']}")
            sc_out["ranking"] = ranking
        out["scenarios"][sc["id"]] = sc_out

    if a.write:
        json.dump(out, open(a.write, "w"), indent=1, ensure_ascii=False, default=str)


if __name__ == "__main__":
    main()
