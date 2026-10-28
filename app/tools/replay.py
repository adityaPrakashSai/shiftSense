"""Replay a stored run (design doc 8.4): re-solve its input and compare tier values.

    uv run python -m app.tools.replay RUN_ID [--db data/shiftsense.db] [--seconds 5]

CP-SAT with several workers is not deterministic, so assignments may differ; tier values must
match wherever both the stored and the replayed stage are proven. Exit code 1 on a mismatch.
"""

import argparse
import sys

from app.config import get_settings
from app.solver.input import SolveInput
from app.solver.solve import SolveResult, solve
from app.store import repo
from app.store.db import open_db


def compare(stored: list[dict[str, object]], res: SolveResult) -> tuple[bool, list[str]]:
    lines = [f"{'tier':<10} {'stored':>8} {'replay':>8}  match"]
    ok = True
    again = {s.tier: s for s in res.stages}
    for row in stored:
        tier = str(row["tier"])
        new = again.get(tier)
        if new is None:
            continue
        both_proven = bool(row["proven"]) and new.proven
        same = row["best"] == new.best
        ok &= same or not both_proven
        mark = "yes" if same else ("NO" if both_proven else "n/a (best found)")
        old_flag = "" if row["proven"] else "~"
        new_flag = "" if new.proven else "~"
        lines.append(f"{tier:<10} {row['best']!s:>7}{old_flag} {new.best:>7}{new_flag}  {mark}")
    return ok, lines


def replay(run_id: str, db: str, seconds: float, workers: int) -> int:
    conn = open_db(db)
    run = repo.get_run(conn, run_id)
    if run is None:
        print(f"no run {run_id}")
        return 2
    inp = SolveInput.from_json(run["input_json"])
    res = solve(inp, stage_seconds=seconds, workers=workers)
    if res.status != "solved":
        print(f"replay {res.status} at {res.failed_tier}")
        return 1
    ok, lines = compare(repo.get_stages(conn, run_id), res)
    print(f"run {run_id} ({run['kind']}, {run['status']}); ~ = best found, not proven")
    print("\n".join(lines))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description="Re-solve a stored run and compare tier values.")
    ap.add_argument("run_id")
    ap.add_argument("--db", default=s.db_path)
    ap.add_argument("--seconds", type=float, default=s.solver_stage_seconds)
    ap.add_argument("--workers", type=int, default=s.solver_workers)
    a = ap.parse_args(argv)
    return replay(a.run_id, a.db, a.seconds, a.workers)


if __name__ == "__main__":
    sys.exit(main())
