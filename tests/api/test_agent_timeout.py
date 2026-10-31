"""Answering an expired ask is rejected, so a late tap can never double-answer a task."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import repo
from app.store.db import open_db


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        yield c


def test_respond_to_an_expired_ask_returns_409(client: TestClient, db_path: Path) -> None:
    ward_id = client.get("/api/ward").json()["id"]
    conn = open_db(db_path)
    try:
        repo.insert_nurse_message(
            conn,
            {
                "id": "m-late",
                "ward_id": ward_id,
                "nurse_id": "N07",
                "kind": "replacement",
                "task_id": "task-x",
                "day": "2026-11-03",
                "shift": "M",
                "text": "Cover?",
                "status": "asked",
            },
        )
    finally:
        conn.close()
    conn = open_db(db_path)
    try:
        repo.set_nurse_message_status(conn, "m-late", "expired")
    finally:
        conn.close()
    r = client.post("/api/inbox/m-late/respond", json={"accept": True})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ASK_EXPIRED"
