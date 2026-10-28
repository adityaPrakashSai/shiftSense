"""Sick-call proposals (design doc 6.4, 6.5, 10.1): read, list, choose a candidate."""

import sqlite3
from typing import Any

from fastapi import APIRouter

from app.api import jobs
from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.routes_intake import request_out
from app.api.routes_runs import change_set_rows
from app.api.schemas import ChooseBody, ProposalOut, RunSummary
from app.api.ws import hub
from app.domain.models import RunStatus
from app.planner.roster_view import run_summary
from app.planner.runs import publish
from app.store import repo

router = APIRouter(prefix="/api/proposals", tags=["proposals"])


def proposal_out(conn: sqlite3.Connection, p: dict[str, Any]) -> ProposalOut:
    run = repo.get_run(conn, p["run_id"])
    assert run is not None
    rk = p["ranking"]
    req = None if p["request_id"] is None else repo.get_request(conn, p["request_id"])
    outcomes = repo.latest_outcomes(conn, str(run["ward_id"]))
    return ProposalOut.model_validate(
        {
            "run_id": p["run_id"],
            "run": run_summary(run),
            "request": None if req is None else request_out(req, outcomes),
            "open_cells": p["open_cells"],
            "open_cell": rk.get("open_cell"),
            "candidates": rk.get("candidates", []),
            "excluded": rk.get("excluded", []),
            "baseline": rk.get("baseline"),
            "coverage_worse": rk.get("coverage_worse", False),
            "published_coverage": rk.get("published_coverage"),
            "timing_ms": rk.get("timing_ms", {}),
            "change_set": change_set_rows(conn, p["run_id"]),
            "chosen_nurse": p["chosen_nurse"],
            "message_draft": p["message_draft"],
        }
    )


@router.get("", response_model=list[ProposalOut])
def list_proposals(conn: Conn, open: bool = True) -> list[ProposalOut]:
    """Newest first; `open` = the proposal's run is still proposed."""
    ward = require_ward(conn)
    return [proposal_out(conn, p) for p in repo.list_proposals(conn, ward.id, open_only=open)]


@router.get("/{run_id}", response_model=ProposalOut)
def get_proposal(conn: Conn, run_id: str) -> ProposalOut:
    p = repo.get_proposal(conn, run_id)
    if p is None:
        raise ApiError(404, "PROPOSAL_NOT_FOUND", f"No proposal {run_id}.")
    return proposal_out(conn, p)


@router.post("/{run_id}/choose", response_model=RunSummary)
async def choose(conn: Conn, run_id: str, body: ChooseBody) -> RunSummary:
    """Publish the run of the chosen candidate (Approve = the top one, 6.5); `nurse_id` null
    publishes the proposal's own run (a plain re-solve has no candidates)."""
    p = repo.get_proposal(conn, run_id)
    if p is None:
        raise ApiError(404, "PROPOSAL_NOT_FOUND", f"No proposal {run_id}.")
    cand = next(
        (c for c in p["ranking"].get("candidates", []) if c["nurse"] == body.nurse_id), None
    )
    if cand is None and body.nurse_id is not None:
        raise ApiError(404, "CANDIDATE_NOT_FOUND", f"{body.nurse_id} is not a ranked candidate.")
    target = run_id if cand is None else str(cand["run_id"])
    run = repo.get_run(conn, target)
    if run is None or run["status"] != RunStatus.PROPOSED.value:
        status = "missing" if run is None else run["status"]
        raise ApiError(409, "RUN_NOT_PROPOSED", f"Run {target} is {status}, not proposed.")
    ward_id = str(run["ward_id"])
    async with jobs.ward_lock(ward_id):
        publish(conn, target, by="charge", log=jobs.live_log(conn, ward_id))
    await hub.broadcast(ward_id, {"type": "roster_published", "run_id": target})
    published = repo.get_run(conn, target)
    assert published is not None
    return RunSummary.model_validate(run_summary(published))
