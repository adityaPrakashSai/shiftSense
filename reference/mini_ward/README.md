# Mini ward: a small, hard, hand-checkable rostering instance

This instance packs the hard parts of the standard nurse-rostering benchmarks (INRC-I, INRC-II,
Curtois & Qu, NSPLib) into **7 regular + 2 standby nurses × 14 days × 3 shifts**. The only thing
it leaves out is scale. It uses the design doc's data model, so the app uses it directly:

| File | What it is | Where it goes in the repo |
|---|---|---|
| `mini_ward.json` | Instance: ward, nurses, demand, 9 requests, 2-period history, rules and weights, 2 sick-call scenarios, 1 demand-change scenario and 1 leave-extension scenario | `data/seed/mini_ward.json` (M1). `make seed SEED=mini` loads it instead of the 24-nurse ward |
| `expected.json` | Golden results: published baseline roster, proven tier vectors, every soft term, trade-off runs, sick-call results and candidate ranking | `tests/fixtures/mini_expected.json` |
| `mini_solver.py` | Reference CP-SAT model (doc sections 5.2–6.4) plus an independent pure-Python evaluator. Every solve is cross-checked against the evaluator | `scripts/mini_reference.py`. It is a spec to diff `app/solver` against, not the app's solver |
| `test_mini_ward.py` | 10 golden tests | `tests/solver/test_mini_ward.py` (M2/M3), and the sick-call and demand-change tests at M7 |

Run it: `pip install ortools==9.15.6755 pytest`, then
`python mini_solver.py mini_ward.json --seconds 300 --variant-seconds 90 --workers 8 --write expected.json`
(about 5 minutes on 4 cores), then `pytest -q test_mini_ward.py` (about 1 minute).

## The ward

Period: Mon 26 Oct to Sun 8 Nov 2026 (day 0 to day 13). Weekends are days 5–6 and 12–13. Shifts are M 07–15, E 15–23 and N 23–07.
Demand target / hard floor: weekday M 3/2, E 2/1, N 1/1, with **Monday and Tuesday mornings at 4** (27 Oct interview); weekend M 2/1, E 2/1, N 1/1. That is 84 target slots.
Contract: 5.5–6 shifts a week, scaled to 11–12 per period, so supply is 77–84: the ward can just meet demand with everyone at maximum. **At most 6 nights a month** (27 Oct interview), scaled to ceil(6 × 14 / 30) = 3 per period, on top of at most 3 consecutive nights. Standby nurses work at most 4 shifts and are never on the published roster.

| Id | Nurse | Role in the instance |
|---|---|---|
| N01 | Anjali | Heaviest night history (10). Asks for Sat 31 Oct off |
| N02 | Priya | Finished last period with **6 consecutive days**. Wants nights Sat–Mon (w3) |
| N03 | Meena | Asks for Sat off. Wants mornings in week 2. **Calls in sick Tue 3 Nov** (scenarios A and B) |
| N04 | Kavya | Approved leave Wed–Thu (hard; counts toward the contract minimum) |
| N05 | Ritu | Finished last period with **3 consecutive nights**. Wants the 2nd weekend off |
| N06 | Fatima | **Night break**: "agle do hafte night duty nahi" (avoid N, all 14 days, w5) |
| N07 | Deepa | Not night-qualified. Asks for Sat off |
| S01 | Arun | Standby |
| S02 | Rekha | Standby, unavailable Tue 3 Nov (exam) |

## Which benchmark challenge each element represents

| Element in `mini_ward.json` | Benchmark origin | Hard / soft (weight, tier) |
|---|---|---|
| Per-day, per-shift demand with a floor and a target; weekday ≠ weekend; busier Mon/Tue mornings | NSPLib coverage matrix; INRC-II minimum (hard) + optimal (soft) coverage; Curtois cover under-penalties | floor hard; `COVER_SHORT` 1, coverage |
| Never overstaff (`max_cover = target`) | Curtois cover over-penalties (made hard here, as in the doc) | hard |
| N after nothing but N or off (`rest_after_night`) | INRC-II / Curtois forbidden shift successions | hard |
| E then M next day (8 h rest) | INRC-I unwanted patterns; Curtois rotation rules | `QUICK_RETURN` 3, requests |
| Max 6 consecutive days, max 3 consecutive nights | INRC-I/II max consecutive working days / same shift | hard |
| Max 6 nights a month (3 per 14 days) | INRC-II / Curtois max assignments per shift type | hard |
| Night blocks: at most 3 nights in a row (hard), at least 2 (no single nights) | INRC-II min/max consecutive same shift; Hospital A roster photos | max hard; `SINGLE_NIGHT` 3, requests |
| Min 2 consecutive working days | INRC-II min consecutive assignments | `ISOLATED_DAY` 1, requests |
| Exactly one N/O day after a night block (no second recovery day) | Hospital A roster photos (27 Oct); replaces INRC-I's "two free days after nights" (`NIGHT_RECOVERY`, removed) | hard, via `rest_after_night` |
| Complete weekends | INRC-I/II complete weekends | `SPLIT_WEEKEND` 1, requests |
| Min/max total shifts, scaled from a weekly contract; leave credited | INRC-II total assignments; Curtois min/max minutes | max hard; `CONTRACT_MIN` 1, contract |
| Day-off, shift-on and shift-off requests with different weights | INRC-I/II and Curtois shift on/off requests; NSPLib preference matrix | `PREF_OFF` 2w, `PREF_SHIFT` w, `PREF_AVOID` w, requests |
| Night break as a weighted 14-day avoid request | Ward interview (not in any benchmark) | `PREF_AVOID` 5 |
| Not night-qualified nurse | INRC skill/eligibility, reduced to one flag because the ward has no skill mix | hard |
| History: consecutive days/nights, the last 3 days' shifts, running totals of nights, evenings, weekends and denials | **INRC-II multi-stage border data** | used by hard rules and by fairness |
| Fairness of nights, weekends, mornings, evenings and denials over running totals | INRC-II total weekends/assignments across stages (as spreads); the photos showed 3–13 mornings and 1–8 weekends per nurse | `FAIR_*` (nights, weekends, mornings, evenings 2; denials 1), fairness |
| Mid-period sick call or demand increase, frozen past, minimal-change re-solve, standby pool | Rescheduling / disruption-management literature (no standard benchmark has it) | `CHANGED` 2, `STANDBY_CALLED` 1, stability |

