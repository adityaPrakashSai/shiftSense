import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import get_settings
from app.store.db import open_db


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("LLM_MODE", "mock")
    # short budgets keep seeding fast in tests; the timing test sets its own
    monkeypatch.setenv("SOLVER_STAGE_SECONDS", "1")
    monkeypatch.setenv("SOLVER_TOTAL_SECONDS", "3")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("EXPLAIN_DENIED", "false")  # background what-ifs; tested directly
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(db_path)
    yield c
    c.close()
