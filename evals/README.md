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
- `score.py`: code-only scoring (no LLM judge) and the summary metrics.
- `reports/`: one JSON and one markdown file per run.

Budget (9.4): at most three full live runs a day; use `--limit 10` while iterating on prompts.
