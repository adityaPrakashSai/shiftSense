"""Sick-call statistics (design doc M13): 20 random sick calls, re-solved with and without the
stability tier.

    uv run python -m evals.sickcall_stats [--seed default] [--n 20] [--rng 2026]

On a fresh database for the seed ward (its baseline solved and published), "now" is Tue 3 Nov
06:05. Each case picks a random published cell from today to the end of the period (fixed RNG),
makes that nurse unavailable for that shift (hard, this re-solve only) and re-solves from the
published roster exactly as a confirmed sick call does (6.2: frozen past, previous roster,
stability tier), then again with the stability tier removed, same time limits. Reports changed
cells for both, solve time, and how often every tier was proven optimal. No model is called.

Writes `evals/reports/{date}-sickcall-stats.json` and `.md`.
"""

import argparse
import json
import random
import statistics
import tempfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.planner.runs import resolve_input
from app.solver.changes import change_set, count_changed
from app.solver.solve import solve
from app.store import repo
from app.store.db import open_db
from app.store.seed import seed_db

REPORTS = Path(__file__).resolve().parent / "reports"
NOW = datetime(2026, 11, 3, 6, 5, tzinfo=ZoneInfo("Asia/Kolkata"))


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def changed(inp: Any, res: Any) -> int | None:
    return count_changed(change_set(inp, res.roster)) if res.status == "solved" else None


def run(seed: str, n: int, rng_seed: int) -> dict[str, Any]:
    s = get_settings()
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "sick.db")
        seed_db(conn, seed)
        ward = repo.get_ward(conn)
        assert ward is not None
        pub = repo.get_published_run(conn, ward.id)
        assert pub is not None
        grid = repo.get_roster(conn, pub["run_id"])
        today = (NOW.date() - ward.horizon_start).days
        cells = [
            (nid, d, shift)
            for nid, days in sorted(grid.items())
            for d, shift in enumerate(days)
            if d >= today and shift in ("M", "E", "N")
        ]
        picks = random.Random(rng_seed).sample(cells, n)
        for nid, d, shift in picks:
            day = date.fromordinal(ward.horizon_start.toordinal() + d)
            _, inp, _, _, _ = resolve_input(
                conn, ward.id, affected=[day], now=NOW, extra_unavailable=[(nid, d, shift)]
            )
            no_tier = replace(inp, priorities=tuple(t for t in inp.priorities if t != "stability"))
            a, b = (
                solve(x, stage_seconds=s.solver_stage_seconds, workers=s.solver_workers,
                      total_seconds=s.solver_total_seconds)
                for x in (inp, no_tier)
            )  # fmt: skip

            row = {
                "nurse": nid, "day": day.isoformat(), "shift": shift,
                "status": a.status,
                "changed_with": changed(inp, a), "changed_without": changed(inp, b),
                "secs_with": round(a.secs, 2), "secs_without": round(b.secs, 2),
                "proven": a.status == "solved" and a.proven,
            }  # fmt: skip
            rows.append(row)
            print(f"{nid} {day} {shift}: {row['status']:10} changed {row['changed_with']} vs "
                  f"{row['changed_without']} without stability, {a.secs:.2f} s, "
                  f"proven {row['proven']}")  # fmt: skip
        conn.close()
    ok = [r for r in rows if r["status"] == "solved" and r["changed_without"] is not None]
    secs = [r["secs_with"] for r in rows]
    summary = {
        "ward": seed,
        "sick_calls": len(rows),
        "solved": f"{sum(r['status'] == 'solved' for r in rows)}/{len(rows)}",
        "changed_cells_with_stability_median": statistics.median(r["changed_with"] for r in ok),
        "changed_cells_with_stability_max": max(r["changed_with"] for r in ok),
        "changed_cells_without_stability_median": statistics.median(
            r["changed_without"] for r in ok
        ),
        "changed_cells_without_stability_max": max(r["changed_without"] for r in ok),
        "solve_secs_p50": round(pct(secs, 0.5), 2),
        "solve_secs_p95": round(pct(secs, 0.95), 2),
        "solve_secs_max": max(secs),
        "proven_rate": f"{100 * sum(r['proven'] for r in rows) / len(rows):.0f}%",
        "solver": {
            "workers": s.solver_workers,
            "stage_seconds": s.solver_stage_seconds,
            "total_seconds": s.solver_total_seconds,
        },  # fmt: skip
        "rng_seed": rng_seed,
    }
    return {"summary": summary, "cases": rows}


def markdown(out: dict[str, Any]) -> str:
    s = out["summary"]
    lines = [
        f"# Sick-call statistics · {s['ward']} ward · {date.today().isoformat()}",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in s.items()],
        "",
        "| Nurse | Day | Shift | Changed (stability) | Changed (no stability) | Secs | Proven |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        *[
            f"| {r['nurse']} | {r['day']} | {r['shift']} | {r['changed_with']} | "
            f"{r['changed_without']} | {r['secs_with']} | {'yes' if r['proven'] else 'no'} |"
            for r in out["cases"]
        ],
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", default="default", choices=["default", "mini", "real_a"])
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--rng", type=int, default=2026)
    a = ap.parse_args(argv)
    out = run(a.seed, a.n, a.rng)
    print(json.dumps(out["summary"], indent=1))
    REPORTS.mkdir(exist_ok=True)
    stem = REPORTS / f"{date.today().isoformat()}-sickcall-stats"
    stem.with_suffix(".json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(out), encoding="utf-8")
    print(f"report: {stem}.json / .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
