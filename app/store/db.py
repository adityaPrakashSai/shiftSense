"""SQLite connection and migration (stdlib `sqlite3`, WAL mode)."""

import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# columns added after a table first shipped: (table, column, type)
ADDED_COLUMNS = [
    ("llm_call", "reasoning", "TEXT"),
    ("llm_call", "reasoning_tokens", "INT"),
    ("message", "language", "TEXT"),
    ("message", "parse_json", "TEXT"),
    ("message", "reply", "TEXT"),
    ("message", "reply_kind", "TEXT"),
    ("message", "request_ids_json", "TEXT"),
    ("request", "counterpart_dates_json", "TEXT"),
    ("request", "counterpart_shifts_json", "TEXT"),
    ("request", "target_total", "INT"),
    ("request", "ref_request_id", "TEXT"),
    ("assignment", "cause", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in ADDED_COLUMNS:  # before the script: its indexes may use them
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
