"""Staffing check endpoint and agent tool (design doc 6.7, M14): a staffing check leaves the
published run and its assignments unchanged, bad input is refused, and the roster agent may call
it only in the conflict and leave workflows."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.tools import ALLOWLIST, REGISTRY, ToolClass
from app.main import app
from app.store.db import connect


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        yield c


def snapshot(db_path: Path) -> tuple[Any, ...]:
    conn = connect(db_path)
    runs = conn.execute("SELECT run_id, status, kind FROM run ORDER BY run_id").fetchall()
    cells = conn.execute(
        "SELECT run_id, nurse_id, day, shift FROM assignment ORDER BY run_id, nurse_id, day"
    ).fetchall()
    reqs = conn.execute("SELECT id, status FROM request ORDER BY id").fetchall()
    conn.close()
    return tuple(map(tuple, runs)), tuple(map(tuple, cells)), tuple(map(tuple, reqs))


def test_staffing_check_leaves_the_published_roster_unchanged(
    client: TestClient, db_path: Path
) -> None:
    before = snapshot(db_path)
    r = client.post("/api/staffing-check", json={"removed": ["N04"], "standby": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "solved" and body["shortfall"] == 4 and body["headcount"] == 6
    assert body["standby_shifts"] == 8 and "lower bound" in body["caveat"]
    assert {c["shift"] for c in body["short_cells"]} <= {"M", "E", "N"}
    assert "N04" not in {row["nurse_id"] for row in body["load"]}
    assert snapshot(db_path) == before


def test_hires_and_floor(client: TestClient) -> None:
    body = client.post("/api/staffing-check", json={"hires": 3, "coverage": "floor",
                                                    "keep_staff": False}).json()  # fmt: skip
    assert body["headcount"] == 5 and body["shortfall"] == 0
    assert body["inputs"]["coverage"] == "floor"


def test_bad_input_is_refused(client: TestClient) -> None:
    r = client.post("/api/staffing-check", json={"removed": ["X99"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "BAD_STAFFING_INPUT"
    r = client.post("/api/staffing-check", json={"hires": 9})
    assert r.status_code == 422


def test_agent_tool_is_compute_and_scoped() -> None:
    assert REGISTRY["staffing_check"].cls is ToolClass.COMPUTE
    allowed = {wf for wf, tools in ALLOWLIST.items() if "staffing_check" in tools}
    assert allowed == {"conflict", "leave"}  # the sick-call tool list (and its cache) is unchanged
