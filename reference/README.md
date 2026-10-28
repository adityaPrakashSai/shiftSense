# reference/ — pre-build prototypes

Written before the app (26–27 Oct 2026) to pin down the solver spec and the golden numbers. These are **specs to diff `app/solver` against, not app code**. They are excluded from lint and from `make test`. Run them with `make reference` (about 1 minute; needs `ortools`).

| Folder | What it is |
|---|---|
| `mini_ward/` | `mini_solver.py`: reference CP-SAT model (design doc 5.2–6.4) plus an independent pure-Python evaluator; `test_mini_ward.py`: golden tests; `gen_instance.py`: generator for `data/seed/mini_ward.json`. Full write-up in `mini_ward/README.md` |
| `mini_ward/staffing/` | Staffing-check prototype (minimum headcount, standby need) and its results, ported in M14 |
| `mini_ward/fixed_assignment/` | Hospital B style (one shift + one fixed weekday off per nurse) reference solve, for M15 |
| `real_rosters/` | `totals.py` (daily totals and printed-row checksum, ported to `app/planner/totals.py` in M1), `plan_check.py` (hard rules over the printed October plan: 0 breaks with max 8 consecutive days, 27 quick returns), `test_real_ward_a.py` |

The fixtures they read live in one place only: `data/seed/` and `tests/fixtures/`.
