"""Generates mini_ward.json (the committed fixture). Run once; the JSON is the source of truth.
The 27 Oct 2026 changes (NIGHT_RECOVERY removed, SINGLE_NIGHT weight 3, FAIR_MORNINGS with
history mornings, night block keys, scenario D) were made in the JSON directly, not here."""
import datetime as dt
import json
import sys

START = dt.date(2026, 10, 26)
DAYS = 14
iso = lambda i: (START + dt.timedelta(i)).isoformat()

shifts = [
    {"code": "M", "name": "Morning", "start": "07:00", "end": "15:00"},
    {"code": "E", "name": "Evening", "start": "15:00", "end": "23:00"},
    {"code": "N", "name": "Night", "start": "23:00", "end": "07:00"},
]

def nurse(id_, name, aliases, *, can_night=True, standby=False, language="hi"):
    return {
        "id": id_, "name": name, "aliases": aliases, "grade": "staff",
        "can_night": can_night, "standby": standby, "language": language,
        "min_shifts_per_week": 0.0 if standby else 5.5,
        "max_shifts_per_week": 2.0 if standby else 6.0,
    }

nurses = [
    nurse("N01", "Anjali Verma", ["Anjali", "अंजलि", "Anjali di"]),
    nurse("N02", "Priya Nair", ["Priya", "प्रिया"], language="en"),
    nurse("N03", "Meena Sharma", ["Meena", "मीना", "Meena di"]),
    nurse("N04", "Kavya Reddy", ["Kavya", "काव्या"], language="en"),
    nurse("N05", "Ritu Singh", ["Ritu", "रितु"]),
    nurse("N06", "Fatima Khan", ["Fatima", "फ़ातिमा"]),
    nurse("N07", "Deepa Joshi", ["Deepa", "दीपा"], can_night=False),
    nurse("S01", "Arun Kumar", ["Arun", "अरुण", "Arun bhaiya"], standby=True),
    nurse("S02", "Rekha Das", ["Rekha", "रेखा"], standby=True),
]

TARGET = {"weekday": {"M": 3, "E": 2, "N": 1}, "weekend": {"M": 2, "E": 2, "N": 1}}
FLOOR = {"weekday": {"M": 2, "E": 1, "N": 1}, "weekend": {"M": 1, "E": 1, "N": 1}}
MON_TUE_MORNING_EXTRA = 1      # interview 27 Oct: Monday and Tuesday mornings are busier
demand = []
for d in range(DAYS):
    wd = (START + dt.timedelta(d)).weekday()
    kind = "weekend" if wd >= 5 else "weekday"
    for s in "MEN":
        extra = MON_TUE_MORNING_EXTRA if (wd in (0, 1) and s == "M") else 0
        demand.append({"day": iso(d), "shift": s, "min_total": FLOOR[kind][s],
                       "target_total": TARGET[kind][s] + extra, "min_senior": 0})

T0 = "2026-10-20T10:00:00Z"
def req(i, nurse_id, kind, days, shifts=None, weight=1, note=""):
    hard = kind in ("unavailable", "leave")
    return {"id": f"R-{i:04d}", "nurse_id": nurse_id, "kind": kind,
            "dates": [iso(d) for d in days], "shifts": shifts, "counterpart_id": None,
            "hard": hard, "weight": 1 if hard else weight, "status": "active",
            "source_message_id": None, "created_at": T0, "note": note}

requests = [
    req(1, "N04", "leave", [2, 3], note="Approved leave: cousin's wedding (Wed–Thu)."),
    req(2, "N01", "day_off", [5], weight=2, note="Wants Sat 31 Oct off."),
    req(3, "N03", "day_off", [5], weight=2, note="Wants Sat 31 Oct off."),
    req(4, "N07", "day_off", [5], weight=2, note="Wants Sat 31 Oct off. Only 4 of the 5 Saturday slots can be filled if all three are granted."),
    req(5, "N06", "shift_avoid", list(range(DAYS)), ["N"], weight=5,
        note="Night break announced in advance: 'agle do hafte night duty nahi'."),
    req(6, "N02", "shift_wanted", [5, 6, 7], ["N"], weight=3,
        note="Wants Sat–Mon nights so she is free during the day for a family event."),
    req(7, "N03", "shift_wanted", [7, 8, 9, 10, 11], ["M"], weight=1,
        note="Prefers mornings in week 2 (child's exams)."),
    req(8, "N05", "day_off", [12, 13], weight=1, note="Wants the second weekend off."),
    req(9, "S02", "unavailable", [8], note="Standby nurse has an exam on Tue 3 Nov."),
]

