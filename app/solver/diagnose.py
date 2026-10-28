"""Infeasibility core (design doc 5.5): which named hard constraints cannot hold together.

Rebuilds the model in guarded mode (every hard constraint enforced only if its guard literal is
true), clears the objective, adds every guard as an assumption and reads
`sufficient_assumptions_for_infeasibility()`. The names returned (for example
`["min_cover:d12:N", "unavailable:N03:d12"]`) form the core. M9 builds conflict options on top.
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from app.solver.input import SolveInput
from app.solver.model import RosterModel


def infeasible_core(inp: SolveInput, seconds: float = 5.0) -> list[str] | None:
    """The core names, [] when infeasible but no core was found in time, None when feasible."""
    rm = RosterModel(inp, guarded=True)
    names = list(rm.guards)
    rm.m.add_assumptions([rm.guards[n] for n in names])
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = seconds
    status = solver.solve(rm.m)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    if status != cp_model.INFEASIBLE:
        return []
    by_index = {rm.guards[n].index: n for n in names}
    return sorted(by_index[i] for i in solver.sufficient_assumptions_for_infeasibility())


def groups(core: list[str], skip: frozenset[str] = frozenset({"forced"})) -> list[str]:
    """Distinct constraint groups in a core, in order ("night_qualified", "min_cover", ...)."""
    out: list[str] = []
    for name in core:
        g = name.split(":", 1)[0]
        if g not in skip and g not in out:
            out.append(g)
    return out
