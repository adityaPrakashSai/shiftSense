# Staffing check prototype (mini ward)

`python staffing_check.py ../mini_ward.json 10` (10 s per stage, 4 workers, about 45 s for all six runs).
It reuses `mini_solver.RosterModel` (hard rules, approved leave, coverage) and adds `used[n]`, with `works[n,d] <= used[n]`.
The solve is lexicographic:

1. coverage shortfall against target
2. nurses used
3. standby shifts
4. balance: 2·spread(total shifts, leave credited) + 2·spread(nights) + 1·spread(evenings) + 2·spread(weekend shifts), counted over the nurses in use for this period only

Requests, sequence rules and history-based fairness are ignored, so every number is a lower bound for a publishable roster.
For example, Fatima's night break is not honoured here.
Hypothetical hires `H1..Hk` are night-qualified, on the same contract (11–12 shifts) and start with a clean history.

Rerun on 27 Oct 2026 against the updated `mini_ward.json` (Mon/Tue mornings +1, so 84 target slots; max 3 nights per 14 days). Log: `run_2026-10-27.log`.

| Run | Shortfall | Nurses | Standby | Balance | Notes |
|---|---|---|---|---|---|
| A. Today's 7, no standby | 3 (Mon 26 Oct and Mon 2 Nov mornings) | 7 | 0 | 8 (proven) | The extra Monday mornings are what the 7 cannot cover |
| B. Today's 7 + hires | 0 | 9 (+2 hires) | 0 | 7 (best found) | Two hires now needed to close the Monday gaps. Standby is the cheaper fix |
| C. Minimum from scratch | 0 | 7 | 0 | 6 (best found) | ceil(84/12) = 7 is tight. The solver prefers clean-history hires over N02, N04 and N05 |
| D. Hard floor only | 0 | 5 | 0 | 2 (best found) | ceil(52/12) = 5. 29 target slots go unfilled (5 nurses × 11 shifts = 55 of 84) |
| E. Kavya out all period | 4 | 6 | 8 | 4 (proven) | Was fully covered before the demand change; now 4 short even with both standby nurses at their cap of 4 |
| F. Kavya + Deepa out | 16 | 5 | 8 | 0 (proven) | The ward cannot cover the target. 16 slots short even with all standby used |

Full per-nurse M/E/N/weekend load is in `staffing_results.json`.
The shortfall and headcount stages prove in well under the 10 s limit.
The balance stage hits the limit in A–D, because the used/unused symmetry among the hires weakens the bound.
