"""Writes a run's evidence from a SolveResult (design doc 4.4, 11.4 rule 2).

Only the planner calls this, and only with a `SolveResult` from the solver: no model output is ever
written into `assignment`, `stage`, `cost` or `request_outcome`.
"""

import sqlite3
from datetime import date, timedelta

from app.domain.models import Request, RequestKind, RequestStatus, Ward
from app.solver.input import SolveInput
from app.solver.solve import SolveResult
from app.store import repo

SOFT_RULE = {
    RequestKind.DAY_OFF: "PREF_OFF",
    RequestKind.SHIFT_WANTED: "PREF_SHIFT",
    RequestKind.SHIFT_AVOID: "PREF_AVOID",
}


def iso_day(start: date, d: int | None) -> str | None:
    return None if d is None else (start + timedelta(days=d)).isoformat()


def write_evidence(
    conn: sqlite3.Connection,
    run_id: str,
    ward: Ward,
    inp: SolveInput,
    res: SolveResult,
    requests: list[Request],
    *,
    causes: dict[tuple[str, int], str] | None = None,
) -> None:
    start = ward.horizon_start
    repo.insert_stages(
        conn,
        run_id,
        [
            (s.rank, s.tier, s.status, s.best, s.bound, int(s.proven), round(s.secs, 3), s.locked)
            for s in res.stages
        ],
    )

    roster = {n.id: [res.roster.get((n.id, d)) for d in range(inp.num_days)] for n in inp.nurses}
    previous = None
    if inp.previous is not None:
        previous = {
            n.id: [inp.previous.get((n.id, d)) for d in range(inp.num_days)] for n in inp.nurses
        }
    repo.insert_assignments(conn, run_id, roster, start, previous=previous, causes=causes)

    repo.insert_costs(
        conn,
        run_id,
        [
            (t.nurse, iso_day(start, t.day), t.tier, t.rule, t.amount, t.weight, t.ref, t.why)
            for t in res.term_values
        ],
    )

    # request outcomes: hard requests are always kept; soft ones are denied when any requested
    # cell was not granted, with the rule codes and dates of the denied cells
    denied_cells: dict[str, list[tuple[str, str]]] = {}
    for t in res.term_values:
        if t.tier == "requests" and t.rule.startswith("PREF_") and t.ref:
            denied_cells.setdefault(t.ref, []).append((t.rule, iso_day(start, t.day) or ""))
    rows: list[tuple[str, str, str | None, str | None]] = []
    active = [r for r in requests if r.status is RequestStatus.ACTIVE]
    for r in active:
        if r.kind in SOFT_RULE and r.id in res.denied:
            cells = denied_cells.get(r.id, [])
            if res.denied[r.id]:
                rows.append(
                    (
                        r.id,
                        "DENIED",
                        SOFT_RULE[r.kind],
                        "denied on " + ", ".join(d for _rule, d in cells),
                    )
                )
            else:
                rows.append((r.id, "GRANTED", None, None))
        elif r.hard:
            rows.append((r.id, "GRANTED", "HARD", "hard constraint"))
    repo.insert_request_outcomes(conn, run_id, rows)

    made: dict[str, int] = {}
    lost: dict[str, int] = {}
    for r in active:
        if r.kind in SOFT_RULE and r.id in res.denied:
            made[r.nurse_id] = made.get(r.nurse_id, 0) + 1
            lost[r.nurse_id] = lost.get(r.nurse_id, 0) + int(res.denied[r.id])
    fair = []
    for n in inp.nurses:
        row = roster[n.id]
        fair.append(
            (
                n.id,
                sum(s == "N" for s in row),
                sum(row[d] is not None for d in inp.weekend_days),
                sum(s is not None for s in row),
                made.get(n.id, 0),
                lost.get(n.id, 0),
            )
        )
    repo.insert_fairness(conn, run_id, fair)
