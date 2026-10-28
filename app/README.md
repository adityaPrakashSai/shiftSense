# app/

Python backend: one FastAPI process (`main.py`) that serves the API, the WebSocket and the built
web app, and runs CP-SAT in-process. Configuration is in `config.py` (design doc 3.5).

Packages arrive milestone by milestone (design doc 11.1): `domain/`, `store/`, `solver/` (pure
Python + `ortools`, no web/LLM/database imports), `planner/`, `evidence/`, `explain/`, `intake/`,
`llm/`, `log/`, `api/`, `agent/`, `tools/`.

`solver/` today: `input.py` (SolveInput), `model.py` (CP-SAT hard rules and the five soft
tiers), `solve.py` (lexicographic and dominance-weighted solves), `validate.py` (independent
validator), `rules.py` (`rule_check`) and `cli.py` (`python -m app.solver.cli data/seed/ward.json`).
`planner/runs.py` runs and publishes; `evidence/writer.py` writes stage, assignment, cost,
request-outcome and fairness rows; `log/decision_log.py` is the event stream; `tools/replay.py`
re-solves a stored run.

`llm/`: `gateway.py` (cache, modes, spend cap, `llm_call` logging), `providers.py`
(`OpenAICompatProvider` for Baseten / Meta Model API / Modal), `jsonproto.py` (JSON tool protocol
fallback), `cache.py`, `mock.py`, `prices.toml`, `prompts/` and `smoke.py`.

`intake/`: `pipeline.py` (message -> prompt -> ConstraintEdit -> repair -> semantic checks ->
pending requests and a templated reply), `semantic.py` (step 5 checks), `resolve_names.py`,
`dates.py` (ward time, `DEMO_NOW`, 35-day calendar) and `templates/{en,hi}.toml`.
`planner/requests.py` confirms, rejects and approves requests; `domain/edits.py` is the 7.2 edit
language.

Re-solve and sick calls (M7): `planner/runs.py` `start_resolve` / `run_resolve` (frozen point,
published roster as `previous` and hint, standby allowed), `solver/changes.py` (change set with
causes, 6.3), `planner/sickcall.py` (candidate filter, parallel forced re-solves, ranking, proposal
rows) and `api/routes_proposals.py`.
