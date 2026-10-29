"""Write a seed into SQLite (design doc 4.5). Used by `scripts/seed.py` and app startup.

Parsing lives in `app/domain/seeds.py`; real_a's printed plans become `import` runs here.
Wards without a printed plan get a baseline solved by CP-SAT and published at seed time. The
demo seed's solved state is committed (`data/seed/{seed}_baseline.json`), because CP-SAT with
several workers and time limits does not return the same roster twice; a pinned baseline gives
every clone, reset and deployment the same published roster, so the scripted demo's cached Muse
answers keep matching.
"""

import hashlib
import logging
import sqlite3
from pathlib import Path

from app.domain.models import RunKind, RunStatus
from app.domain.seeds import SEED_DIR, SEED_FILES, Seed, load_seed, parse_seed
from app.planner.build_input import build_input
from app.store import repo, snapshot

__all__ = ["SEED_DIR", "SEED_FILES", "Seed", "load_seed", "parse_seed", "seed_db", "write_seed"]
log = logging.getLogger(__name__)


def baseline_path(name: str) -> Path:
    return SEED_DIR / f"{name}_baseline.json"


def seed_sha(name: str) -> str:
    return hashlib.sha256((SEED_DIR / SEED_FILES[name]).read_bytes()).hexdigest()


def snapshot_tag(name: str) -> str:
    """The seed name, plus the committed baseline's hash when there is one: a deployment whose
    volume keeps an old reset snapshot takes a new one after the baseline changes."""
    path = baseline_path(name)
    if not path.exists():
        return name
    return f"{name}-{hashlib.sha256(path.read_bytes()).hexdigest()[:8]}"


def load_baseline(conn: sqlite3.Connection, name: str) -> bool:
    """Load the committed seeded state, if there is one for this seed file as it is now."""
    path = baseline_path(name)
    if name not in SEED_FILES or not path.exists():
        return False
    meta = snapshot.load_json(conn, path)
    if meta.get("seed_sha256") != seed_sha(name):
        log.warning("%s is stale (seed file changed); solving a new baseline", path.name)
        clear(conn)
        return False
    return True


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


def seed_db(conn: sqlite3.Connection, name: str = "default", *, solve: bool = True) -> Seed:
    """Write the seed; unless it brings its own published plan, solve and publish a baseline."""
    seed = load_seed(name)
    write_seed(conn, seed)
    if solve and repo.get_published_run(conn, seed.ward.id) is None:
        from app.planner.runs import publish, run_full  # planner imports store; avoid a cycle

        out = run_full(conn, seed.ward.id, trigger="seed")
        if out.result.status == "solved":
            publish(conn, out.run_id, by="seed")
    return seed
