"""GET /api/ward and GET /api/roster (design doc 10.1)."""

from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from app.api import jobs
from app.api.deps import Conn, require_run, require_ward
from app.api.errors import ApiError
from app.api.schemas import PrioritiesBody, RosterOut
from app.api.ws import hub
from app.config import get_settings
from app.domain.models import Ward
from app.planner.roster_view import roster_cells, run_summary
from app.store import repo

router = APIRouter(prefix="/api", tags=["ward"])


@router.get("/ward", response_model=Ward)
def get_ward(conn: Conn) -> Ward:
    return require_ward(conn)


@router.put("/ward/priorities", response_model=Ward)
async def set_priorities(conn: Conn, body: PrioritiesBody) -> Ward:
    """Reorder the priority tiers (5.3, P1); the next solve uses the new order."""
    ward = require_ward(conn)
    if sorted(body.priorities) != sorted(ward.priorities):
        raise ApiError(422, "BAD_PRIORITIES",
                       f"Give the tiers {sorted(ward.priorities)} in a new order.")  # fmt: skip
    before = list(ward.priorities)
    ward.priorities = list(body.priorities)
    repo.save_ward(conn, ward)
    jobs.live_log(conn, ward.id).emit("ward.priorities", "user:charge", None, None,
                                      before=before, after=ward.priorities)  # fmt: skip
    await hub.broadcast(ward.id, {"type": "ward_updated"})
    return ward


def ward_today(tz: str) -> date:
    demo_now = get_settings().demo_now
    if demo_now:
        return datetime.fromisoformat(demo_now).astimezone(ZoneInfo(tz)).date()
    return datetime.now(ZoneInfo(tz)).date()


@router.get("/roster", response_model=RosterOut)
def get_roster(
    conn: Conn,
    run_id: str | None = None,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
) -> RosterOut:
    """The published roster (or `run_id`), optionally limited to days `from`..`to`."""
    ward = require_ward(conn)
    if run_id is None:
        run = repo.get_published_run(conn, ward.id)
        if run is None:
            raise ApiError(404, "NO_PUBLISHED_RUN", "No published roster yet; generate one.")
    else:
        run = require_run(conn, run_id)
    cells, totals = roster_cells(conn, ward, run)

    def keep(iso: str) -> bool:
        d = date.fromisoformat(iso)
        return (from_ is None or d >= from_) and (to is None or d <= to)

    days = sorted({c["day"] for c in cells if keep(c["day"])})
    today = ward_today(ward.tz)
    return RosterOut.model_validate(
        {
            "run": run_summary(run),
            "days": days,
            "today": today if days and days[0] <= today.isoformat() <= days[-1] else None,
            "grid": [c for c in cells if keep(c["day"])],
            "totals": [t for t in totals if keep(t["day"])],
            "stages": repo.get_stages(conn, str(run["run_id"])),
        }
    )
