# scripts/

- `seed.py`: recreate `data/shiftsense.db` from a seed (`make seed`, `make seed SEED=mini|real_a`).
- `gen_ward.py`: deterministic generator for the default synthetic ward `data/seed/ward.json`.

Later milestones add `cache_export.py`, `cache_import.py` and scenario scripts.
- `demo_check.py`: runs the demo script (design doc 2.2, steps 2-9) over HTTP against a running
  server (`--base`, `--reset`); in live mode it spends credits.
