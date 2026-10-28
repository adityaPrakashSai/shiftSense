"""Shared FastAPI dependencies."""

import sqlite3
from collections.abc import Iterator

from app.config import get_settings
from app.store.db import connect


def get_conn() -> Iterator[sqlite3.Connection]:
    """One SQLite connection per request (cheap; WAL lets readers run beside the solver)."""
    conn = connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()