Competing rules are built in on purpose:
- Three day-off requests for a 5-slot Saturday compete with coverage.
- Fatima's night break competes with night fairness, because her low night history pins the minimum.
- Priya's wanted nights compete with the night cap and the night block rules.
- Leave and carry-in compete with the contract minimum.
- The night cap makes nights scarce: 14 nights must fit into 5 night nurses × 3, so Fatima's night break leaves exactly one spare night.

## Hand checks (all asserted in `test_hand_checks`)

1. **Day 0 is two slots short, and that is forced.** Priya's carry-in of 6 days and Ritu's 3 nights (rest after night, and a 4th night would break max 3) put both off on Monday. That leaves 5 regular nurses for 7 Monday slots. (The optimum has one more short slot, Mon 2 Nov evening, for 3 in total.) The off day after a night block is labelled N/O in `expected.json → baseline.display`; Ritu's day 0 is N/O because of her carry-in nights.
2. **At least one Saturday request must be denied.** Saturday has 5 slots and 7 nurses, and 3 of them ask for the day off. The optimum denies two (Anjali and Deepa) and grants Meena's, who has the most past denials (2).
3. **Nights fit exactly.** 14 nights, 3 per nurse at most, Deepa not qualified, Fatima on a night break: Anjali, Priya, Meena and Ritu do 3 each and Kavya 2.
4. **No standby nurse is on the published roster.**
5. **The model's tier values equal the evaluator's**, recomputed term by term from the grid. Every soft term with its nurse, day, request and weight is listed in `expected.json → baseline.terms`.

## Baseline result (every tier proven optimal: 3 / 0 / 0 / 19 / 55)

Regenerated 27 Oct 2026 after the real-roster night rules (one N/O, no `NIGHT_RECOVERY`, `SINGLE_NIGHT` weight 3) and the new `FAIR_MORNINGS` term (history now carries `mornings`).

```
      Mo Tu We Th Fr Sa Su Mo Tu We Th Fr Sa Su
N01    E  E  E  E  .  M  E  N  N  N  .  M  M  M   Anjali
N02    .  M  M  M  M  N  N  .  M  M  M  E  E  E   Priya
N03    E  E  N  N  N  .  M  M  M  M  M  M  .  M   Meena
N04    N  N  .  .  M  M  E  E  E  .  M  M  E  N   Kavya
N05    .  M  E  E  E  E  .  M  E  E  N  N  N  .   Ritu
N06    M  M  M  M  E  .  M  M  M  E  E  E  .  .   Fatima
N07    M  M  M  M  M  E  .  M  M  M  E  .  M  E   Deepa
```
What the result does with each request:
- Fatima's night break is fully granted, and so are Meena's week-2 mornings.
- Priya gets 2 of her 3 wanted nights (Monday is denied).
- Anjali's and Deepa's Saturdays off are denied.
- Ritu gets one day of her second weekend off.
- Every night block is 2 or 3 nights and is followed by one off day; no quick returns or single nights inside the period. Kavya's single night on the last day is not penalised because the block continues into the next period.

Requests tier 19 = Saturday denials 4 + 4, Priya's Monday night 3, Ritu's weekend 2, and 6 split weekends. Fairness 55 = nights 20, mornings 22, weekends 8, evenings 4, denials 1; the history offsets dominate the morning and night spreads.

## Trade-offs (tier vectors scored under the default rules; every run proven)

