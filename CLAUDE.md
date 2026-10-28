# ShiftSense — instructions for Claude Code

Build the app specified in `docs/DESIGN.md` (the full design document). Read it before writing code.
Section 11.4 (rules for the coding agent) and section 12 (milestones M0–M13) are binding.

## Working rules
- One milestone at a time, in order. Each milestone ends with a green `make test`, exactly one commit
  (message given in section 12, e.g. `chore(m0): ...`) and a tag `m00` … `m13`.
- Stop after every milestone and wait for my review. At M2, I read `app/solver/model.py` against
  the tables in 5.2 and 5.3 before it is committed.
- Commits are authored by me; keep any Claude credit to a `Co-Authored-By` line.
- Never commit API keys. Keys live only in `.env` (git-ignored) and Railway variables; keep `.env.example` current.
- Synthetic or pseudonymized data only. Never add real names, hospitals, photos or patient data.

## LLM provider
- Default: `meta-models/muse-spark-open-1026` on Baseten, base URL `https://inference.baseten.co/v1`.
  The model always reasons; parse only the final content and tool calls.
- Use the `openai` Python SDK only as the HTTP client for that OpenAI-compatible endpoint, in a class
  named `OpenAICompatProvider`. No OpenAI (or any non-Meta) model is ever called; the README must say so.
- Meta Model API: `META_BASE_URL`, `META_MODEL`, `META_API_KEY` from env, never hard-coded.

## Fixtures (unzip `shiftsense-fixtures.zip` at the repo root)
- `data/seed/mini_ward.json`: 9-nurse, 14-day mini ward (`SEED=mini`); scenarios A–D.
- `data/seed/real_ward_a.json`: pseudonymized real October ward (`SEED=real_a`).
- `tests/fixtures/mini_expected.json`: proven golden tier vector 3/0/0/19/55, sick-call results.
- `tests/fixtures/fixed_assignment.json`: M15 stretch fixture.
- `reference/`: reference CP-SAT model (`mini_ward/mini_solver.py`), golden tests, staffing-check
  prototype (M14), `real_rosters/totals.py` (port to `app/planner/totals.py` in M1) and `plan_check.py`.
  These are specs to diff `app/solver` against, not app code; run with `pytest reference` (~1 min).
  Never edit expected values to make a test pass; flag doubts in the commit message.

## READMEs
A top-level README covering the problem (a charge nurse spends about 2 hours per roster by hand),
how Meta models are used, architecture, setup and the demo; plus a short README in each top-level folder.
