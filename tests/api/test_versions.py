"""Planned vs current versions (design doc 6.5c) on real_a: the printed plan, the hand edit that
left nights 13-14 Oct at 2, and the app's proposal for the same leave extension side by side."""

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.domain.models import Request, RequestKind, RequestStatus
from app.main import app
from app.planner.runs import start_resolve
from app.planner.sickcall import rank
from app.store import repo
from app.store.db import open_db

NOW = datetime(2026, 10, 10, 6, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
EXTENSION = [date(2026, 10, d) for d in (10, 11, 12, 13)]


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "real_a")
    monkeypatch.setenv("DEMO_NOW", NOW.isoformat())
    with TestClient(app) as c:
        yield c


def test_leave_extension_proposal_beside_the_hand_edit(client: TestClient, db_path: Path) -> None:
    v = client.get("/api/versions").json()
    assert v["planned"]["run_id"] == "run-ward-real-a-planned"
    hand = v["versions"][0]
    assert hand["label"] == "current" and hand["run"]["run_id"] == "run-ward-real-a-updated"
    short = [d for d, (n, t) in enumerate(zip(hand["night_totals"], hand["night_targets"],
                                              strict=True)) if n < t]  # fmt: skip
    assert short == [12, 13]  # the hand edit left nights 13-14 Oct at 2 against 3
    assert {c["cause"] for c in hand["changes"]} == {"hand_edit"}

    # the charge nurse enters the same leave extension; the re-solve keeps every night at 3
    conn = open_db(db_path)
    try:
        r = Request(id="R-0900", nurse_id="N05", kind=RequestKind.LEAVE, dates=EXTENSION,
                    shifts=None, leave_type="EL", hard=True, status=RequestStatus.ACTIVE,
                    created_at=NOW)  # fmt: skip
        repo.insert_request(conn, "ward-real-a", r)
        plan = start_resolve(conn, trigger="request:R-0900", affected=EXTENSION,
                             request_id="R-0900", now=NOW)  # fmt: skip
        out = rank(conn, plan)
    finally:
        conn.close()
    assert out.status.value == "proposed"

    v = client.get("/api/versions").json()
    labels = [x["label"] for x in v["versions"]]
    assert labels == ["current", "proposal"]
    prop = v["versions"][1]
    assert prop["run"]["run_id"] == plan.run_id
    assert all(n >= t for n, t in zip(prop["night_totals"], prop["night_targets"], strict=True))
    assert prop["night_totals"][12] == prop["night_totals"][13] == 3
    by_cell = {(c["nurse_id"], c["day"]): c for c in prop["changes"]}
    assert any(c["run_id"] == plan.run_id and c["request_id"] == "R-0900" for c in by_cell.values())
