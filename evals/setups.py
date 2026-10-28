"""Scenario setups for the explain suite (design doc 9.3): each case starts from the mini ward
with the reference baseline published (deterministic), denied-request reasons filled, and
optionally a sick call (scenario A) or a demand change (scenario C) ranked on top.

Run ids are made deterministic per case, so the questions the model sees, and so the response
cache keys, repeat exactly between runs.
"""

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.domain.models import Request, RequestKind, RequestStatus
from app.evidence.counterfactual import explain_denied
from app.planner import requests as lifecycle
from app.planner import runs, sickcall
from app.store import repo
from app.store.seed import seed_db

ROOT = Path(__file__).resolve().parent.parent
BASELINE = json.loads((ROOT / "tests/fixtures/mini_expected.json").read_text())["baseline"]


class RunIds:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"run-eval-{self.n:04d}"


def prepare(conn: sqlite3.Connection, setup: str, now: datetime) -> str:
    """Build the case's world; returns the run the question is about. Synchronous on purpose:
    the run-id counter is patched for the duration and nothing else interleaves."""
    ids = RunIds()
    saved = (runs.new_run_id, sickcall.__dict__["new_run_id"])
    runs.__dict__["new_run_id"] = sickcall.__dict__["new_run_id"] = ids
    try:
        seed_db(conn, "mini", solve=False)
        base = runs.evaluate_roster(conn, BASELINE["roster"], trigger="fixture")
        explain_denied(conn, base)
        if setup == "baseline":
            return base
        ward = repo.get_ward(conn)
        assert ward is not None
        if setup == "A":
            tue = date(2026, 11, 3)
            repo.insert_request(conn, ward.id, Request(
                id="R-0100", nurse_id="N03", kind=RequestKind.UNAVAILABLE, dates=[tue],
                shifts=["M"], hard=True, status=RequestStatus.ACTIVE, created_at=now))  # fmt: skip
            affected: list[Any] = [tue]
            rid = "R-0100"
        elif setup == "C":
            thu = date(2026, 11, 5)
            repo.insert_request(conn, ward.id, Request(
                id="R-0200", nurse_id="charge", kind=RequestKind.DEMAND, dates=[thu],
                shifts=["E"], target_total=3, hard=False,
                status=RequestStatus.PENDING_CONFIRM, created_at=now))  # fmt: skip
            lifecycle.confirm(conn, ward, "R-0200")
            affected, rid = [thu], "R-0200"
        else:
            raise ValueError(f"unknown setup {setup!r}")
        plan = runs.start_resolve(
            conn, trigger=f"request:{rid}", affected=affected, request_id=rid, now=now
        )
        sickcall.rank(conn, plan)
        return plan.run_id
    finally:
        runs.__dict__["new_run_id"], sickcall.__dict__["new_run_id"] = saved
