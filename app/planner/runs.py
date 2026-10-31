"""Run orchestration (design doc 3.2, 6): build the SolveInput, call the engine, write evidence.

`run_full` builds a fresh roster for the ward's period (status `proposed`). `start_resolve` /
`run_resolve` re-plan only the future after the roster is published (6.1, 6.2): days before the
frozen point and shifts already under way are fixed, every changed cell costs in the stability
tier, the solve starts from the published roster as a hint, and standby nurses may be called in.
`publish` makes a run the baseline. The ranked sick-call flow built on these is `sickcall.py`.
"""

import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.domain.models import Request, RequestStatus, RunKind, RunStatus, Ward
from app.evidence.writer import write_evidence
from app.intake.dates import shift_start, ward_now
from app.log.decision_log import DecisionLog, SolverEvents
from app.planner.build_input import build_input, day_index, horizon_dates
from app.solver.changes import Change, change_set, count_changed
from app.solver.input import SHIFTS, Forcing, SolveInput
from app.solver.solve import SolveResult, solve
from app.store import repo


@dataclass
class RunOutcome:
    run_id: str
    status: RunStatus
    result: SolveResult
    conflict_id: str | None = None  # set when the run was infeasible and diagnosed (5.5)


def infeasible(
    conn: sqlite3.Connection,
    run_id: str,
    ward: Ward,
    inp: SolveInput,
    res: SolveResult,
    status: RunStatus,
    log: DecisionLog,
) -> RunOutcome:
    """Log a failed solve; an infeasible one is diagnosed into a conflict with options (5.5)."""
    from app.planner import conflicts  # conflicts imports planner helpers

    cid = None
    core: list[str] = []
    if status is RunStatus.INFEASIBLE:
        cid = conflicts.create(conn, run_id, ward, inp, log)
        c = repo.get_conflict(conn, cid)
        core = [] if c is None else list(c["core"]["names"])
    log.emit("run.infeasible", "solver", run_id, stage=res.failed_tier, core=core,
             conflict_id=cid)  # fmt: skip
    return RunOutcome(run_id, status, res, cid)


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
    inp = build_input(
        ward, active, repo.get_history(conn, ward.id), waived=repo.active_waivers(conn, ward.id)
    )
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
        return infeasible(conn, run_id, prep.ward, inp, res, status, log)

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


# ---------------------------------------------------------------- minimal-change re-solve (6.1-6.3)


@dataclass
class ResolvePlan:
    """A re-solve run already inserted with status `solving` (fast), ready to be solved."""

    run_id: str
    ward: Ward
    inp: SolveInput  # with previous, frozen_before_day, frozen_cells, standby allowed
    active: list[Request]
    parent_run: str
    now: datetime
    trigger: str
    request_id: str | None = None
    added_slots: tuple[tuple[int, str], ...] = ()  # (day, shift) per nurse a demand change adds
    extra: dict[str, object] = field(default_factory=dict)


def frozen_point(
    ward: Ward, inp: SolveInput, previous: dict[tuple[str, int], str | None], now: datetime,
    affected: Iterable[date],
) -> tuple[int, frozenset[tuple[str, int]], frozenset[tuple[str, int, str | None]]]:  # fmt: skip
    """6.1: days before today (or tomorrow, when the earliest affected shift is not today) are
    frozen. When today is open, a shift already under way is fixed too: its nurses keep their cell
    and nobody else can join it (extra `unavailable` rows)."""
    today = now.date()
    t = day_index(ward, today)
    first = min(affected, default=None)
    point = t if first is not None and first <= today else t + 1
    point = max(0, min(point, inp.num_days))
    cells: set[tuple[str, int]] = set()
    blocked: set[tuple[str, int, str | None]] = set()
    if point == t and 0 <= t < inp.num_days:
        day = horizon_dates(ward)[t]
        started = {s.code.value for s in ward.shifts if s.code.value in SHIFTS
                   and shift_start(ward, s.code, day) <= now}  # fmt: skip
        for n in inp.nurses:
            p = previous.get((n.id, t))
            if p is not None and p in started:
                cells.add((n.id, t))
            else:
                blocked |= {(n.id, t, s) for s in started}
    return point, frozenset(cells), frozenset(blocked)


