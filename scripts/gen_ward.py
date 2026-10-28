"""Generates data/seed/ward.json, the default synthetic seed ward (design doc 4.1, 4.5).

Run once: `uv run python scripts/gen_ward.py`. The JSON is the source of truth; this script is
deterministic so the file can be regenerated after a change here. All names are invented.
"""

import datetime as dt
import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "seed" / "ward.json"
START = dt.date(2026, 10, 26)  # Monday
DAYS = 14
T0 = "2026-10-20T10:00:00Z"


def iso(i: int) -> str:
    return (START + dt.timedelta(i)).isoformat()


SHIFTS = [
    {"code": "M", "name": "Morning", "start": "07:00", "end": "15:00", "hours": 8.0},
    {"code": "E", "name": "Evening", "start": "15:00", "end": "23:00", "hours": 8.0},
    {"code": "N", "name": "Night", "start": "23:00", "end": "07:00", "hours": 8.0},
]

# id, name, Devanagari first name, grade, language, extra aliases
REGULAR = [
    ("N01", "Anjali Verma", "अंजलि", "senior", "hi", []),
    ("N02", "Pooja Iyer", "पूजा", "senior", "en", []),
    ("N03", "Neha Gupta", "नेहा", "senior", "hi", []),
    ("N04", "Kavya Reddy", "काव्या", "staff", "en", []),
    ("N05", "Priya Nair", "प्रिया", "staff", "en", []),
    ("N06", "Fatima Khan", "फ़ातिमा", "staff", "hi", []),
    ("N07", "Meena Sharma", "मीना", "staff", "hi", ["Meena di"]),
    ("N08", "Ritu Singh", "रितु", "staff", "hi", []),
    ("N09", "Deepa Joshi", "दीपा", "staff", "hi", []),
    ("N10", "Asha Kurian", "आशा", "senior", "en", []),
    ("N11", "Shalini Bose", "शालिनी", "staff", "en", []),
    ("N12", "Divya Rawat", "दिव्या", "staff", "hi", []),
    ("N13", "Geeta Yadav", "गीता", "staff", "hi", []),
    ("N14", "Mercy Joseph", "मर्सी", "staff", "en", []),
    ("N15", "Swati Kulkarni", "स्वाति", "staff", "hi", []),
    ("N16", "Lata Pillai", "लता", "staff", "hi", []),
    ("N17", "Rekha Menon", "रेखा", "staff", "hi", []),
    ("N18", "Sneha Patil", "स्नेहा", "staff", "hi", []),
    ("N19", "Nisha Thakur", "निशा", "trainee", "hi", []),
    ("N20", "Bindu Thomas", "बिंदु", "trainee", "en", []),
]
STANDBY = [
    ("S01", "Arun Kumar", "अरुण", "staff", "hi", ["Arun bhaiya"], True),
    ("S02", "Kiran Das", "किरण", "staff", "hi", [], True),
    ("S03", "Rahul Mishra", "राहुल", "staff", "hi", [], True),
    # a trainee who is not night-qualified: the counterfactual test case (11.3)
    ("S04", "Joseph Mathew", "जोसेफ", "trainee", "en", [], False),
]


def nurse(nid, name, dev, grade, lang, extra, *, standby=False, can_night=True):
    first = name.split()[0]
    return {
        "id": nid,
        "name": name,
        "aliases": [first, dev, *extra],
        "grade": grade,
        "role": "senior" if grade == "senior" else "staff",
        "standby": standby,
        "can_night": can_night,
        "language": lang,
        "fixed_duty": {},
        # 4.1: 5.5-6 shifts a week for regular nurses; standby at most 4 per 14 days
        "min_shifts_per_week": 0.0 if standby else 5.5,
        "max_shifts_per_week": 2.0 if standby else 6.0,
    }


nurses = [nurse(*r) for r in REGULAR] + [
    nurse(nid, name, dev, grade, lang, extra, standby=True, can_night=cn)
    for nid, name, dev, grade, lang, extra, cn in STANDBY
]

# 4.1 demand: target weekday M 7 (Mon/Tue 8), E 5, N 4; weekend M 6, E 5, N 4; floor M 5, E 4, N 3
TARGET = {"weekday": {"M": 7, "E": 5, "N": 4}, "weekend": {"M": 6, "E": 5, "N": 4}}
FLOOR = {"M": 5, "E": 4, "N": 3}
demand = []
for d in range(DAYS):
    wd = (START + dt.timedelta(d)).weekday()
    kind = "weekend" if wd >= 5 else "weekday"
    for s in "MEN":
        extra = 1 if (wd in (0, 1) and s == "M") else 0
        demand.append(
            {
                "day": iso(d),
                "shift": s,
                "min_total": FLOOR[s],
                "target_total": TARGET[kind][s] + extra,
                "max_total": None,
                "min_senior": 0,
            }
        )


