"""Grid view of a stored run (design doc 10.3): display codes, leave banners, daily totals."""

import json
import sqlite3
from datetime import date
from typing import Any

from app.domain.models import RequestKind, RequestStatus, Ward
from app.planner.totals import daily_totals
from app.solver.input import SHIFTS, SolveInput
from app.store import repo


def leave_codes(conn: sqlite3.Connection, ward: Ward) -> dict[tuple[str, date], str]:
    """(nurse, date) -> leave code for active leave; whole-day unavailability shows as "L"."""
    out: dict[tuple[str, date], str] = {}
    for r in repo.list_requests(conn, ward.id, status=RequestStatus.ACTIVE):
        if r.kind is RequestKind.LEAVE:
            for d in r.dates:
                out[r.nurse_id, d] = r.leave_type.value if r.leave_type else "L"
        elif r.kind is RequestKind.UNAVAILABLE and r.shifts is None:
            for d in r.dates:
                out.setdefault((r.nurse_id, d), "L")
    return out


def roster_cells(
    conn: sqlite3.Connection, ward: Ward, run: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cells and daily totals for a run, in nurse order then day order."""
    run_id = str(run["run_id"])
    rows = conn.execute(
        "SELECT nurse_id, day, shift, prev_shift, changed, cause FROM assignment WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    by_cell = {(r["nurse_id"], r["day"]): r for r in rows}
    days = sorted({r["day"] for r in rows})
    inp = SolveInput.from_json(run["input_json"]) if run.get("input_json") else None
    leave = leave_codes(conn, ward)

    cells = []
    roster: dict[str, list[str | None]] = {}
    for n in ward.nurses:
        prev: str | None = None
        if inp is not None and n.id in inp.history:
            prev = inp.history[n.id].last_shifts[-1]
        roster[n.id] = []
        for iso in days:
            r = by_cell.get((n.id, iso))
            shift = None if r is None else r["shift"]
            roster[n.id].append(shift)
            lt = leave.get((n.id, date.fromisoformat(iso)))
            if shift is not None:
                display = shift
            elif lt is not None:
                display = lt
            elif prev == "N":
                display = "N/O"
            else:
                display = "D/O"
            cells.append(
                {
                    "nurse_id": n.id,
                    "day": iso,
                    "shift": shift,
                    "display": display,
                    "leave_type": lt if shift is None else None,
                    "prev_shift": None if r is None else r["prev_shift"],
                    "changed": None if r is None or r["changed"] is None else bool(r["changed"]),
                    "cause": None if r is None else r["cause"],
                }
            )
            prev = shift

    got = daily_totals(roster, len(days))
    demand = {(d.day.isoformat(), d.shift.value): d for d in ward.demand}
    totals = []
    for i, iso in enumerate(days):
        for s in SHIFTS:
            dm = demand.get((iso, s))
            target = 0 if dm is None else dm.target_total
            totals.append(
                {
                    "day": iso,
                    "shift": s,
                    "assigned": got[s][i],
                    "target": target,
                    "min": 0 if dm is None else dm.min_total,
                    "max": target if dm is None or dm.max_total is None else dm.max_total,
                }
            )
    return cells, totals


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in run.items() if k != "input_json"}
    out["priorities"] = json.loads(run["priorities"] or "[]")
    return out