def published_roster(
    conn: sqlite3.Connection, run_id: str, inp: SolveInput
) -> dict[tuple[str, int], str | None]:
    grid = repo.get_roster(conn, run_id)
    return {
        (n.id, d): (grid.get(n.id) or [None] * inp.num_days)[d]
        for n in inp.nurses
        for d in range(inp.num_days)
    }


def added_slots(old: SolveInput | None, new: SolveInput) -> tuple[tuple[int, str], ...]:
    """Slots a demand increase added since the published run (one entry per extra nurse)."""
    if old is None:
        return ()
    out: list[tuple[int, str]] = []
    for k, dm in sorted(new.demand.items()):
        before = old.demand.get(k)
        if before is not None and dm.target_total > before.target_total:
            out += [k] * (dm.target_total - before.target_total)
    return tuple(out)


def resolve_input(
    conn: sqlite3.Connection,
    ward_id: str | None = None,
    *,
    affected: Iterable[date] = (),
    now: datetime | None = None,
    extra_unavailable: Iterable[tuple[str, int, str | None]] = (),
    forced: Iterable[Forcing] = (),
) -> tuple[Ward, SolveInput, dict[str, Any], list[Request], datetime]:
    """6.2 steps 1-3 without writing anything: (ward, input with previous / frozen point /
    waivers, published run, active requests, now). `extra_unavailable` holds cells a declined
    replacement cannot take (6.5): hard for this re-solve only, never stored as a request.
    `forced` pins cells (a conditional deal keeps its nurse on the open cell)."""
    ward = repo.get_ward(conn, ward_id)
    if ward is None:
        raise LookupError("no ward in the database")
    published = repo.get_published_run(conn, ward.id)
    if published is None:
        raise LookupError("no published run to re-solve from")
    now = now or ward_now(ward.tz, get_settings().demo_now)
    active = [r for r in repo.list_requests(conn, ward.id) if r.status is RequestStatus.ACTIVE]
    inp = build_input(
        ward,
        active,
        repo.get_history(conn, ward.id),
        standby_allowed=True,
        waived=repo.active_waivers(conn, ward.id),
    )
    previous = published_roster(conn, str(published["run_id"]), inp)
    point, cells, blocked = frozen_point(ward, inp, previous, now, affected)
    inp = replace(
        inp,
        previous=previous,
        frozen_before_day=point,
        frozen_cells=cells,
        unavailable=inp.unavailable | blocked | frozenset(extra_unavailable),
        forced=(*inp.forced, *forced),
    )
    return ward, inp, published, active, now


def start_resolve(
    conn: sqlite3.Connection,
    ward_id: str | None = None,
    *,
    trigger: str,
    affected: Iterable[date] = (),
    request_id: str | None = None,
    now: datetime | None = None,
    log: DecisionLog | None = None,
    extra_unavailable: Iterable[tuple[str, int, str | None]] = (),
    forced: Iterable[Forcing] = (),
) -> ResolvePlan:
    """6.2 steps 1-3 (`resolve_input`), then insert the run (status `solving`) and log
    `run.started`."""
    ward, inp, published, active, now = resolve_input(
        conn,
        ward_id,
        affected=affected,
        now=now,
        extra_unavailable=extra_unavailable,
        forced=forced,
    )
    point = inp.frozen_before_day
    old = SolveInput.from_json(published["input_json"]) if published["input_json"] else None
    log = log or DecisionLog(conn, ward.id)
    run_id = new_run_id()
    parent = str(published["run_id"])
    repo.insert_run(
        conn,
        run_id=run_id,
        ward_id=ward.id,
        kind=RunKind.RESOLVE,
        status=RunStatus.SOLVING,
        trigger=trigger,
        horizon_start=ward.horizon_start,
        priorities=inp.priorities,
        input_json=inp.to_json(),
        input_hash=inp.input_hash(),
        parent_run=parent,
        frozen_before_day=point,
    )
    log.emit(
        "run.started",
        "planner",
        run_id,
        kind=RunKind.RESOLVE.value,
        trigger=trigger,
        parent_run=parent,
        priorities=list(inp.priorities),
        frozen_before_day=point,
        input_hash=inp.input_hash(),
    )
    return ResolvePlan(
        run_id, ward, inp, active, parent, now, trigger, request_id, added_slots(old, inp)
    )