def req(i, nurse_id, kind, days, *, shifts=None, weight=1, leave_type=None):
    hard = kind in ("unavailable", "leave")
    return {
        "id": f"R-{i:04d}",
        "nurse_id": nurse_id,
        "kind": kind,
        "dates": [iso(d) for d in days],
        "shifts": shifts,
        "leave_type": leave_type,
        "counterpart_id": None,
        "hard": hard,
        "weight": 1 if hard else weight,
        "status": "active",
        "source_message_id": None,
        "created_at": T0,
    }


# 12 requests (4.5): 3 leave, 6 day-off preferences, 3 shift preferences
requests = [
    req(1, "N04", "leave", [2, 3], leave_type="CL"),  # Wed-Thu 28-29 Oct
    req(2, "N13", "leave", [7, 8, 9], leave_type="EL"),  # Mon-Wed 2-4 Nov
    req(3, "N16", "leave", [11], leave_type="RH"),  # Fri 6 Nov
    req(4, "N01", "day_off", [5], weight=2),  # Sat 31 Oct
    req(5, "N08", "day_off", [5], weight=2),  # Sat 31 Oct
    req(6, "N11", "day_off", [6]),  # Sun 1 Nov
    req(7, "N15", "day_off", [4]),  # Fri 30 Oct
    req(8, "N18", "day_off", [10]),  # Thu 5 Nov
    req(9, "N05", "day_off", [12, 13]),  # second weekend
    req(10, "N02", "shift_wanted", [7, 8, 9, 10, 11], shifts=["M"]),  # mornings in week 2
    req(11, "N12", "shift_wanted", [5, 6], shifts=["N"], weight=2),  # first weekend nights
    # night break announced in advance (5.3): avoid_shift N, weight 5
    req(12, "N09", "shift_avoid", list(range(7, 14)), shifts=["N"], weight=5),
]

# Carry-in from two prior 14-day periods (4.3, 4.5). (last_days for days -3..-1, consecutive
# working days, consecutive nights). Every pattern is legal: no M or E right after N.
PATTERNS = [
    (["-", "M", "E"], 2, 0),
    (["E", "-", "-"], 0, 0),
    (["M", "M", "-"], 0, 0),
    (["-", "-", "M"], 1, 0),
    (["E", "E", "E"], 4, 0),
    (["N", "N", "-"], 0, 0),
    (["-", "N", "N"], 2, 2),
    (["M", "E", "-"], 0, 0),
    (["-", "E", "E"], 2, 0),
    (["M", "M", "M"], 5, 0),
]
SPECIAL = {
    "N02": (["M", "E", "E"], 6, 0),  # finished last period with 6 consecutive days
    "N08": (["N", "N", "N"], 3, 3),  # finished last period with 3 consecutive nights
}
rng = random.Random(2026)
history = {}
for i, (nid, *_rest) in enumerate(REGULAR):
    last, cd, cn = SPECIAL.get(nid, PATTERNS[i % len(PATTERNS)])
    nights = rng.choice([4, 5, 5, 6, 6, 6, 7])
    total = rng.choice([22, 23, 23, 24])
    mornings = round((total - nights) * rng.uniform(0.52, 0.64))
    history[nid] = {
        "consecutive_days": cd,
        "consecutive_nights": cn,
        "last_days": last,
        "nights": nights,
        "mornings": mornings,
        "evenings": total - nights - mornings,
        "weekends": rng.choice([4, 5, 6, 6, 7, 8]),
        "denied": rng.choice([0, 0, 0, 1, 1, 2]),
    }
for nid, *_rest in STANDBY:
    m, e = rng.choice([0, 1, 2]), rng.choice([0, 1])
    history[nid] = {
        "consecutive_days": 0,
        "consecutive_nights": 0,
        "last_days": ["-", "-", "-"],
        "nights": 0,
        "mornings": m,
        "evenings": e,
        "weekends": rng.choice([0, 1]),
        "denied": 0,
    }

seed = {
    "schema": "shiftsense.ward_seed/v1",
    "about": "Default synthetic demo ward (design doc 4.1, 4.5): 20 regular + 4 standby nurses, "
    "14 days from Mon 26 Oct 2026. Invented names only. Generated by scripts/gen_ward.py.",
    "ward": {
        "id": "ward-7",
        "name": "Ward 7 (General Medicine)",
        "tz": "Asia/Kolkata",
        "roster_style": "grid",
        "horizon_preset": "fortnight",
        "horizon_start": START.isoformat(),
        "horizon_days": DAYS,
        "shifts": SHIFTS,
        "nurses": nurses,
        "demand": demand,
        "max_consecutive_days": 6,
        "night_block_min": 2,
        "night_block_max": 3,
        "max_nights_per_month": 6,
    },
    "requests": requests,
    "history": {"periods": 2, "days_covered": 28, "nurses": history},
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(seed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
