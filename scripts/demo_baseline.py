"""Solve a seed's baseline once and commit it as data/seed/{seed}_baseline.json.

    uv run python -m scripts.demo_baseline [--seed mini]

CP-SAT with several workers and per-stage time limits returns a different (equally ranked) roster
on each solve, so without this every clone, reset and deployment would publish its own baseline
and the cached Muse answers for the scripted demo would stop matching. With the file present, an
empty database loads it instead of solving (`app.store.seed.load_baseline`). The file records the
seed file's sha256; after editing the seed, run this again (a stale file is ignored with a warning).
"""

import argparse
import tempfile
from pathlib import Path

from app.config import get_settings
from app.store import repo, snapshot
from app.store.db import open_db
from app.store.seed import SEED_FILES, baseline_path, seed_db, seed_sha


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", default="mini", choices=sorted(SEED_FILES))
    a = ap.parse_args(argv)
    s = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "baseline.db")
        seed = seed_db(conn, a.seed)
        run = repo.get_published_run(conn, seed.ward.id)
        if run is None:
            print(f"seed {a.seed!r} did not produce a published baseline")
            return 1
        tiers = {r["tier"]: (r["best"], r["status"]) for r in repo.get_stages(conn, run["run_id"])}
        meta = {
            "seed": a.seed,
            "seed_sha256": seed_sha(a.seed),
            "run_id": run["run_id"],
            "solver": {
                "workers": s.solver_workers,
                "stage_seconds": s.solver_stage_seconds,
                "total_seconds": s.solver_total_seconds,
            },  # fmt: skip
            "tiers": tiers,
        }
        out = baseline_path(a.seed)
        snapshot.dump_json(conn, out, meta)
        conn.close()
    print(f"wrote {out.name}: {run['run_id']} {tiers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
