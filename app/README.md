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
