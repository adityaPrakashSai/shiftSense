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

Explanations (M8): `evidence/counterfactual.py` (what-if re-solves, full and local scope, cache,
denied-request reason codes), `solver/diagnose.py` (infeasibility core), `explain/tools.py` (7
tools), `explain/agent.py` (loop, budgets, both tool modes), `explain/claims.py` (grounding check),
`explain/describe.py` (deterministic text with claims) and `explain/mock.py`.

Conflicts (M9): `solver/diagnose.py` (unsat core over guarded constraints, minimized),
`planner/conflicts.py` (waivable core, context, template options, float nurse, waivers),
`explain/narrate.py` (checked summary) and `api/routes_conflicts.py`.

Roster agent (M10): `agent/tools.py` (tool registry in three classes, per-workflow allowlists, no
publish tool), `agent/loop.py` (resumable Muse Spark tool loop over `agent_task`, budgets,
guardrails, switch to the fallback), `agent/fallback.py` (the workflows as fixed code, approval
summaries, trace lines), `agent/runtime.py` (start, resume, match approvals and replies to the
waiting task), `agent/writer.py` (message writer with post-checks and templates), `agent/mock.py`
and `agent/env.py`. `api/jobs.py` runs tasks in the background; `api/routes_agent.py` serves the
Agent tab and the nurse inbox; `api/routes_insight.py` serves Fairness and Versions.

Voice and photo (M12): `intake/voice.py` (audio -> transcript through `Role.VOICE`, then the typed
path), `intake/photo.py` (photo -> preview grid through `Role.VISION`: whole-sheet read with a
two-tile fallback, merge, name mapping, totals checksum), `planner/importer.py` (the corrected grid
as the published `import` run, leave cells as leave requests, rule breaks as warnings) and
`api/routes_import.py`.

Staffing check (M14): `solver/staffing.py` (used[n] per nurse on the roster model, lexicographic
shortfall / nurses used / standby shifts / balance, hire symmetry breaking), `planner/staffing.py`
(the what-if from the database, read-only), `api/routes_staffing.py` and the agent's
`staffing_check` compute tool (conflict and leave workflows).

