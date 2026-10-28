# Design decisions log

One line per non-obvious choice (design doc 11.4 rule 9): date, decision, reason.

- 2026-10-28 · Python env and deps managed with `uv` (`uv.lock` committed) · fast, reproducible installs; Docker uses `uv sync --frozen`.
- 2026-10-28 · `LLM_MODE` defaults to `live` in code but `.env.example` sets `mock`, and `make setup` copies it to `.env` · deployed app is live by default; a fresh clone runs offline with no keys.
- 2026-10-28 · `/api/health` reports `db` as `ok` / `absent` / `error` and opens SQLite read-only · a health check must not create the database file.
- 2026-10-28 · `make test` also runs `ruff format --check` · keeps generated code tidy (3.3).
- 2026-10-28 · `reference/` is excluded from ruff and from `make test`; run it with `make reference` · it is spec code, not app code (reference/README.md).
- 2026-10-28 · The fixtures zip was unpacked at the repo root (`data/seed/`, `tests/fixtures/`, `reference/`) and the design doc lives at `docs/DESIGN.md` · matches CLAUDE.md.
