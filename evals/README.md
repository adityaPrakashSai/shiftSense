# evals/

Fixed eval sets for the LLM roles (design doc 9.3). The `*.jsonl` files are versioned, never edited
to make a run pass, and never used as few-shot examples (the intake examples live in
`app/llm/prompts/intake_examples.jsonl` and share no message with `intake.jsonl`).

- `intake.jsonl`: 50 messages with the expected parse, each pinned to a `now` on the default seed
  ward (`data/seed/ward.json`).
- `run.py`: `uv run python -m evals.run --suite intake --provider baseten [--mode live|replay|mock]
  [--limit N] [--max-usd 2] [--compare baseten,meta]`. Live runs add their responses to
  `data/llm_cache.jsonl`; CI replays the suite from that file with no keys.
- `explain.jsonl`: 15 why-questions, each on a scripted world (`setups.py`: the mini ward's
  reference baseline, optionally the scenario-A sick call or the scenario-C demand change) with
  the tools it should use, facts it must cite and words it must not say.
- `writer.jsonl`: 20 replacement asks (nurse, day, shift, the facts behind the choice) for the
  message writer (7.7); checked for the date, the shift name, length and the health blocklist.
  `tests/unit/test_agent.py` replays the recorded drafts from the cache.
- `agent.jsonl` + `agent_eval.py`: 15 roster-agent scenarios on the mini ward (ag-01..ag-10
  publish-first: sick call with accept, declines and escalation, a non-top choice, a rejection,
  a demand change, a conflict, a leave, a swap, a new period; ag-11..ag-15 the contact-plan
  cascade), each run by the agent and by the fixed-code fallback with a scripted charge nurse
  and scripted replies.
- `agent_adversarial.jsonl` + `agent_adv.py`: 9 misuse scenarios (hostile source text, an
  unknown nurse, rogue model turns via pinned fixtures, duplicate and late answers, a rejected
  plan), mock mode only: `uv run python -m evals.run --suite agent_adversarial --provider mock`.
  Each must end with forbidden attempts blocked and logged, zero publishes by code, and no
  messages beyond budget.
- `vision/` + `vision_eval.py`: the photo-import set (7.8): five synthetic roster sheets with the
  seed wards' invented names (`labels.json`, three 14-day and two 31-day grids, one with two rows
  named "Priya Nair"), made by `scripts/vision_sheets.py` and rendered in handwriting fonts by
  `scripts/render_sheets.mjs`. Scored per cell after the app's own name mapping, plus the printed
  totals. A photo of a handwritten copy saved as `vision/photo-<sheet>.jpg` is scored against the
  same labels: `--suite vision`.
- `sickcall_stats.py`: 20 random sick calls on the default ward, each re-solved with and without
  the stability tier (changed cells, solve time, proven rate); no model is called.
- `summary.py`: one table from the latest live report per suite and provider
  (`reports/summary.md`; `--readme` updates the README's eval table).
- `--fresh` on `run.py`: start without the committed cache so every call is live; each report then
  carries per-call latency (p50/p95), cost and tokens by role, from the gateway's call log.
- `score.py`: code-only scoring (no LLM judge) and the summary metrics.
- `reports/`: one JSON and one markdown file per run.

Budget (9.4): at most three full live runs a day; use `--limit 10` while iterating on prompts.
