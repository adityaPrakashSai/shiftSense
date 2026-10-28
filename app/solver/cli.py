"""Solve a seed file from the command line and print the roster grid (milestone M2).

    uv run python -m app.solver.cli data/seed/ward.json [--seconds 5] [--workers 8]

The one module in app/solver allowed to import outside the package: it reads a seed file through
`app.domain.seeds` and `app.planner.build_input` (both pure, no database or network).
"""

from __future__ import annotations

import argparse
import sys

from app.domain.seeds import load_seed_file
from app.planner.build_input import build_input
from app.solver.input import SHIFTS, SolveInput
from app.solver.solve import ListSink, RosterGrid, SolveResult, solve
from app.solver.validate import display_codes, violations


def render(inp: SolveInput, roster: RosterGrid, names: dict[str, str]) -> str:
    width = max(len(f"{n.id} {names.get(n.id, '')}") for n in inp.nurses)
    head = " " * width + " " + " ".join(f"{d:>3}" for d in range(inp.num_days))
    weekend = (
        " " * width
        + " "
        + " ".join(" we" if d in inp.weekend_days else "   " for d in range(inp.num_days))
    )
    lines = [head, weekend]
    for spec in inp.nurses:
        label = f"{spec.id} {names.get(spec.id, '')}".ljust(width)
        cells = " ".join(f"{c:>3}" for c in display_codes(inp, roster, spec))
        tags = " standby" if spec.standby else ""
        nights = sum(roster.get((spec.id, d)) == "N" for d in range(inp.num_days))
        shifts = sum(roster.get((spec.id, d)) is not None for d in range(inp.num_days))
        lines.append(f"{label} {cells}   {shifts:>2} shifts {nights} N{tags}")
    for s in SHIFTS:
        got = [sum(roster.get((n.id, d)) == s for n in inp.nurses) for d in range(inp.num_days)]
        tgt = [inp.demand[(d, s)].target_total for d in range(inp.num_days)]
        cells = " ".join(f"{g:>3}" if g >= t else f"{g:>2}!" for g, t in zip(got, tgt, strict=True))
        lines.append(f"{('total ' + s).ljust(width)} {cells}")
    return "\n".join(lines)


def summary(res: SolveResult) -> str:
    parts = []
    for st in res.stages:
        if st.status == "skip":
            continue
        flag = "proven" if st.proven else f"best found (bound {st.bound})"
        parts.append(f"{st.tier} {st.best} [{flag}, {st.secs:.2f} s]")
    return "; ".join(parts) or "no objective"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Solve a ShiftSense seed file with CP-SAT.")
    ap.add_argument("seed", help="path to a seed JSON (data/seed/ward.json, mini_ward.json, ...)")
    ap.add_argument("--seconds", type=float, default=5.0, help="time limit per stage")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    seed = load_seed_file(args.seed)
    inp = build_input(seed.ward, seed.requests, seed.history)
    res = solve(inp, stage_seconds=args.seconds, workers=args.workers, sink=ListSink())
    print(f"{seed.ward.name}: {len(inp.nurses)} nurses x {inp.num_days} days")
    if res.status != "solved":
        print(f"{res.status} at tier {res.failed_tier}")
        return 1
    print(render(inp, res.roster, {n.id: n.name for n in seed.ward.nurses}))
    bad = violations(inp, res.roster)
    result = bad if bad else "0 violations"
    print(f"\n{summary(res)}; total {res.secs:.2f} s; validator: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
