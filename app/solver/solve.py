"""Lexicographic solve over the priority tiers (design doc 5.4, 5.6).

For each tier in `SolveInput.priorities`: minimise its penalty subject to the hard rules and the
locks of every earlier tier, then lock it at the best value found (`lock:{tier}`). A stage that
hits the time limit is locked at its best value and marked `proven = False`. In M2 only the
coverage tier has terms; the others are skipped.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from ortools.sat.python import cp_model

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
    best: int
    bound: int
    proven: bool
    secs: float
    locked: str | None  # "coverage <= 3"


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

    def tier_vector(self) -> dict[str, int]:
        return {s.tier: s.best for s in self.stages}


def new_solver(seconds: float, workers: int, seed: int = 42) -> cp_model.CpSolver:
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = seconds
    s.parameters.num_workers = workers
    s.parameters.random_seed = seed
    return s


def changed_cells(inp: SolveInput, roster: RosterGrid) -> int:
    """Cells that differ from the published roster (refined into causes in M7, 6.3)."""
    if inp.previous is None:
        return 0
    return sum(1 for k, s in roster.items() if inp.previous.get(k) != s)


def solve(
    inp: SolveInput,
    *,
    stage_seconds: float,
    workers: int,
    sink: EventSink | None = None,
    forcings: Sequence[Forcing] = (),
) -> SolveResult:
    t0 = time.perf_counter()
    if forcings:
        inp = replace(inp, forced=(*inp.forced, *forcings))
    rm = RosterModel(inp)
    if sink:
        sink.stage(
            "model", "built", hard=rm.hard_counts(), soft={t: len(v) for t, v in rm.terms.items()}
        )
    stages: list[Stage] = []
    solver: cp_model.CpSolver | None = None

    for rank, tier in enumerate(inp.priorities, 1):
        if not rm.terms.get(tier):
            stages.append(Stage(rank, tier, "skip", 0, 0, True, 0.0, None))
            if sink:
                sink.stage(tier, "skip")
            continue
        pen = rm.tier_penalty(tier)
        rm.m.minimize(pen)
        solver = new_solver(stage_seconds, workers)
        if sink:
            sink.stage(tier, "start", objective=tier)
        ts = time.perf_counter()
        status = solver.solve(rm.m)
        secs = time.perf_counter() - ts
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            name = solver.status_name(status)
            stages.append(Stage(rank, tier, name, 0, 0, False, secs, None))
            if sink:
                sink.stage(tier, "done", status=name, secs=secs)
            kind: Literal["infeasible", "failed"] = (
                "infeasible" if status == cp_model.INFEASIBLE else "failed"
            )
            return SolveResult(kind, {}, stages, [], 0, time.perf_counter() - t0, tier)
        best = round(solver.objective_value)
        bound = round(solver.best_objective_bound)
        locked = f"{tier} <= {best}"
        rm.m.add(pen <= best).with_name(f"lock:{tier}")
        stage = Stage(
            rank,
            tier,
            solver.status_name(status),
            best,
            bound,
            status == cp_model.OPTIMAL,
            secs,
            locked,
        )
        stages.append(stage)
        if sink:
            sink.stage(
                tier,
                "done",
                status=stage.status,
                best=best,
                bound=bound,
                proven=stage.proven,
                locked=locked,
                secs=secs,
            )
        rm.hint_from(solver)

    if solver is None:  # no tier has terms: still find a feasible roster
        solver = new_solver(stage_seconds, workers)
        status = solver.solve(rm.m)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return SolveResult("infeasible", {}, stages, [], 0, time.perf_counter() - t0, None)

    roster = rm.roster(solver)
    bad = violations(inp, roster)
    if bad:  # the model and the independent validator disagree: a solver bug, never ship it
        raise RuntimeError(f"solver roster breaks hard rules: {bad[:10]}")
    terms = [
        TermValue(t.tier, t.rule, t.nurse, t.day, t.ref, amount, t.weight, t.why)
        for tier_terms in rm.terms.values()
        for t in tier_terms
        if (amount := int(solver.value(t.expr))) > 0
    ]
    return SolveResult(
        "solved", roster, stages, terms, changed_cells(inp, roster), time.perf_counter() - t0
    )
