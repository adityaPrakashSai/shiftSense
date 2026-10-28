"""Shared FastAPI dependencies and helpers."""

import sqlite3
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends

from app.api.errors import ApiError
from app.config import get_settings
from app.domain.models import Ward
from app.store import repo
from app.store.db import connect


def get_conn() -> Iterator[sqlite3.Connection]:
    """One SQLite connection per request (cheap; WAL lets readers run beside the solver)."""
    conn = connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def require_ward(conn: sqlite3.Connection) -> Ward:
    ward = repo.get_ward(conn)
    if ward is None:
        raise ApiError(404, "WARD_NOT_FOUND", "No ward in the database; run `make seed`.")
    return ward


def require_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = repo.get_run(conn, run_id)
    if run is None:
        raise ApiError(404, "RUN_NOT_FOUND", f"No run {run_id}.")
    return run