| Run | coverage | stab. | contract | requests | fairness | What it shows |
|---|---|---|---|---|---|---|
| **baseline** | 3 | 0 | 0 | 19 | 55 | |
| fairness above requests | 3 | 0 | 0 | 39 | 43 | Fairness improves by 12, requests get worse by 20 |
| requests above coverage | 10 | 0 | 4 | 0 | 56 | Every wish granted, 10 slots short: why coverage comes first |
| solved without history | 1 | 0 | 0 | 20 | 58 | Looks better, but **breaks 5 hard rules** once real history is applied (Ritu's rest after night, four 7th days in a row) |
| core doc rules only | 3 | 0 | 0 | 57 | 51 | A roster that ignores sequence quality scores 57 on sequence and request penalties |
| dominance-weighted single objective | 3 | 0 | 0 | 19 | 55 | Same vector as lexicographic, a cross-check |
| sequence rules in the contract tier | 3 | 0 | 0 | 19 | 55 | The doc's literal reading. Same result on this version of the instance; see finding 1 |

## Disruptions (re-solve with day 0–7 frozen, standby allowed)

- **A. Meena out for Tue M.** The re-solve takes 0.12 s. Arun (standby) takes the shift and nobody else's duty changes (1 changed cell).
  - Ranked alternatives: Ritu moving E→M (+2 stability, +2 fairness) and Kavya moving E→M (+2 stability, +3 requests, +2 fairness).
  - Excluded: Anjali (rest after Monday's night), Rekha (unavailable).
- **B. Meena out Tue–Thu.** The re-solve takes 0.16 s. Arun covers Tue and Rekha covers Wed and Thu, so only standby nurses change (3 cells).
- **C. Thursday evening needs 3 instead of 2** (`change_demand`, the doc's 6.5b flow). The re-solve takes 0.17 s and one standby nurse is added (1 changed cell).
  - Arun and Rekha **tie** on every tier, which exercises the doc's TIE rule.
  - Then come Priya and Kavya moving from M, Ritu from N, and Meena from M (2 changed cells each).
  - Excluded: Anjali (rest after night, 7th consecutive day, over max shifts).
- **D. Ritu's leave is extended over Thu–Sun of week 2, across her night block** (the October roster case, doc 6.5c). The re-solve takes 0.16 s. Rekha (standby) takes the whole 3-night block, so every night stays at target, the cover is a legal block followed by N/O, and no regular nurse's duty moves (3 changed cells). The hand edit on the real October sheet left two nights short instead.

## Solve-time findings

- **Full solve:** with 8 workers on 4 cores, all five tiers prove in 11 s (the whole regeneration, with every trade-off run and scenario, takes under 2 minutes).
  - With 10 s per stage at 4 workers it reaches the optimal values (19 / 55) but cannot prove the last two tiers, so the UI shows "best found". At 2 workers it stops at 24 / 58.
  - Removing `NIGHT_RECOVERY` (27 Oct) made the instance easier again (22 s → 11 s). The first version's hard part was `NIGHT_RECOVERY` together with `SPLIT_WEEKEND` in a tier above requests.
- **Re-solves are easy** (under 0.2 s, all proven), because the frozen past plus the stability lock leave little freedom.
- **The fairness spread with history offsets is weak.** Fatima's night break pins the night minimum at her history (3), so `FAIR_NIGHTS` collapses to "minimise the maximum". `FAIR_MORNINGS` has the same shape: Deepa (no nights) has the most mornings in her history.

## Changes this implies for the design doc

1. **Put the sequence-quality rules in a weighted tier with the requests, not a lexicographic tier above them.** Weighted together, the combined sequence-and-request cost is minimised directly, so it is never worse than the lexicographic split. In the first version of this instance (before the 27 Oct rules) the split cost 32 against 17 and its tier would not prove optimal. On the current version both give 27. Suggested name: "wellbeing & requests".
2. **Add the sequence rules** to doc 5.3 as soft terms: `QUICK_RETURN`, `SINGLE_NIGHT`, `ISOLATED_DAY`, `SPLIT_WEEKEND`. (`NIGHT_RECOVERY` was in this list until 27 Oct; the real roster photos showed one N/O day after each block is the rule, which `rest_after_night` already enforces, so it was removed.)
3. **NurseHistory needs the last 3 days' shift codes**, not just "last day was a night". Quick return, night recovery and the isolated-day rule all reach across the period border.
4. **Widen the sick-call candidate filter (6.4) to nurses moved from another shift that day.** In this ward, 6 of 7 regular nurses work on a weekday, so "nurses not working that day" leaves only the standby nurses and one excluded nurse.
5. **Credit leave and sick days toward `CONTRACT_MIN`.** Otherwise a sick call creates a contract shortfall that the re-solve then tries to make up.
6. **Fairness spread:** leave out nurses whose night break is active (and non-night nurses, as now), or score deviation from a fair share. With history offsets, max − min degenerates and its LP bound is weak.
7. **Solve budgets:** expect "best found" on the requests and fairness tiers within 10 s on 2 vCPU, even at 9 nurses. Assert tier properties in tests, not optimal values. Store proven golden values from an offline run, as here.
8. **Milestones:** M1 seeds `mini_ward.json` next to the 24-nurse ward. M2 uses it for the hard-rule tests (the day-0 carry-in checks bite). M3 checks tier values against `expected.json`. M7 runs the sick-call test on scenario A, the demand-change test on scenario C and the leave-extension test on scenario D. The mini ward is also the fast demo reset.
