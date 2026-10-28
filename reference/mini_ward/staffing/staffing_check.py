"""Staffing check prototype: minimum headcount and what-if staffing on the mini ward.

Reuses mini_solver.RosterModel (hard rules 5.2 + coverage tier) and adds one bool per nurse
saying whether that nurse is used. Only hard rules, approved leave and coverage matter here;
requests, sequence quality and fairness are ignored, so the numbers are lower bounds on what a
publishable roster needs.

    python staffing_check.py ../mini_ward.json
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mini_solver import SHIFTS, Ctx, RosterModel, _solver, load, violations  # noqa: E402
from ortools.sat.python import cp_model  # noqa: E402

CLEAN = {"consecutive_days": 0, "consecutive_nights": 0, "last_days": ["-", "-", "-"],
         "nights": 0, "evenings": 0, "weekends": 0, "denied": 0}


def with_hires(inst, k):
    """Add k hypothetical night-qualified regular nurses H1..Hk with clean history."""
    ids = tuple(f"H{i}" for i in range(1, k + 1))
    ref = inst.regular[0]
    return replace(
        inst,
        nurses=inst.nurses + ids, regular=inst.regular + ids,
        can_night={**inst.can_night, **{h: True for h in ids}},
        names={**inst.names, **{h: f"Hire {h}" for h in ids}},
        min_shifts={**inst.min_shifts, **{h: inst.min_shifts[ref] for h in ids}},
        max_shifts={**inst.max_shifts, **{h: inst.max_shifts[ref] for h in ids}},
        hist={**inst.hist, **{h: CLEAN for h in ids}},
    )


def without(inst, gone):
    """Remove nurses from the pool (long leave, transfer)."""
    return replace(inst, nurses=tuple(n for n in inst.nurses if n not in gone),
                   regular=tuple(n for n in inst.regular if n not in gone),
                   requests=tuple(r for r in inst.requests if r["nurse_id"] not in gone))


def check(inst, *, allow_standby=False, fixed=(), cover="target", seconds=10.0, workers=4):
    """Lexicographic: (1) coverage shortfall vs target, (2) nurses used, (3) standby shifts,
    (4) balance: weighted max-min spreads of total shifts, nights, evenings and weekend shifts
    over the nurses used (this period only; the roster's fairness tier adds history).

    fixed: nurses that count as used regardless (existing staff when asking "how many extra?").
    cover="floor" drops the soft target and asks only for the hard floor.
    """
    t0 = time.time()
    ctx = Ctx(allow_standby=allow_standby)
    rm = RosterModel(inst, ctx)
    m, D = rm.m, inst.days
    used = {n: m.new_bool_var(f"used[{n}]") for n in inst.regular}
    for n in inst.regular:
        for d in range(D):
            m.add(rm.works[n, d] <= used[n])
        if n in fixed:
            m.add(used[n] == 1)
    short = rm.tier_expr("coverage") if cover == "target" else 0
    standby_shifts = sum(rm.works[n, d] for n in inst.standby for d in range(D)) if allow_standby else 0
    credit = {n: sum(1 for (m_, d) in __import__("mini_solver").hard_unavailable(inst, ctx) if m_ == n)
              for n in inst.regular}
    big = 2 * D
    spreads = []
    for w, who, val in (
            (2, inst.regular, lambda n: sum(rm.works[n, d] for d in range(D)) + credit[n]),
            (2, [n for n in inst.regular if inst.can_night[n]], lambda n: sum(rm.x[n, d, "N"] for d in range(D))),
            (1, inst.regular, lambda n: sum(rm.x[n, d, "E"] for d in range(D))),
            (2, inst.regular, lambda n: sum(rm.works[n, d] for d in inst.weekend_days))):
        hi, lo = m.new_int_var(0, big, ""), m.new_int_var(0, big, "")
        for n in who:                      # only nurses in use count toward the spread
            m.add(hi >= val(n) - big * (1 - used[n]))
            m.add(lo <= val(n) + big * (1 - used[n]))
        spreads.append(w * (hi - lo))
    stages = []
    for name, expr in (("shortfall", short), ("nurses_used", sum(used.values())),
                       ("standby_shifts", standby_shifts), ("balance", sum(spreads))):
        if isinstance(expr, int):
            stages.append({"stage": name, "value": expr, "proven": True})
            continue
        m.minimize(expr)
        s = _solver(seconds, workers)
        st = s.solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return {"status": s.status_name(st), "stages": stages, "secs": round(time.time() - t0, 2)}
        best = int(round(s.objective_value))
        stages.append({"stage": name, "value": best, "proven": st == cp_model.OPTIMAL,
                       "bound": int(round(s.best_objective_bound))})
        m.add(expr <= best)
        m.clear_hints()
        for k, v in rm.x.items():
            m.add_hint(v, s.value(v))
    roster = rm.roster(s)
    bad = violations(inst, ctx, roster)
    assert not bad, bad
    short_cells = [f"d{d}{sh}" for d in range(D) for sh in SHIFTS
                   if sum(roster[n, d] == sh for n in inst.nurses) < inst.target[d, sh]]
    return {"status": "solved", "stages": stages, "secs": round(time.time() - t0, 2),
            "used": sorted(n for n in inst.regular if s.value(used[n])),
            "short_cells": short_cells,
            "load": {n: {"total": sum(roster[n, d] is not None for d in range(D)),
                         **{sh: sum(roster[n, d] == sh for d in range(D)) for sh in SHIFTS},
                         "wkend": sum(roster[n, d] is not None for d in inst.weekend_days)}
                     for n in inst.nurses if any(roster[n, d] for d in range(D))}}


def main():
    inst = load(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[3] / "data/seed/mini_ward.json"))
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    slots = sum(inst.target.values())
    floor = sum(inst.floor.values())
    print(f"target slots {slots}, floor slots {floor}, regular {len(inst.regular)}, "
          f"max per nurse {inst.max_shifts[inst.regular[0]]}")
    runs = {
        "A. today's staff, no standby": check(inst, fixed=inst.regular, seconds=secs),
        "B. extra hires needed (staff fixed, up to 3 hires)":
            check(with_hires(inst, 3), fixed=inst.regular, seconds=secs),
        "C. minimum headcount from scratch (staff + 3 hires, any subset)":
            check(with_hires(inst, 3), seconds=secs),
        "D. minimum headcount for the hard floor only": check(with_hires(inst, 3), cover="floor", seconds=secs),
        "E. what-if Kavya (N04) off all period, standby allowed":
            check(without(inst, {"N04"}), allow_standby=True, fixed=set(inst.regular) - {"N04"}, seconds=secs),
        "F. what-if Kavya + Deepa off, standby allowed":
            check(without(inst, {"N04", "N07"}), allow_standby=True,
                  fixed=set(inst.regular) - {"N04", "N07"}, seconds=secs),
    }
    for k, r in runs.items():
        print(f"\n{k}  ({r['secs']} s)")
        print("  " + ", ".join(f"{s['stage']}={s['value']}{'' if s['proven'] else ' (best found)'}"
                               for s in r["stages"]))
        if r["status"] == "solved":
            print(f"  used {r['used']}")
            print(f"  short {r['short_cells']}")
            print("  nurse  tot  M  E  N  wkend")
            for n, l in r["load"].items():
                print(f"  {n:5} {l['total']:4} {l['M']:2} {l['E']:2} {l['N']:2} {l['wkend']:4}")
    json.dump(runs, open(Path(__file__).with_name("staffing_results.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
