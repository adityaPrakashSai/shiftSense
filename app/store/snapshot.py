"""Fast demo reset (US-9): copy the seeded database to a snapshot once, restore it in < 1 s.

Every table is restored except `llm_cache`, which a reset keeps (design doc 10.1). `dump_json` /
`load_json` hold the same tables as text, for a seeded state committed to the repo.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

KEEP = {"llm_cache", "sqlite_sequence"}


def snapshot_path(db_path: str | Path, seed: str) -> Path:
    p = Path(db_path)
    return p.with_name(f"{p.stem}.snapshot-{seed}.db")


def save(conn: sqlite3.Connection, path: Path) -> None:
    path.unlink(missing_ok=True)
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()


def restore(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("ATTACH DATABASE ? AS snap", (str(path),))
    try:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM snap.sqlite_master WHERE type = 'table'")
            if r[0] not in KEEP
        ]
        newer = [
            r[0]
            for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")
            if r[0] not in KEEP and r[0] not in tables
        ]
        with conn:
            for t in newer:  # tables added after the snapshot was taken start empty
                conn.execute(f"DELETE FROM main.{t}")  # noqa: S608 - names from sqlite_master
            for t in tables:
                # by column name, so a snapshot taken before a column was added still restores
                main_cols = {r[1] for r in conn.execute(f"PRAGMA main.table_info({t})")}
                cols = [
                    r[1] for r in conn.execute(f"PRAGMA snap.table_info({t})") if r[1] in main_cols
                ]
                names = ", ".join(cols)
                conn.execute(f"DELETE FROM main.{t}")  # noqa: S608 - names from sqlite_master
                conn.execute(f"INSERT INTO main.{t}({names}) SELECT {names} FROM snap.{t}")  # noqa: S608
    finally:
        conn.execute("DETACH DATABASE snap")


def _tables(conn: sqlite3.Connection, schema: str = "main") -> list[str]:
    rows = conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")  # noqa: S608
    return [r[0] for r in rows if r[0] not in KEEP]


def dump_json(conn: sqlite3.Connection, path: Path, meta: dict[str, Any]) -> None:
    """Every non-empty table except the cache, as {columns, rows}, plus `meta`."""
    tables: dict[str, Any] = {}
    for t in sorted(_tables(conn)):
        cur = conn.execute(f"SELECT * FROM {t} ORDER BY rowid")  # noqa: S608 - from sqlite_master
        rows = [list(r) for r in cur.fetchall()]
        if rows:
            tables[t] = {"columns": [d[0] for d in cur.description], "rows": rows}
    doc = {"meta": meta, "tables": tables}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_json(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    """Replace every table (except the cache) with the dump's rows; returns its `meta`."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    with conn:
        for t in _tables(conn):
            conn.execute(f"DELETE FROM {t}")  # noqa: S608 - names from sqlite_master
            data = doc["tables"].get(t)
            if not data:
                continue
            main_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            keep = [i for i, c in enumerate(data["columns"]) if c in main_cols]
            names = ", ".join(data["columns"][i] for i in keep)
            marks = ", ".join("?" for _ in keep)
            conn.executemany(f"INSERT INTO {t}({names}) VALUES({marks})",  # noqa: S608
                             [[row[i] for i in keep] for row in data["rows"]])  # fmt: skip
    return dict(doc["meta"])