# Carry-in from two published 14-day periods (28 days). last_days = codes for day -3, -2, -1 ("-" = off).
history = {
    "periods": 2, "days_covered": 28,
    "nurses": {
        "N01": {"consecutive_days": 2, "consecutive_nights": 0, "last_days": ["-", "M", "E"],
                "nights": 10, "evenings": 6, "weekends": 6, "denied": 0},
        "N02": {"consecutive_days": 6, "consecutive_nights": 0, "last_days": ["M", "E", "E"],
                "nights": 6, "evenings": 8, "weekends": 5, "denied": 0},
        "N03": {"consecutive_days": 0, "consecutive_nights": 0, "last_days": ["E", "-", "-"],
                "nights": 5, "evenings": 9, "weekends": 5, "denied": 2},
        "N04": {"consecutive_days": 4, "consecutive_nights": 0, "last_days": ["M", "M", "M"],
                "nights": 4, "evenings": 7, "weekends": 4, "denied": 1},
        "N05": {"consecutive_days": 3, "consecutive_nights": 3, "last_days": ["N", "N", "N"],
                "nights": 9, "evenings": 5, "weekends": 6, "denied": 0},
        "N06": {"consecutive_days": 1, "consecutive_nights": 0, "last_days": ["-", "-", "M"],
                "nights": 3, "evenings": 8, "weekends": 5, "denied": 1},
        "N07": {"consecutive_days": 0, "consecutive_nights": 0, "last_days": ["M", "E", "-"],
                "nights": 0, "evenings": 10, "weekends": 3, "denied": 1},
        "S01": {"consecutive_days": 0, "consecutive_nights": 0, "last_days": ["-", "-", "-"],
                "nights": 0, "evenings": 0, "weekends": 0, "denied": 0},
        "S02": {"consecutive_days": 0, "consecutive_nights": 0, "last_days": ["-", "-", "-"],
                "nights": 0, "evenings": 0, "weekends": 0, "denied": 0},
    },
}

rules = {
    "hard": {
        "max_consecutive_days": 6,
        "max_consecutive_nights": 3,
        "rest_after_night": "no M or E the day after N",
        "max_cover_is_target": True,
        "standby_off_roster_in_full_run": True,
        "leave_counts_toward_contract_min": True,
        "max_nights_per_month": 6,
        "max_nights_scaling": "ceil(6 * horizon_days / 30): 3 per 14 days",
    },
    "priorities": ["coverage", "stability", "contract", "requests", "fairness"],
    "soft": [
        {"rule": "COVER_SHORT", "tier": "coverage", "weight": 1, "core": True},
        {"rule": "CHANGED", "tier": "stability", "weight": 2, "core": True},
        {"rule": "STANDBY_CALLED", "tier": "stability", "weight": 1, "core": True},
        {"rule": "CONTRACT_MIN", "tier": "contract", "weight": 1, "core": True},
        {"rule": "QUICK_RETURN", "tier": "requests", "weight": 3, "core": False,
         "what": "E then M next day (15–23 then 07–15: 8 h rest)"},
        {"rule": "NIGHT_RECOVERY", "tier": "requests", "weight": 2, "core": False,
         "what": "after a night, fewer than 2 days off before the next day shift (N, off, M/E)"},
        {"rule": "SINGLE_NIGHT", "tier": "requests", "weight": 1, "core": False,
         "what": "a night block of length 1 (min consecutive nights = 2)"},
        {"rule": "ISOLATED_DAY", "tier": "requests", "weight": 1, "core": False,
         "what": "off, work, off (min consecutive working days = 2)"},
        {"rule": "SPLIT_WEEKEND", "tier": "requests", "weight": 1, "core": False,
         "what": "works exactly one of Sat/Sun (complete weekends)"},
        {"rule": "PREF_OFF", "tier": "requests", "weight": "2 x request.weight", "core": True},
        {"rule": "PREF_SHIFT", "tier": "requests", "weight": "request.weight", "core": True},
        {"rule": "PREF_AVOID", "tier": "requests", "weight": "request.weight", "core": True},
        {"rule": "FAIR_NIGHTS", "tier": "fairness", "weight": 2, "core": True},
        {"rule": "FAIR_WEEKENDS", "tier": "fairness", "weight": 2, "core": True},
        {"rule": "FAIR_EVENINGS", "tier": "fairness", "weight": 1, "core": True},
        {"rule": "FAIR_DENIALS", "tier": "fairness", "weight": 1, "core": True},
    ],
}

scenarios = [
    {"id": "A-sick-morning", "title": "Meena calls in sick for today's Morning (the demo story)",
     "now": "2026-11-03T06:05:00+05:30",
     "message": "Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai",
     "edit": {"op": "mark_unavailable", "nurse": "N03", "dates": [iso(8)], "shifts": ["M"],
              "reason_category": "sick"},
     "frozen_before_day": 8},
    {"id": "B-sick-three-days", "title": "Meena is out for three days (Tue–Thu, all shifts)",
     "now": "2026-11-03T06:05:00+05:30",
     "message": "Doctor ne 3 din rest bola hai",
     "edit": {"op": "mark_unavailable", "nurse": "N03", "dates": [iso(8), iso(9), iso(10)],
              "shifts": None, "reason_category": "sick"},
     "frozen_before_day": 8},
    {"id": "C-demand-up", "title": "Thursday evening needs 3 nurses instead of 2 (charge nurse change)",
     "now": "2026-11-03T06:05:00+05:30",
     "message": "Thursday shaam ko ICU se 2 patient aa rahe hain, 3 nurse chahiye",
     "edit": {"op": "change_demand", "nurse": "self", "dates": [iso(10)], "shifts": ["E"],
              "target_total": 3},
     "frozen_before_day": 8},
]

instance = {
    "schema": "shiftsense.mini_ward/v1",
    "about": "Small, hand-checkable instance with the hard parts of INRC-I/INRC-II/Curtois/NSPLib except scale. See README.md.",
    "ward": {"id": "ward-mini", "name": "Mini ward (7 + 2 standby)", "tz": "Asia/Kolkata",
             "horizon_start": iso(0), "horizon_days": DAYS, "shifts": shifts,
             "nurses": nurses, "demand": demand,
             "max_consecutive_days": 6, "max_consecutive_nights": 3, "max_nights_per_month": 6},
    "requests": requests,
    "history": history,
    "rules": rules,
    "scenarios": scenarios,
}
json.dump(instance, open(sys.argv[1], "w"), indent=1, ensure_ascii=False)
