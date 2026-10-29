"""Fast demo reset (US-9): copy the seeded database to a snapshot once, restore it in < 1 s.

Every table is restored except `llm_cache`, which a reset keeps (design doc 10.1).
"""

import sqlite3
from pathlib import Path

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
