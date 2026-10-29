# scripts/

- `seed.py`: recreate `data/shiftsense.db` from a seed (`make seed`, `make seed SEED=mini|real_a`).
- `gen_ward.py`: deterministic generator for the default synthetic ward `data/seed/ward.json`.
- `demo_baseline.py`: solves a seed once and writes `data/seed/{seed}_baseline.json` (the pinned
  demo baseline; rerun after editing the seed file).
- `cache_export.py` / `cache_import.py`: LLM response cache to and from `data/llm_cache.jsonl`;
  `cache_prune.py` drops entries for prompts that no longer exist.
- `demo_check.py`: runs the demo script (design doc 2.2, steps 2-9, then the other two /demo chips)
  over HTTP against a running server (`--base`, `--reset`), e.g. the deployed URL; in live mode it
  spends credits only for messages not already cached.
- `vision_sheets.py` + `render_sheets.mjs`: the synthetic roster sheets for the photo eval
  (`evals/vision/`): labels and printable HTML, then JPEG renders with the e2e Chromium.
- `scenarios/`: set up a scenario on a running server (e.g. the night crunch conflict).
- `export_openapi.py`, `stability_compare.py`, `whatif_bench.py`: OpenAPI export and benchmarks.
