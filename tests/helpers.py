"""Shared test setup: the mini ward with the reference baseline published (with full evidence),
and the scenario-A sick call ranked on top of it."""

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.models import Request, RequestKind, RequestStatus
from app.planner.runs import ResolvePlan, evaluate_roster, start_resolve
from app.planner.sickcall import rank
from app.store import repo
from app.store.seed import seed_db

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = json.loads((ROOT / "tests/fixtures/mini_expected.json").read_text())
NOW = datetime(2026, 11, 3, 6, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
TUE = date(2026, 11, 3)


def mini_baseline(conn: sqlite3.Connection) -> str:
    seed_db(conn, "mini", solve=False)
    return evaluate_roster(conn, EXPECTED["baseline"]["roster"], trigger="fixture")


def scenario_a(conn: sqlite3.Connection) -> ResolvePlan:
    """Meena (N03) sick for Tue 3 Nov Morning at 06:05, ranked (S01 first)."""
    mini_baseline(conn)
    r = Request(id="R-0100", nurse_id="N03", kind=RequestKind.UNAVAILABLE, dates=[TUE],
                shifts=["M"], hard=True, status=RequestStatus.ACTIVE, created_at=NOW)  # fmt: skip
    repo.insert_request(conn, "ward-mini", r)
    plan = start_resolve(conn, trigger="request:R-0100", affected=[TUE], request_id="R-0100",
                         now=NOW)  # fmt: skip
    rank(conn, plan)
    return plan
