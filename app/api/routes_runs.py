"""Runs: generate, inspect, publish, lineage (design doc 10.1)."""

import sqlite3
from typing import Any

from fastapi import APIRouter, status

from app.agent.runtime import answers_for_published
from app.api import jobs
from app.api.deps import Conn, require_run, require_ward
from app.api.errors import ApiError
from app.api.schemas import CreateRun, PublishBody, RunAccepted, RunDetail, RunSummary
from app.api.ws import hub
from app.domain.models import RunStatus
from app.planner.roster_view import run_summary
from app.planner.runs import publish, start_full
from app.store import repo

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_run(conn: Conn, body: CreateRun | None = None) -> RunAccepted:
    """Start a full solve in the background; progress and the result arrive over the WebSocket."""
    ward = require_ward(conn)
    prep = start_full(conn, ward.id, trigger="generate", log=jobs.live_log(conn, ward.id))
    await hub.broadcast(ward.id, {"type": "run_started", "run_id": prep.run_id})
    jobs.spawn(prep)
    jobs.agent_start(ward.id, "new_period", prefetch=prep.run_id, run_id=prep.run_id)
    return RunAccepted(run_id=prep.run_id)


def change_set_rows(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT nurse_id, day, prev_shift, shift, cause FROM assignment "
        "WHERE run_id = ? AND changed = 1 ORDER BY day, nurse_id",
        (run_id,),
    )
    return [
        {
            "nurse_id": r["nurse_id"],
            "day": r["day"],
            "from_shift": r["prev_shift"],
            "to_shift": r["shift"],
            "cause": r["cause"],
        }
        for r in rows
    ]


@router.get("/{run_id}", response_model=RunDetail)
def get_run(conn: Conn, run_id: str) -> RunDetail:
    run = require_run(conn, run_id)
    costs = conn.execute(
        "SELECT tier, rule, SUM(amount * weight) AS total, COUNT(*) AS n FROM cost "
        "WHERE run_id = ? GROUP BY tier, rule ORDER BY tier, rule",
        (run_id,),
    )
    return RunDetail.model_validate(
        {
            "run": run_summary(run),
            "stages": repo.get_stages(conn, run_id),
            "costs": [
                {"tier": c["tier"], "rule": c["rule"], "total": c["total"], "rows": c["n"]}
                for c in costs
            ],
            "change_set": change_set_rows(conn, run_id),
            "request_outcomes": repo.get_request_outcomes(conn, run_id),
        }
    )


@router.post("/{run_id}/publish", response_model=RunSummary)
async def publish_run(conn: Conn, run_id: str, body: PublishBody | None = None) -> RunSummary:
    run = require_run(conn, run_id)
    if run["status"] != RunStatus.PROPOSED.value:
        raise ApiError(409, "RUN_NOT_PROPOSED", f"Run {run_id} is {run['status']}, not proposed.")
    by = (body or PublishBody()).by
    ward_id = str(run["ward_id"])
    async with jobs.ward_lock(ward_id):
        publish(conn, run_id, by=by, log=jobs.live_log(conn, ward_id))
    await hub.broadcast(ward_id, {"type": "roster_published", "run_id": run_id})
    jobs.agent_answer(ward_id, answers_for_published(conn, ward_id, run_id))
    return RunSummary.model_validate(run_summary(require_run(conn, run_id)))


@router.get("/{run_id}/lineage", response_model=list[RunSummary])
def lineage(conn: Conn, run_id: str) -> list[RunSummary]:
    """The run and its ancestors, oldest first (seed baseline -> ... -> this run)."""
    chain = []
    seen: set[str] = set()
    cur: dict[str, Any] | None = require_run(conn, run_id)
    while cur is not None and cur["run_id"] not in seen:
        seen.add(cur["run_id"])
        chain.append(RunSummary.model_validate(run_summary(cur)))
        parent = cur["parent_run"]
        cur = None if parent is None else repo.get_run(conn, parent)
    return list(reversed(chain))
