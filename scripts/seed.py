"""Recreate the SQLite database from a seed (design doc 4.5).

    uv run python -m scripts.seed               # default ward (SEED env var, else "default")
    SEED=mini uv run python -m scripts.seed     # 9-nurse mini ward
    SEED=real_a uv run python -m scripts.seed   # pseudonymized October ward with its printed plan

The baseline roster is not solved yet (that arrives in M3).
"""

import argparse
import os
from pathlib import Path

from app.config import get_settings
from app.store.db import open_db
from app.store.seed import SEED_FILES, seed_db


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--seed", default=os.environ.get("SEED") or "default", choices=sorted(SEED_FILES)
    )
    ap.add_argument("--db", default=get_settings().db_path)
    args = ap.parse_args()

    db = Path(args.db)
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        p.unlink(missing_ok=True)
    conn = open_db(db)
    seed = seed_db(conn, args.seed)
    standby = sum(n.standby for n in seed.ward.nurses)
    runs = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    print(
        f"seeded {db} with {args.seed!r}: ward {seed.ward.id}, {len(seed.ward.nurses)} nurses "
        f"({standby} standby), {len(seed.requests)} requests, {runs} runs"
    )
    conn.close()


if __name__ == "__main__":
    main()
