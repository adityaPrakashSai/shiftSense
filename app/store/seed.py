"""Load a seed file into SQLite (design doc 4.5). Used by `scripts/seed.py` and app startup.

Seeds: `default` (data/seed/ward.json, 20 + 4 standby), `mini` (9-nurse mini ward) and `real_a`
(pseudonymized October ward with its printed plan and hand-edited update as import runs).
Solving the baseline arrives in M3; until then only `real_a` has runs.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.models import Request, RunKind, RunStatus, Ward, WardHistory
from app.planner.build_input import build_input
from app.store import repo

SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "seed"
SEED_FILES = {
    "default": "ward.json",
    "mini": "mini_ward.json",
    "real_a": "real_ward_a.json",
}


@dataclass
class Seed:
    name: str
    ward: Ward
    requests: list[Request]
    history: WardHistory
    runs: dict[str, Any]  # raw "runs" block (real_a only)


def _off(code: str | None) -> str | None:
    return None if code in (None, "-") else code


def parse_seed(raw: dict[str, Any], name: str) -> Seed:
    w = dict(raw["ward"])
    if "max_consecutive_nights" in w:  # mini_ward/v1 spelling of night_block_max
        w.setdefault("night_block_max", w.pop("max_consecutive_nights"))
    ward = Ward.model_validate(w)

    default_created = datetime.fromisoformat(f"{ward.horizon_start.isoformat()}T00:00:00+00:00")
    requests = []
    for r in raw.get("requests", []):
        requests.append(Request.model_validate({"created_at": default_created, **r}))

    h = raw.get("history", {})
    history = WardHistory.model_validate(
        {
            "periods": h.get("periods", 0),
            "nurses": {
                nid: {**rec, "last_days": [_off(c) for c in rec.get("last_days", [])]}
                for nid, rec in h.get("nurses", {}).items()
            },
        }
    )
    return Seed(name, ward, requests, history, raw.get("runs", {}))


def load_seed(name: str = "default") -> Seed:
    if name not in SEED_FILES:
        raise ValueError(f"unknown seed {name!r}; choose one of {sorted(SEED_FILES)}")
    raw = json.loads((SEED_DIR / SEED_FILES[name]).read_text(encoding="utf-8"))
    return parse_seed(raw, name)


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
