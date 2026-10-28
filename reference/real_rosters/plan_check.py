"""Runs the design doc's hard rules (5.2) over the printed October plan and prints what a
hand-made roster breaks. Run: python3 plan_check.py  (the M2 write-up number; not a test).
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
SEED = json.load(open(HERE.parents[1] / "data/seed/real_ward_a.json"))
W = SEED["ward"]
DAYS = W["horizon_days"]


def check(roster, hist):
    out = []
    for n, row in roster.items():
        h = hist.get(n, {})
        last = [None if x == "-" else x for x in h.get("last_days", ["-"] * 3)]
        seq = last + row                      # index 3 = day 0
        run = h.get("consecutive_days", 0)
        nrun = h.get("consecutive_nights", 0)
        for d, s in enumerate(row):
            prev = seq[d + 2]
            if prev == "N" and s in ("M", "E"):
                out.append(f"rest_after_night:{n}:d{d + 1}")
            if prev == "E" and s == "M":
                out.append(f"QUICK_RETURN (soft):{n}:d{d + 1}")
            run = run + 1 if s else 0
            nrun = nrun + 1 if s == "N" else 0
            if run == W["max_consecutive_days"] + 1:
                out.append(f"max_consecutive_days:{n}:d{d + 1}")
            if nrun == W["night_block_max"] + 1:
                out.append(f"max_consecutive_nights:{n}:d{d + 1}")
        if sum(s == "N" for s in row) > W["max_nights_per_month"]:
            out.append(f"max_nights:{n}")
    return out


if __name__ == "__main__":
    hist = SEED["history"]["nurses"]
    res = {k: check(SEED["runs"][k]["roster"], hist) for k in ("planned", "updated")}
    for k, v in res.items():
        hard = [x for x in v if "(soft)" not in x]
        print(f"{k}: {len(hard)} hard-rule breaks, {len(v) - len(hard)} quick returns")
        for x in hard:
            print("  ", x)
    json.dump(res, open(HERE / "plan_check_results.json", "w"), indent=1)
