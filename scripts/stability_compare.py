"""Changed cells with and without the stability tier (design doc 6, M7 write-up number).

    uv run python -m scripts.stability_compare

Re-solves the mini-ward scenarios A-D from the reference baseline, and a Morning sick call on the
default ward (Tue 3 Nov, 06:05) from a freshly solved baseline, once with the default priorities
and once with the stability tier removed. Same time limits for both.
"""

import tempfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.models import Request, RequestKind, RequestStatus
from app.planner.runs import start_resolve
from app.solver.changes import change_set, count_changed
from app.solver.input import SolveInput
from app.solver.solve import solve
from app.store import repo
from app.store.db import open_db
from app.store.seed import seed_db
from tests.solver.test_resolve import SCENARIOS, scenario_input

SECS = 5.0


def compare(inp: SolveInput, added: tuple[tuple[int, str], ...] = ()) -> tuple[int, int, float]:
    a = solve(inp, stage_seconds=SECS, workers=8)
    no_tier = tuple(t for t in inp.priorities if t != "stability")
    b = solve(replace(inp, priorities=no_tier), stage_seconds=SECS, workers=8)
    return (
        count_changed(change_set(inp, a.roster, added)),
        count_changed(change_set(inp, b.roster, added)),
        a.secs,
    )


def main() -> None:
    print(f"{'case':34} {'with':>5} {'without':>8} {'secs':>6}")
    for sid in sorted(SCENARIOS):
        inp, added = scenario_input(sid)
        w, wo, s = compare(inp, added)
        print(f"{'mini ' + sid:34} {w:>5} {wo:>8} {s:>6.2f}")
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "cmp.db")
        seed_db(conn, "default")
        pub = repo.get_published_run(conn, "ward-7")
        assert pub is not None
        grid = repo.get_roster(conn, pub["run_id"])
        nurse = next(n for n in ["N07", *sorted(grid)] if grid[n][8] == "M")
        now = datetime(2026, 11, 3, 6, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
        tue = date(2026, 11, 3)
        r = Request(
            id="R-0900",
            nurse_id=nurse,
            kind=RequestKind.UNAVAILABLE,
            dates=[tue],
            shifts=["M"],
            hard=True,
            status=RequestStatus.ACTIVE,
            created_at=now,
        )
        repo.insert_request(conn, "ward-7", r)
        plan = start_resolve(conn, trigger="compare", affected=[tue], now=now)
        w, wo, s = compare(plan.inp)
        print(f"{'default ward sick call (' + nurse + ', 3 Nov M)':34} {w:>5} {wo:>8} {s:>6.2f}")
        conn.close()


if __name__ == "__main__":
    main()