def write_solved(
    conn: sqlite3.Connection,
    run_id: str,
    ward: Ward,
    inp: SolveInput,
    res: SolveResult,
    active: list[Request],
    *,
    added: Iterable[tuple[int, str]] = (),
    log: DecisionLog,
) -> list[Change]:
    """Evidence, change set with causes (6.3) and status `proposed` for a solved re-solve."""
    changes = change_set(inp, res.roster, added)
    write_evidence(
        conn, run_id, ward, inp, res, active, causes={(c.nurse, c.day): c.cause for c in changes}
    )
    n_changed = count_changed(changes)
    repo.update_run(
        conn,
        run_id,
        status=RunStatus.PROPOSED.value,
        total_secs=round(res.secs, 3),
        changed_cells=n_changed,
    )
    days = horizon_dates(ward)
    log.emit(
        "run.proposed",
        "planner",
        run_id,
        changed_cells=n_changed,
        change_set=[
            {
                "nurse_id": c.nurse,
                "day": days[c.day].isoformat(),
                "from": c.before,
                "to": c.after,
                "cause": c.cause,
            }
            for c in changes
        ],  # fmt: skip
        tier_vector=res.tier_vector(),
        proven=res.proven,
        secs=round(res.secs, 3),
    )
    return changes


def run_resolve(
    conn: sqlite3.Connection,
    plan: ResolvePlan,
    *,
    settings: Settings | None = None,
    log: DecisionLog | None = None,
    stage_seconds: float | None = None,
    total_seconds: float | None = None,
) -> RunOutcome:
    """6.2 steps 4-5: solve the prepared re-solve and write it as a `proposed` run."""
    s = settings or get_settings()
    log = log or DecisionLog(conn, plan.ward.id)
    res = solve(
        plan.inp,
        stage_seconds=stage_seconds or s.solver_stage_seconds,
        workers=s.solver_workers,
        sink=SolverEvents(log, plan.run_id),
        total_seconds=total_seconds or s.solver_total_seconds,
        log_dir=Path(s.log_dir) / plan.run_id,
    )
    if res.status != "solved":
        status = RunStatus.INFEASIBLE if res.status == "infeasible" else RunStatus.FAILED
        repo.update_run(conn, plan.run_id, status=status.value, total_secs=round(res.secs, 3))
        return infeasible(conn, plan.run_id, plan.ward, plan.inp, res, status, log)
    write_solved(
        conn, plan.run_id, plan.ward, plan.inp, res, plan.active, added=plan.added_slots, log=log
    )
    return RunOutcome(plan.run_id, RunStatus.PROPOSED, res)


def evaluate_roster(
    conn: sqlite3.Connection,
    roster: dict[str, list[str | None]],
    *,
    ward_id: str | None = None,
    trigger: str = "fixed",
    publish_it: bool = True,
) -> str:
    """Store a given roster as a full run with evidence (stages, costs, outcomes): the solver runs
    with every cell forced, so the tier values are exactly this roster's. Used for the reference
    baseline in tests and evals, and for imported plans later. Raises if it breaks a hard rule."""
    ward = repo.get_ward(conn, ward_id)
    if ward is None:
        raise LookupError("no ward in the database")
    active = [r for r in repo.list_requests(conn, ward.id) if r.status is RequestStatus.ACTIVE]
    inp = build_input(ward, active, repo.get_history(conn, ward.id))
    fixed = [
        Forcing("fixed", n.id, d, (roster.get(n.id) or [None] * inp.num_days)[d])
        for n in inp.nurses
        for d in range(inp.num_days)
    ]
    res = solve(replace(inp, forced=(*inp.forced, *fixed)), stage_seconds=2, workers=4)
    if res.status != "solved":
        raise ValueError(f"roster breaks hard rules ({res.status} at {res.failed_tier})")
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
    )
    write_evidence(conn, run_id, ward, inp, res, active)
    repo.update_run(conn, run_id, status=RunStatus.PROPOSED.value, total_secs=round(res.secs, 3))
    if publish_it:
        publish(conn, run_id, by="system")
    return run_id


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
    proposal = repo.proposal_for_run(conn, run_id)
    if proposal is not None:  # a sick-call proposal: record which candidate was published
        chosen = next(
            (c["nurse"] for c in proposal["ranking"].get("candidates", [])
             if c.get("run_id") == run_id),
            None,
        )  # fmt: skip
        repo.set_proposal_chosen(conn, proposal["run_id"], chosen)
    log = log or DecisionLog(conn, run["ward_id"])
    actor = "system" if by in ("seed", "system") else f"user:{by}"
    log.emit("run.published", actor, run_id, superseded=old)
    return old
