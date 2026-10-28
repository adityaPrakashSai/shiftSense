"""Lexicographic and dominance-weighted solves (design doc 5.4, 5.6).

`solve_lexicographic`: for each tier in `SolveInput.priorities`, minimise its penalty subject to the
hard rules and the locks of every earlier tier, then lock it at the best value found
(`lock:{tier}`, slack 0) and warm-start the next stage from that solution. A stage that hits its
time limit is locked at its best value and marked `proven = False`.

`solve_weighted`: one objective with dominance weights (each tier's multiplier exceeds the worst
case of all lower tiers combined); used as a cross-check that both give the same tier vector.

Stage `best` is reported as the tier's value in the final roster. It equals the stage optimum when
the stage is proven; an unproven stage may be improved by later stages, and the evidence (cost
rows) must add up to what was published.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from ortools.sat.python import cp_model

from app.solver.changes import change_set, count_changed
from app.solver.input import Forcing, SolveInput
from app.solver.model import RosterModel
from app.solver.validate import violations

RosterGrid = dict[tuple[str, int], str | None]


class EventSink(Protocol):
    def stage(self, tier: str, kind: str, **data: Any) -> None: ...


@dataclass
class ListSink:
    """In-memory sink for tests and the CLI."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def stage(self, tier: str, kind: str, **data: Any) -> None:
        self.events.append({"tier": tier, "kind": kind, **data})


@dataclass(frozen=True)
class Stage:
    rank: int
    tier: str
    status: str  # OPTIMAL | FEASIBLE | INFEASIBLE | UNKNOWN | MODEL_INVALID | skip
    best: int  # tier value in the final roster
    bound: int
    proven: bool
    secs: float
    locked: str | None  # "coverage <= 3"
    stage_best: int | None = None  # objective when the stage itself ended


@dataclass(frozen=True)
class TermValue:
    tier: str
    rule: str
    nurse: str | None
    day: int | None
    ref: str | None
    amount: int
    weight: int
    why: str


@dataclass
class SolveResult:
    status: Literal["solved", "infeasible", "failed"]
    roster: RosterGrid
    stages: list[Stage]
    term_values: list[TermValue]  # every soft term with value > 0
    changed_cells: int
    secs: float
    failed_tier: str | None = None
    denied: dict[str, bool] = field(default_factory=dict)  # soft request id -> denied
    method: Literal["lexicographic", "weighted"] = "lexicographic"

    def tier_vector(self) -> dict[str, int]:
        """Tier -> sum of weight x amount over its terms (the cost rows), in priority order."""
        out = {s.tier: 0 for s in self.stages}
        for t in self.term_values:
            out[t.tier] = out.get(t.tier, 0) + t.weight * t.amount
        return out

    @property
    def proven(self) -> bool:
        return all(s.proven for s in self.stages)


class ImprovingSolutionCallback(cp_model.CpSolverSolutionCallback):
    """Sends `stage.improved` for every improving solution within a stage (8.2)."""

    def __init__(self, sink: EventSink, tier: str, rank: int) -> None:
        super().__init__()
        self.sink, self.tier, self.rank, self.n = sink, tier, rank, 0

    def on_solution_callback(self) -> None:
        self.n += 1
        self.sink.stage(
            self.tier,
            "improved",
            rank=self.rank,
            solution=self.n,
            objective=round(self.objective_value),
            wall=round(self.wall_time, 3),
        )


def new_solver(
    seconds: float, workers: int, seed: int = 42, log: Callable[[str], None] | None = None
) -> cp_model.CpSolver:
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = max(seconds, 0.05)
    s.parameters.num_workers = workers
    s.parameters.random_seed = seed
    if log is not None:
        s.parameters.log_search_progress = True
        s.parameters.log_to_stdout = False
        s.log_callback = log
    return s


def _file_logger(log_dir: Path | None, name: str) -> tuple[Callable[[str], None] | None, Any]:
    if log_dir is None:
        return None, None
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = (log_dir / f"{name}.solver.log").open("w", encoding="utf-8")

    def write(line: str) -> None:
        fh.write(line + "\n")

    return write, fh


def changed_cells(inp: SolveInput, roster: RosterGrid) -> int:
    """Cells that differ from the published roster, not counting the cells the request itself
    removed (6.3)."""
    return count_changed(change_set(inp, roster))


def _finish(
    rm: RosterModel,
    solver: cp_model.CpSolver,
    inp: SolveInput,
    stages: list[Stage],
    t0: float,
    method: Literal["lexicographic", "weighted"],
) -> SolveResult:
    roster = rm.roster(solver)
    bad = violations(inp, roster)
    if bad:  # the model and the independent validator disagree: a solver bug, never ship it
        raise RuntimeError(f"solver roster breaks hard rules: {bad[:10]}")
    terms: list[TermValue] = []
    for tier_terms in rm.terms.values():
        for t in tier_terms:
            amount = int(solver.value(t.expr))
            if amount <= 0:
                continue
            why = t.why
            members = rm.spread_members.get(t.rule) if t.nurse is None else None
            if members:  # name who sets the max and the min of a fairness spread
                vals = sorted((int(solver.value(e)), n) for n, e in members)
                why = f"{why}: max {vals[-1][0]} ({vals[-1][1]}), min {vals[0][0]} ({vals[0][1]})"
            terms.append(TermValue(t.tier, t.rule, t.nurse, t.day, t.ref, amount, t.weight, why))
    res = SolveResult(
        "solved",
        roster,
        stages,
        terms,
        changed_cells(inp, roster),
        time.perf_counter() - t0,
        denied={rid: bool(solver.value(v)) for rid, (_n, v) in rm.denied.items()},
        method=method,
    )
    final = res.tier_vector()
    res.stages = [
        replace(s, best=final.get(s.tier, 0)) if s.status != "skip" else s for s in stages
    ]
    return res


