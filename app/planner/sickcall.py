"""Sick-call flow and replacement ranking (design doc 6.4, 6.5b).

Runs when a confirmed change opens a published cell within the next 72 hours (a sick call or
leave removes a nurse), or a demand increase adds a slot. Otherwise a plain re-solve runs and the
proposal just shows the diff.

1. Open cells: the published cells the edit removes, then the slots a demand increase adds.
2. Candidate filter (milliseconds): every nurse not already on that shift, including nurses who
   would move from another shift that day (the re-solve refills their cell). `rule_check` with all
   other cells fixed; nurses who would break a hard rule go to `excluded` with the rule names.
3. Baseline re-solve with no forcing (its tier vector is B) and, in parallel, up to 6 forced
   re-solves `assign(candidate, d, s)` (standby first, then nurses who are off that day, then
   fewest shifts this period, then id), each within the 3 s budget.
4. Rank by tier vector in priority order, then changed cells, then shifts this period, then id;
   keep the top 3. Equal vectors with equal changed cells are a TIE.
5. The top candidate's run is the `proposed` run; the others are stored as runs too, so "pick
   another candidate" publishes exactly what was shown.
Only the first open cell is ranked (the demo story has one); the re-solve fills the others.
"""

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from app.config import Settings, get_settings
from app.domain.models import RunKind, RunStatus, ShiftCode
from app.intake.dates import shift_start
from app.log.decision_log import DecisionLog
from app.planner.build_input import horizon_dates
from app.planner.runs import ResolvePlan, RunOutcome, infeasible, new_run_id, write_solved
from app.solver.changes import away_cells, change_set, count_changed
from app.solver.input import Forcing, SolveInput
from app.solver.rules import rule_check
from app.solver.solve import SolveResult, solve
from app.store import repo

MAX_FORCED = 6
KEEP = 3
BUDGET_S = 2.4  # wall time for the parallel re-solves; filter and writes fit in the rest of 3 s
STAGE_S = 1.5
WINDOW = timedelta(hours=72)


@dataclass(frozen=True)
class OpenCell:
    nurse: str | None  # None for a slot added by a demand change
    day: int
    shift: str


