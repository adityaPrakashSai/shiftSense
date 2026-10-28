"""Write a seed into SQLite (design doc 4.5). Used by `scripts/seed.py` and app startup.

Parsing lives in `app/domain/seeds.py`; real_a's printed plans become `import` runs here.
Solving the baseline arrives in M3; until then only `real_a` has runs.
"""

import sqlite3

from app.domain.models import RunKind, RunStatus
from app.domain.seeds import SEED_DIR, SEED_FILES, Seed, load_seed, parse_seed
from app.planner.build_input import build_input
from app.store import repo

__all__ = ["SEED_DIR", "SEED_FILES", "Seed", "load_seed", "parse_seed", "seed_db", "write_seed"]


def clear(conn: sqlite3.Connection) -> None:
    """Remove every ward-scoped row; keeps llm_cache (demo reset keeps cached responses)."""
    tables = [
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if r["name"] not in ("llm_cache", "sqlite_sequence")
    ]
    for t in tables:
        conn.execute(f"DELETE FROM {t}")  # noqa: S608 - names come from sqlite_master
    conn.commit()


def write_seed(conn: sqlite3.Connection, seed: Seed) -> None:
    clear(conn)
    repo.save_ward(conn, seed.ward)
    repo.save_history(conn, seed.ward.id, seed.history)
    for r in seed.requests:
        repo.insert_request(conn, seed.ward.id, r)
    _write_import_runs(conn, seed)


def _write_import_runs(conn: sqlite3.Connection, seed: Seed) -> None:
    """Printed plans become `import` runs; the planned one is the parent of later versions."""
    if not seed.runs:
        return
    inp = build_input(seed.ward, seed.requests, seed.history)
    ids = {key: f"run-{seed.ward.id}-{key}" for key in seed.runs}
    for key, run in seed.runs.items():
        parent = run.get("parent")
        status = RunStatus(run["status"])
        repo.insert_run(
            conn,
            run_id=ids[key],
            ward_id=seed.ward.id,
            kind=RunKind(run.get("kind", "import")),
            status=status,
            trigger="import",
            horizon_start=seed.ward.horizon_start,
            priorities=inp.priorities,
            input_json=inp.to_json(),
            input_hash=inp.input_hash(),
            parent_run=None if parent is None else ids[parent],
            published_by="import" if status is RunStatus.PUBLISHED else None,
        )
        previous = None if parent is None else seed.runs[parent]["roster"]
        repo.insert_assignments(
            conn, ids[key], run["roster"], seed.ward.horizon_start, previous=previous
        )


def seed_db(conn: sqlite3.Connection, name: str = "default") -> Seed:
    seed = load_seed(name)
    write_seed(conn, seed)
    return seed