def solve_lexicographic(
    inp: SolveInput,
    *,
    stage_seconds: float,
    workers: int,
    sink: EventSink | None = None,
    total_seconds: float | None = None,
    log_dir: Path | None = None,
) -> SolveResult:
    t0 = time.perf_counter()
    rm = RosterModel(inp)
    if sink:
        sink.stage(
            "model",
            "built",
            variables=len(rm.m.proto.variables),
            constraints=len(rm.m.proto.constraints),
            hard=rm.hard_counts(),
            soft={t: len(v) for t, v in rm.terms.items()},
        )
    stages: list[Stage] = []
    solver: cp_model.CpSolver | None = None
    if inp.previous is not None:  # 6.2 step 3: start from the published roster
        rm.hint_roster(dict(inp.previous))

    for rank, tier in enumerate(inp.priorities, 1):
        if not rm.terms.get(tier):
            stages.append(Stage(rank, tier, "skip", 0, 0, True, 0.0, None))
            if sink:
                sink.stage(tier, "skip", rank=rank)
            continue
        pen = rm.tier_penalty(tier)
        rm.m.minimize(pen)
        budget = stage_seconds
        if total_seconds is not None:
            budget = min(budget, total_seconds - (time.perf_counter() - t0))
        log, fh = _file_logger(log_dir, tier)
        solver = new_solver(budget, workers, log=log)
        if sink:
            sink.stage(tier, "start", rank=rank, objective=tier, seconds=round(budget, 2))
        ts = time.perf_counter()
        cb = ImprovingSolutionCallback(sink, tier, rank) if sink else None
        status = solver.solve(rm.m, cb)
        secs = time.perf_counter() - ts
        if fh:
            fh.close()
        name = solver.status_name(status)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            stages.append(Stage(rank, tier, name, 0, 0, False, secs, None))
            if sink:
                sink.stage(tier, "done", rank=rank, status=name, secs=round(secs, 3))
            kind: Literal["infeasible", "failed"] = (
                "infeasible" if status == cp_model.INFEASIBLE else "failed"
            )
            return SolveResult(kind, {}, stages, [], 0, time.perf_counter() - t0, tier)
        best = round(solver.objective_value)
        bound = round(solver.best_objective_bound)
        locked = f"{tier} <= {best}"
        rm.m.add(pen <= best).with_name(f"lock:{tier}")
        stage = Stage(
            rank, tier, name, best, bound, status == cp_model.OPTIMAL, secs, locked, stage_best=best
        )
        stages.append(stage)
        if sink:
            sink.stage(
                tier,
                "done",
                rank=rank,
                status=name,
                best=best,
                bound=bound,
                proven=stage.proven,
                locked=locked,
                secs=round(secs, 3),
            )
        rm.hint_from(solver)

    if solver is None:  # no tier has terms: still find a feasible roster
        solver = new_solver(stage_seconds, workers)
        if solver.solve(rm.m) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return SolveResult("infeasible", {}, stages, [], 0, time.perf_counter() - t0, None)
    return _finish(rm, solver, inp, stages, t0, "lexicographic")


def solve_weighted(
    inp: SolveInput, *, seconds: float, workers: int, sink: EventSink | None = None
) -> SolveResult:
    """Single objective: sum_t mult_t x penalty_t, mult_t > worst case of all lower tiers."""
    t0 = time.perf_counter()
    rm = RosterModel(inp)
    tiers = list(inp.priorities)
    ub = {t: sum(term.weight * term.ub for term in rm.terms.get(t, [])) for t in tiers}
    mult: dict[str, int] = {}
    worst_below = 0
    for t in reversed(tiers):
        mult[t] = worst_below + 1
        worst_below += mult[t] * ub[t]
    rm.m.minimize(sum(mult[t] * rm.tier_penalty(t) for t in tiers))
    solver = new_solver(seconds, workers)
    if sink:
        sink.stage("weighted", "start", multipliers=mult)
    ts = time.perf_counter()
    status = solver.solve(rm.m)
    secs = time.perf_counter() - ts
    name = solver.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        stage = Stage(1, "weighted", name, 0, 0, False, secs, None)
        kind: Literal["infeasible", "failed"] = (
            "infeasible" if status == cp_model.INFEASIBLE else "failed"
        )
        return SolveResult(
            kind, {}, [stage], [], 0, time.perf_counter() - t0, "weighted", method="weighted"
        )
    proven = status == cp_model.OPTIMAL
    stages = [
        Stage(r, t, name if rm.terms.get(t) else "skip", 0, 0, proven, secs, None)
        for r, t in enumerate(tiers, 1)
    ]
    return _finish(rm, solver, inp, stages, t0, "weighted")


def solve(
    inp: SolveInput,
    *,
    stage_seconds: float,
    workers: int,
    sink: EventSink | None = None,
    forcings: Sequence[Forcing] = (),
    total_seconds: float | None = None,
    log_dir: Path | None = None,
) -> SolveResult:
    """Engine entry point (5.6): lexicographic solve with optional extra forcings."""
    if forcings:
        inp = replace(inp, forced=(*inp.forced, *forcings))
    return solve_lexicographic(
        inp,
        stage_seconds=stage_seconds,
        workers=workers,
        sink=sink,
        total_seconds=total_seconds,
        log_dir=log_dir,
    )
