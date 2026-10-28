"""Run orchestration (design doc 3.2, 6): build the SolveInput, call the engine, write evidence.

`run_full` builds a fresh roster for the ward's period (status `proposed`); `publish` makes a run
the baseline. Minimal-change re-solves (`run_resolve`) arrive in M7.
"""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.domain.models import Request, RequestStatus, RunKind, RunStatus, Ward
from app.evidence.writer import write_evidence
from app.log.decision_log import DecisionLog, SolverEvents
from app.planner.build_input import build_input
from app.solver.input import SolveInput
from app.solver.solve import SolveResult, solve
from app.store import repo


@dataclass
class RunOutcome:
    run_id: str
    status: RunStatus
    result: SolveResult


def new_run_id() -> str:
    return f"run-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


@dataclass
class PreparedRun:
    """A run row already inserted with status `solving`, ready to be solved."""

    run_id: str
    ward: Ward
    inp: SolveInput
    active: list[Request]


def start_full(
    conn: sqlite3.Connection,
    ward_id: str | None = None,
    *,
    trigger: str = "generate",
    log: DecisionLog | None = None,
) -> PreparedRun:
    """Build the SolveInput, insert the run (status `solving`) and log `run.started`. Fast."""
    ward = repo.get_ward(conn, ward_id)
    if ward is None:
        raise LookupError("no ward in the database")
    requests = repo.list_requests(conn, ward.id)
    active = [r for r in requests if r.status is RequestStatus.ACTIVE]
    inp = build_input(ward, active, repo.get_history(conn, ward.id))
    log = log or DecisionLog(conn, ward.id)

    published = repo.get_published_run(conn, ward.id)
    parent = None if published is None else str(published["run_id"])
    run_id = new_run_id()
    repo.insert_run(
        conn,
        run_id=run_id,
        ward_id=ward.id,
        kind=RunKind.FULL,
        status=RunStatus.SOLVING,
        trigger=trigger,
        horizon_start=ward.horizon_start,
        priorities=inp.priorities,
        input_json=inp.to_json(),
        input_hash=inp.input_hash(),
        parent_run=parent,
    )
    log.emit(
        "run.started",
        "planner",
        run_id,
        kind=RunKind.FULL.value,
        trigger=trigger,
        parent_run=parent,
        priorities=list(inp.priorities),
        frozen_before_day=0,
        input_hash=inp.input_hash(),
    )
    return PreparedRun(run_id, ward, inp, active)


def finish_full(
    conn: sqlite3.Connection,
    prep: PreparedRun,
    *,
    settings: Settings | None = None,
    log: DecisionLog | None = None,
) -> RunOutcome:
    """Solve a prepared run and write its evidence (seconds; runs in a worker thread)."""
    s = settings or get_settings()
    run_id, inp = prep.run_id, prep.inp
    log = log or DecisionLog(conn, prep.ward.id)
    res = solve(
        inp,
        stage_seconds=s.solver_stage_seconds,
        workers=s.solver_workers,
        sink=SolverEvents(log, run_id),
        total_seconds=s.solver_total_seconds,
        log_dir=Path(s.log_dir) / run_id,
    )
    if res.status != "solved":
        status = RunStatus.INFEASIBLE if res.status == "infeasible" else RunStatus.FAILED
        repo.update_run(conn, run_id, status=status.value, total_secs=round(res.secs, 3))
        log.emit("run.infeasible", "solver", run_id, stage=res.failed_tier, core=[])
        return RunOutcome(run_id, status, res)

    write_evidence(conn, run_id, prep.ward, inp, res, prep.active)
    repo.update_run(
        conn,
        run_id,
        status=RunStatus.PROPOSED.value,
        total_secs=round(res.secs, 3),
        changed_cells=res.changed_cells,
    )
    log.emit(
        "run.proposed",
        "planner",
        run_id,
        changed_cells=res.changed_cells,
        change_set=[],
        tier_vector=res.tier_vector(),
        proven=res.proven,
        secs=round(res.secs, 3),
    )
    return RunOutcome(run_id, RunStatus.PROPOSED, res)


def run_full(
    conn: sqlite3.Connection,
    ward_id: str | None = None,
    *,
    trigger: str = "generate",
    settings: Settings | None = None,
    log: DecisionLog | None = None,
) -> RunOutcome:
    """Build a fresh roster for the ward's period (status `proposed`)."""
    prep = start_full(conn, ward_id, trigger=trigger, log=log)
    return finish_full(conn, prep, settings=settings, log=log)


def publish(
    conn: sqlite3.Connection, run_id: str, *, by: str = "charge", log: DecisionLog | None = None
) -> str | None:
    """Make a proposed run the published baseline; returns the superseded run id."""
    run = repo.get_run(conn, run_id)
    if run is None:
        raise LookupError(run_id)
    if run["status"] != RunStatus.PROPOSED.value:
        raise ValueError(f"run {run_id} is {run['status']}, not proposed")
    old = repo.set_published(conn, run["ward_id"], run_id, by)
    log = log or DecisionLog(conn, run["ward_id"])
    actor = "system" if by in ("seed", "system") else f"user:{by}"
    log.emit("run.published", actor, run_id, superseded=old)
    return old