def open_cells(plan: ResolvePlan) -> list[OpenCell]:
    """Published cells removed by unavailable / leave rows (on or after the frozen point), then
    the slots a demand increase added."""
    inp = plan.inp
    assert inp.previous is not None
    away = away_cells(inp)
    out = [
        OpenCell(n, d, p)
        for (n, d), p in sorted(inp.previous.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        if p is not None
        and d >= inp.frozen_before_day
        and (n, d) in away
        and (away[n, d] is None or p in (away[n, d] or set()))
    ]
    out += [OpenCell(None, d, s) for d, s in plan.added_slots]
    return out


def within_window(plan: ResolvePlan, cell: OpenCell) -> bool:
    day = horizon_dates(plan.ward)[cell.day]
    return shift_start(plan.ward, ShiftCode(cell.shift), day) < plan.now + WINDOW


def _shifts_this_period(inp: SolveInput, n: str) -> int:
    assert inp.previous is not None
    return sum(1 for d in range(inp.num_days) if inp.previous.get((n, d)) is not None)


def filter_candidates(
    plan: ResolvePlan, cell: OpenCell, opened: list[OpenCell]
) -> tuple[list[str], list[dict[str, Any]]]:
    """6.4 step 2: (survivors in try order, excluded with rule names)."""
    inp = plan.inp
    assert inp.previous is not None
    roster = dict(inp.previous)
    for c in opened:
        if c.nurse is not None:
            roster[c.nurse, c.day] = None
    gone = {c.nurse for c in opened if c.nurse is not None}
    survivors, excluded = [], []
    for spec in inp.nurses:
        n = spec.id
        if n in gone or roster.get((n, cell.day)) == cell.shift or spec.role == "ans":
            continue
        broken = rule_check(roster, inp, n, cell.day, cell.shift, nurse_only=True)
        if broken:
            excluded.append({"nurse": n, "rules": broken})
        else:
            survivors.append(n)
    standby = {s.id for s in inp.nurses if s.standby}
    survivors.sort(
        key=lambda n: (
            n not in standby,
            roster.get((n, cell.day)) is not None,  # off that day before moving someone
            _shifts_this_period(inp, n),
            n,
        )
    )
    return survivors, excluded


def _vector(res: SolveResult, priorities: tuple[str, ...]) -> tuple[int, ...]:
    tv = res.tier_vector()
    return tuple(tv.get(t, 0) for t in priorities)


def rank(conn: sqlite3.Connection, plan: ResolvePlan, *, settings: Settings | None = None,
         log: DecisionLog | None = None) -> RunOutcome:  # fmt: skip
    """The whole flow for a prepared re-solve: ranked proposal when a cell opens within 72 h,
    a plain re-solve otherwise. Writes runs, the proposal row and the events."""
    s = settings or get_settings()
    log = log or DecisionLog(conn, plan.ward.id)
    t0 = time.perf_counter()
    inp, days = plan.inp, horizon_dates(plan.ward)
    opened = open_cells(plan)
    urgent = [c for c in opened if within_window(plan, c)]

    survivors: list[str] = []
    excluded: list[dict[str, Any]] = []
    cell = urgent[0] if urgent else None
    if cell is not None:
        survivors, excluded = filter_candidates(plan, cell, opened)
    t_filter = time.perf_counter()

    tried = survivors[:MAX_FORCED]
    jobs: list[SolveInput] = [inp]
    if cell is not None:
        jobs += [
            replace(inp, forced=(*inp.forced, Forcing("replacement", n, cell.day, cell.shift)))
            for n in tried
        ]
    threads = len(jobs)
    workers = max(2, min(s.solver_workers, (os.cpu_count() or 8) // threads))

    def one(job: SolveInput) -> SolveResult:
        return solve(job, stage_seconds=STAGE_S, workers=workers, total_seconds=BUDGET_S)

    with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="sickcall") as pool:
        results = list(pool.map(one, jobs))
    t_solve = time.perf_counter()
    base, forced = results[0], results[1:]

    pr = inp.priorities
    cands = []
    for n, job, res in zip(tried, jobs[1:], forced, strict=True):
        if res.status != "solved":
            excluded.append({"nurse": n, "rules": [f"no_feasible_roster:{res.status}"]})
            continue
        changes = change_set(job, res.roster, plan.added_slots)
        cands.append((n, job, res, changes))
    cands.sort(
        key=lambda c: (_vector(c[2], pr), count_changed(c[3]), _shifts_this_period(inp, c[0]), c[0])
    )
    cands = cands[:KEEP]

    if base.status != "solved" and not cands:
        status = RunStatus.INFEASIBLE if base.status == "infeasible" else RunStatus.FAILED
        repo.update_run(conn, plan.run_id, status=status.value, total_secs=round(base.secs, 3))
        return infeasible(conn, plan.run_id, plan.ward, inp, base, status, log)

    standby = {x.id for x in inp.nurses if x.standby}
    best = _vector(cands[0][2], pr) if cands else None
    ranked: list[dict[str, Any]] = []
    for i, (n, _job, res, changes) in enumerate(cands):
        run_id = plan.run_id if i == 0 else new_run_id()
        vec = res.tier_vector()
        assert inp.previous is not None
        before = inp.previous.get((n, cell.day)) if cell else None
        entry: dict[str, Any] = {
            "nurse": n,
            "rank": i + 1,
            "standby": n in standby,
            "kind": "call in" if before is None else f"move from {before}",
            "tiers": vec,
            "delta_vs_base": {t: vec.get(t, 0) - base.tier_vector().get(t, 0) for t in pr}
            if base.status == "solved"
            else {},
            "delta_vs_best": {
                t: v - b
                for t, v, b in zip(pr, _vector(res, pr), best or (), strict=False)
                if v != b
            },
            "changed_cells": count_changed(changes),
            "run_id": run_id,
            "proven": res.proven,
        }
        if i > 0:
            worse = next((t for t, v in entry["delta_vs_best"].items() if v > 0), None)
            entry["reason_code"] = f"TIER:{worse}" if worse else "CHANGED_CELLS"
        ranked.append(entry)
    tied = [
        e
        for e in ranked
        if e["tiers"] == ranked[0]["tiers"] and e["changed_cells"] == ranked[0]["changed_cells"]
    ]
    if len(tied) > 1:
        load = {_shifts_this_period(inp, e["nurse"]) for e in tied}
        tie_break = "fewest shifts this period" if len(load) > 1 else "nurse id"
        for e in tied:
            e["tie"] = True
            e["tie_break"] = tie_break
            if e["rank"] > 1:
                e["reason_code"] = "TIE"

    # write runs: the top candidate (or the plain re-solve) into the reserved run
    if cands:
        top_job, top_res = cands[0][1], cands[0][2]
        repo.update_run(
            conn, plan.run_id, input_json=top_job.to_json(), input_hash=top_job.input_hash()
        )
    else:
        top_job, top_res = inp, base
    write_solved(conn, plan.run_id, plan.ward, top_job, top_res, plan.active,
                 added=plan.added_slots, log=log)  # fmt: skip
    for entry, (_n, job, res, _ch) in zip(ranked[1:], cands[1:], strict=True):
        repo.insert_run(
            conn,
            run_id=entry["run_id"],
            ward_id=plan.ward.id,
            kind=RunKind.RESOLVE,
            status=RunStatus.SOLVING,
            trigger=f"candidate:{plan.run_id}",
            horizon_start=plan.ward.horizon_start,
            priorities=job.priorities,
            input_json=job.to_json(),
            input_hash=job.input_hash(),
            parent_run=plan.parent_run,
            frozen_before_day=job.frozen_before_day,
        )
        write_solved(conn, entry["run_id"], plan.ward, job, res, plan.active,
                     added=plan.added_slots, log=DecisionLog(conn, plan.ward.id))  # fmt: skip

    t_end = time.perf_counter()
    published = repo.get_run(conn, plan.parent_run)
    published_cov = next(
        (st["best"] for st in repo.get_stages(conn, plan.parent_run) if st["tier"] == "coverage"),
        None,
    ) if published else None  # fmt: skip
    top_cov = top_res.tier_vector().get("coverage", 0)
    ranking: dict[str, Any] = {
        "open_cell": None
        if cell is None
        else {"nurse": cell.nurse, "day": days[cell.day].isoformat(), "shift": cell.shift},
        "candidates": ranked,
        "excluded": sorted(excluded, key=lambda e: e["nurse"]),
        "baseline": base.tier_vector() if base.status == "solved" else None,
        "coverage_worse": published_cov is not None and top_cov > published_cov,
        "published_coverage": published_cov,
        "frozen_before_day": inp.frozen_before_day,
        "timing_ms": {
            "filter": round(1000 * (t_filter - t0)),
            "solves": round(1000 * (t_solve - t_filter)),
            "total": round(1000 * (t_end - t0)),
        },
        "parallel_solves": threads,
        "workers_each": workers,
    }
    repo.insert_proposal(
        conn,
        plan.run_id,
        request_id=plan.request_id,
        open_cells=[
            {"nurse": c.nurse, "day": days[c.day].isoformat(), "shift": c.shift} for c in opened
        ],
        ranking=ranking,
    )
    if cell is not None:
        log.emit("candidates.ranked", "planner", plan.run_id, **ranking)
    repo.update_run(conn, plan.run_id, total_secs=round(t_end - t0, 3))
    return RunOutcome(plan.run_id, RunStatus.PROPOSED, top_res)
