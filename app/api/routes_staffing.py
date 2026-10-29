"""Staffing check (design doc 6.7, M14): POST /api/staffing-check, a read-only what-if."""

import asyncio
from typing import Any

from fastapi import APIRouter

from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.schemas import StaffingBody, StaffingOut
from app.config import get_settings
from app.planner.staffing import StaffingInputError, run_check
from app.store.db import connect

router = APIRouter(prefix="/api", tags=["staffing"])


def _in_thread(ward_id: str, body: StaffingBody) -> dict[str, Any]:
    conn = connect(get_settings().db_path)
    try:
        return run_check(conn, ward_id, hires=body.hires, removed=body.removed,
                         coverage=body.coverage, standby=body.standby,
                         keep_staff=body.keep_staff)  # fmt: skip
    finally:
        conn.close()


@router.post("/staffing-check", response_model=StaffingOut)
async def staffing_check(conn: Conn, body: StaffingBody) -> StaffingOut:
    """Minimum headcount, shortfall cells, standby shifts and per-nurse load for this period under
    a what-if (hires, nurses out for the whole period, standby, target or floor). Nothing is
    stored; the published roster is untouched. Every number is a lower bound (see `caveat`)."""
    ward = require_ward(conn)
    try:
        out = await asyncio.to_thread(_in_thread, ward.id, body)
    except StaffingInputError as exc:
        raise ApiError(422, "BAD_STAFFING_INPUT", str(exc)) from exc
    return StaffingOut.model_validate(out)
