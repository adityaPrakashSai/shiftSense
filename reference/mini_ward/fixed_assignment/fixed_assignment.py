"""Fixed-assignment ward fixture (design doc 5.7, stretch milestone M15) and a reference solve.

Hospital B style: every nurse gets one shift and one fixed weekday off for the whole period,
and shifts differ in length (M 7 h, E 6 h, N 11 h). Synthetic: 8 nurses, 16 days.
Run: python3 fixed_assignment.py   (writes fixed_assignment.json with the expected result)
"""
import datetime as dt
import json
import pathlib

from ortools.sat.python import cp_model

HERE = pathlib.Path(__file__).resolve().parent
START = dt.date(2026, 11, 1)            # a Sunday; period 1-16 Nov (half month)
DAYS = 16
SHIFTS = {"M": 7, "E": 6, "N": 11}
TARGET = {"M": 3, "E": 2, "N": 2}        # 7 on duty every day out of 8 nurses
FLOOR = {"M": 2, "E": 1, "N": 1}        # a fixed weekday off always leaves some day one short
MAX = {"M": 4, "E": 3, "N": 3}
NAMES = ["Anita", "Bhavna", "Chitra", "Daisy", "Esther", "Farah", "Gauri", "Hema"]
PREV = {"N1": "N", "N2": "N", "N3": "E", "N4": "E", "N5": "M", "N6": "M", "N7": "M", "N8": "M"}
LEAVE = {("N4", d) for d in range(5, 8)}  # 6-8 Nov, CL


def build():
    return {
        "schema": "shiftsense.ward_seed/v1",
        "about": "Synthetic fixed-assignment ward (Hospital B style). Never add real names.",
        "ward": {
            "id": "ward-fixed", "name": "Fixed-assignment test ward", "tz": "Asia/Kolkata",
            "roster_style": "fixed_assignment", "horizon_preset": "half_month",
            "horizon_start": START.isoformat(), "horizon_days": DAYS,
            "shifts": [{"code": s, "hours": h} for s, h in SHIFTS.items()],
            "nurses": [{"id": f"N{i + 1}", "name": f"{NAMES[i]} (synthetic)", "grade": "staff",
                        "can_night": i != 7, "previous_period_shift": PREV[f"N{i + 1}"]} for i in range(8)],
            "demand": [{"day": (START + dt.timedelta(d)).isoformat(), "shift": s, "min_total": FLOOR[s],
                        "target_total": TARGET[s], "max_total": MAX[s]} for d in range(DAYS) for s in SHIFTS],
        },
        "requests": [{"id": "R-F001", "nurse_id": "N4", "kind": "leave", "leave_type": "CL",
                      "dates": [(START + dt.timedelta(d)).isoformat() for d in (5, 6, 7)], "shifts": None}],
    }


def solve(seed):
    nurses = [n["id"] for n in seed["ward"]["nurses"]]
    canN = {n["id"]: n["can_night"] for n in seed["ward"]["nurses"]}
    m = cp_model.CpModel()
    y = {(n, s): m.new_bool_var(f"y{n}{s}") for n in nurses for s in SHIFTS}
    w = {(n, k): m.new_bool_var(f"w{n}{k}") for n in nurses for k in range(7)}
    x = {}
    for n in nurses:
        m.add_exactly_one(y[n, s] for s in SHIFTS)
        m.add_exactly_one(w[n, k] for k in range(7))
        if not canN[n]:
            m.add(y[n, "N"] == 0)
        for d in range(DAYS):
            wd = (START + dt.timedelta(d)).weekday()
            for s in SHIFTS:
                v = x[n, d, s] = m.new_bool_var("")
                if (n, d) in LEAVE:
                    m.add(v == 0)
                else:   # x = y AND NOT w[weekday]
                    m.add(v <= y[n, s]); m.add(v <= 1 - w[n, wd]); m.add(v >= y[n, s] - w[n, wd])
    short = []
    for d in range(DAYS):
        for s in SHIFTS:
            c = sum(x[n, d, s] for n in nurses)
            m.add(c >= FLOOR[s]); m.add(c <= MAX[s])
            sh = m.new_int_var(0, TARGET[s], "")
            m.add(sh >= TARGET[s] - c)
            short.append(sh)
    # tier 1 coverage; tier 2 stability (keep last period's shift); tier 3 night rotation
    stab = sum(1 - y[n, PREV[n]] for n in nurses)
    nights_again = sum(y[n, "N"] for n in nurses if PREV[n] == "N")
    tiers = [("coverage", sum(short)), ("rotation", nights_again), ("stability", stab)]
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 8
    solver.parameters.max_time_in_seconds = 20
    vals = {}
    for name, e in tiers:
        m.minimize(e)
        st = solver.solve(m)
        assert st == cp_model.OPTIMAL, (name, solver.status_name(st), vals)
        vals[name] = int(solver.objective_value)
        m.add(e == vals[name])
        m.clear_hints()
        for v in list(y.values()) + list(w.values()):
            m.add_hint(v, solver.value(v))
    m.clear_objective()
    st = solver.solve(m)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assign = {n: {"shift": next(s for s in SHIFTS if solver.value(y[n, s])),
                  "weekday_off": days[next(k for k in range(7) if solver.value(w[n, k]))]} for n in nurses}
    return {"tier_values": vals, "assignment": assign,
            "note": "Tier order for this fixture: coverage, then night rotation (last period's night nurses "
                    "not on nights again), then stability. The app's order may differ; the asserted facts "
                    "are coverage and the rotation value."}


if __name__ == "__main__":
    seed = build()
    seed["expected"] = solve(seed)
    json.dump(seed, open(HERE.parents[2] / "tests/fixtures/fixed_assignment.json", "w"), indent=1, ensure_ascii=False)
    print(json.dumps(seed["expected"], indent=1))
