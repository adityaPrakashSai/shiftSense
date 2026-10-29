"""Import a corrected roster grid as the published plan (design doc 7.8, 6.5c).

The charge nurse fixes the photo preview (`app/intake/photo.py`) and clicks Import. The grid
becomes a `kind = import` run with no parent, published at once, so it is the period's planned
version and the root of later re-solves. Leave cells (`L:CL` ...) become active leave requests,
so the solver keeps those days free; a bare `L` becomes whole-day unavailability. A paper plan is
the ward's real plan even when it breaks a rule, so hard-rule breaks are returned as warnings
(the independent validator's names), not refused. Nothing here calls a model.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.domain.models import (
    LeaveType,
    Request,
    RequestKind,
    RequestStatus,
    RunKind,
    RunStatus,
    Ward,
)
from app.log.decision_log import DecisionLog
from app.planner.build_input import build_input
from app.planner.runs import new_run_id, publish
from app.solver.validate import violations
from app.store import repo

CODE = re.compile(r"^(M|E|N|G|N/O|D/O|L:[A-Z]+)$")
SHIFT_CODES = ("M", "E", "N", "G")


class ImportRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ImportResult:
    run_id: str
    superseded: str | None
    leave_requests: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # broken hard rules, by constraint name


def check_grid(ward: Ward, rows: list[tuple[str, list[str]]]) -> None:
    known = {n.id for n in ward.nurses}
    seen: set[str] = set()
    for nurse_id, cells in rows:
        if nurse_id not in known:
            raise ImportRejected("NURSE_NOT_FOUND", f"No nurse {nurse_id} in {ward.id}.")
        if nurse_id in seen:
            raise ImportRejected("DUPLICATE_NURSE", f"{nurse_id} has two rows; pick another id.")
        seen.add(nurse_id)
        if len(cells) != ward.horizon_days:
            raise ImportRejected(
                "WRONG_DAYS",
                f"{nurse_id}: {len(cells)} cells for a {ward.horizon_days}-day period.",
            )
        bad = [c for c in cells if not CODE.match(c)]
        if bad:
            raise ImportRejected(
                "UNREADABLE_CELLS",
                f"{nurse_id}: fix the cells marked {', '.join(sorted(set(bad)))}.",
            )


def _covered(conn: sqlite3.Connection, ward: Ward) -> set[tuple[str, date]]:
    out: set[tuple[str, date]] = set()
    for r in repo.list_requests(conn, ward.id, status=RequestStatus.ACTIVE):
        if r.kind in (RequestKind.LEAVE, RequestKind.UNAVAILABLE) and r.shifts is None:
            out |= {(r.nurse_id, d) for d in r.dates}
    return out


def _leave_requests(
    conn: sqlite3.Connection, ward: Ward, rows: list[tuple[str, list[str]]]
) -> list[Request]:
    """One active request per nurse and leave type, for days not already on leave."""
    covered = _covered(conn, ward)
    by_key: dict[tuple[str, str], list[date]] = {}
    for nurse_id, cells in rows:
        for i, c in enumerate(cells):
            day = ward.horizon_start + timedelta(days=i)
            if c.startswith("L:") and (nurse_id, day) not in covered:
                by_key.setdefault((nurse_id, c[2:]), []).append(day)
    out = []
    now = repo.now_iso()
    for (nurse_id, kind), days in sorted(by_key.items()):
        leave = kind in LeaveType.__members__
        out.append(
            Request.model_validate(
                {
                    "id": repo.next_request_id(conn),
                    "nurse_id": nurse_id,
                    "kind": RequestKind.LEAVE if leave else RequestKind.UNAVAILABLE,
                    "leave_type": kind if leave else None,
                    "dates": days,
                    "hard": True,
                    "status": RequestStatus.ACTIVE,
                    "created_at": now,
                }
            )
        )
        repo.insert_request(conn, ward.id, out[-1])
    return out


def import_grid(
    conn: sqlite3.Connection,
    ward: Ward,
    rows: list[tuple[str, list[str]]],
    *,
    by: str = "charge",
    log: DecisionLog | None = None,
) -> ImportResult:
    """Store the grid as the published, planned run. `rows`: (nurse id, one code per day)."""
    check_grid(ward, rows)
    log = log or DecisionLog(conn, ward.id)
    leave = _leave_requests(conn, ward, rows)
    grid = dict(rows)
    roster: dict[str, list[str | None]] = {
        n.id: [c if c in SHIFT_CODES else None for c in grid.get(n.id, [])]
        or [None] * ward.horizon_days
        for n in ward.nurses
    }
    active = [r for r in repo.list_requests(conn, ward.id) if r.status is RequestStatus.ACTIVE]
    inp = build_input(ward, active, repo.get_history(conn, ward.id))
    warnings = violations(
        inp, {(n, d): s for n, cells in roster.items() for d, s in enumerate(cells)}
    )
    run_id = new_run_id()
    repo.insert_run(
        conn,
        run_id=run_id,
        ward_id=ward.id,
        kind=RunKind.IMPORT,
        status=RunStatus.PROPOSED,
        trigger="import",
        horizon_start=ward.horizon_start,
        priorities=inp.priorities,
        input_json=inp.to_json(),
        input_hash=inp.input_hash(),
    )
    repo.insert_assignments(conn, run_id, roster, ward.horizon_start)
    log.emit(
        "roster.imported", f"user:{by}", run_id, None,
        nurses=len(rows), leave_requests=[r.id for r in leave], warnings=warnings[:50],
    )  # fmt: skip
    old = publish(conn, run_id, by=by, log=log)
    return ImportResult(run_id, old, [r.id for r in leave], warnings)
