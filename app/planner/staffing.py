"""Staffing check for the current period (design doc 6.7, M14): a read-only what-if.

Builds the period's `SolveInput` from the ward, the active requests (approved leave and sick
calls are hard; preferences are ignored by the check) and history, applies the what-if (nurses
removed for the whole period, hypothetical hires H1..Hk, standby allowed or not, target or floor
coverage) and runs `app.solver.staffing.check`. It never writes to the database: the published
run and its assignments are untouched. Used by `POST /api/staffing-check`, the charge view's
Staffing check panel and the roster agent's `staffing_check` tool.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from app.config import get_settings
from app.domain.models import RequestStatus
from app.planner.build_input import build_input
from app.solver.staffing import Cover, check, hire_ids, with_hires, without
from app.store import repo

MAX_HIRES = 5
CAVEAT = (
    "Requests, sequence rules and history-based fairness are ignored, so every number is a lower "
    "bound for a publishable roster."
)


class StaffingInputError(ValueError):
    pass


def run_check(
    conn: sqlite3.Connection,
    ward_id: str | None = None,
    *,
    hires: int = 0,
    removed: Iterable[str] = (),
    coverage: Cover = "target",
    standby: bool = False,
    keep_staff: bool = True,
    seconds: float | None = None,
) -> dict[str, Any]:
    """`keep_staff`: today's regular nurses (minus `removed`) count as used, so the headcount
    answers "how many extra?"; False asks for the minimum headcount from scratch."""
    ward = repo.get_ward(conn, ward_id)
    if ward is None:
        raise LookupError("no ward in the database")
    gone = sorted(set(removed))
    known = {n.id for n in ward.nurses}
    if unknown := [n for n in gone if n not in known]:
        raise StaffingInputError(f"unknown nurse ids: {', '.join(unknown)}")
    if not 0 <= hires <= MAX_HIRES:
        raise StaffingInputError(f"hires must be between 0 and {MAX_HIRES}")
    active = [r for r in repo.list_requests(conn, ward.id) if r.status is RequestStatus.ACTIVE]
    inp = with_hires(
        without(build_input(ward, active, repo.get_history(conn, ward.id)), gone), hires
    )
    staff = [n.id for n in inp.nurses if not n.standby and n.role != "ans" and n.id in known]
    s = get_settings()
    res = check(
        inp, allow_standby=standby, fixed=staff if keep_staff else (), cover=coverage,
        seconds=seconds or s.solver_stage_seconds, workers=s.solver_workers,
    )  # fmt: skip
    names = {n.id: n.name for n in ward.nurses} | {h: f"Hire {h}" for h in hire_ids(hires)}

    def day(d: int) -> str:
        return (ward.horizon_start + timedelta(days=d)).isoformat()

    target_slots = sum(dm.target_total for dm in inp.demand.values())
    return {
        "status": res.status,
        "inputs": {
            "hires": hires,
            "removed": gone,
            "coverage": coverage,
            "standby": standby,
            "keep_staff": keep_staff,
        },
        "stages": [asdict(x) for x in res.stages],
        "shortfall": res.value("shortfall"),
        "headcount": res.value("nurses_used"),
        "hires_needed": sum(1 for n in res.used if n not in known),
        "standby_shifts": res.value("standby_shifts"),
        "used": [{"nurse_id": n, "name": names.get(n, n)} for n in res.used],
        "short_cells": [
            {"day": day(d), "shift": sh, "assigned": got, "target": tgt}
            for d, sh, got, tgt in res.short_cells
        ],
        "load": [{"nurse_id": n, "name": names.get(n, n), **v} for n, v in res.load.items()],
        "target_slots": target_slots,
        "secs": res.secs,
        "caveat": CAVEAT,
    }
