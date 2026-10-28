"""The default seed solves fast and clean; real_a's printed plan is checked by the validator
(a write-up number in docs/decisions.md, not a test failure)."""

import time
from dataclasses import replace

from app.domain.seeds import load_seed
from app.planner.build_input import build_input
from app.solver.solve import solve
from app.solver.validate import violations


def test_default_seed_solves_under_10s_with_no_violations() -> None:
    s = load_seed("default")
    inp = build_input(s.ward, s.requests, s.history)
    t0 = time.perf_counter()
    res = solve(inp, stage_seconds=5, workers=8)
    assert time.perf_counter() - t0 < 10
    assert res.status == "solved" and violations(inp, res.roster) == []
    assert res.stages[0].tier == "coverage" and res.stages[0].proven


def test_real_a_solve_keeps_ans_on_general_duty() -> None:
    s = load_seed("real_a")
    inp = build_input(s.ward, s.requests, s.history)
    res = solve(inp, stage_seconds=5, workers=8)
    assert res.status == "solved" and violations(inp, res.roster) == []
    g_days = {f.day for f in inp.forced if f.nurse == "ANS"}
    assert {d for d in range(31) if res.roster[("ANS", d)] == "G"} == g_days
    assert all(res.roster[("ANS", d)] in ("G", None) for d in range(31))


def _plan(key: str) -> dict[tuple[str, int], str | None]:
    raw = load_seed("real_a").runs[key]["roster"]
    return {(n, d): c for n, row in raw.items() for d, c in enumerate(row)}


def streaks(
    roster: dict[tuple[str, int], str | None], nurse: str, days: int, carry: int = 0
) -> list[int]:
    """Lengths of working streaks; the first one includes the carry-in run from last period."""
    out, run = [], carry
    for d in range(days):
        run = run + 1 if roster.get((nurse, d)) else 0
        if run and (d == days - 1 or not roster.get((nurse, d + 1))):
            out.append(run)
    return out


def test_real_a_printed_plan_under_the_validator() -> None:
    s = load_seed("real_a")
    inp = build_input(s.ward, s.requests, s.history)
    planned, updated = _plan("planned"), _plan("updated")
    assert violations(inp, planned) == []  # legal with real_a's limit of 8 days in a row (Q16)
    # the hand edit (leave extension) left nights 13-14 Oct at 2, which is under the floor of 3
    assert violations(inp, updated) == ["min_cover:d12:N", "min_cover:d13:N"]
    # at the synthetic ward's limit of 6, the printed plan has 8 streaks of 7-8 working days
    strict = replace(inp, max_consecutive_days=6)
    runs = {
        n.id: streaks(planned, n.id, 31, inp.history[n.id].consecutive_days) for n in inp.nurses
    }
    long_runs = [r for rs in runs.values() for r in rs if r > 6]
    assert len(long_runs) == 8 and set(long_runs) <= {7, 8}
    assert {v.split(":")[1] for v in violations(strict, planned)} == {
        n for n, rs in runs.items() if any(r > 6 for r in rs)
    }
