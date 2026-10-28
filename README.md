# ShiftSense

A nurse scheduling assistant. Meta's Muse Spark models handle the plain-language, voice and
photo requests; an OR-Tools CP-SAT solver builds every roster; every decision is explained from
the solver's actual results.

> **Status:** milestone M6 (Muse Spark intake to typed constraint edits, nurse chat with confirmation).
> The build started on **28 Oct 2026**. See [`docs/DESIGN.md`](docs/DESIGN.md) for the full
> design and the milestone plan (section 12).

## The problem

In a government hospital department of 24 nurses, the charge nurse builds the roster entirely by
hand, and **one schedule takes about 2 hours**. Sudden gaps are covered by phoning standby nurses,
and existing rostering software is hard to update when the plan changes (for example, when a shift
suddenly needs 5 nurses instead of 4). ShiftSense aims to produce a valid draft roster in under a
minute and absorb a sick call with as few changes as possible.

## How Meta models are used

- The default live model is **Muse Spark Open** (`meta-models/muse-spark-open-1026`) served on
  Baseten; the Meta Model API (Muse Spark) is the alternative provider.
- The model never edits the roster. It turns messages into typed constraint edits, a person
  confirms them, and CP-SAT re-solves.
- The `openai` Python package is used **only as the HTTP client** for Baseten's OpenAI-compatible
  endpoint, which serves Muse Spark Open. **No OpenAI (or any non-Meta) model is ever called.**
- Every call goes through one gateway (`app/llm/`) with a response cache. `LLM_MODE=live` calls
  the model on a cache miss, `replay` serves only cached answers (no keys needed), and `mock` uses
  a rule-based stand-in for UI work and tests; the header shows which provider answered and a
  MOCK MODE badge. A per-process spend cap (`LLM_SPEND_CAP_USD`) stops live calls.
- **Intake** (`app/intake/`): a nurse writes "Kal mujhe off chahiye" in English, Hindi or Hinglish.
  Muse Spark Open turns it into a `ConstraintEdit` (a closed set of operations, checked against a
  JSON schema), code checks names, dates and the quoted evidence, and a fixed template (never the
  model) writes the confirmation in the sender's language. Only after the nurse taps Confirm does
  the request reach the solver. The first live run of the 50-case intake eval is in
  `evals/reports/`.

## Architecture

One FastAPI process serves the API, the WebSocket and the built React app, runs CP-SAT in-process,
and calls Meta models over HTTPS. State lives in one SQLite file. Details: design doc section 3.

| Folder | Contents |
| --- | --- |
| `app/` | Python backend (FastAPI, solver, planner, LLM gateway) |
| `web/` | React + TypeScript + Vite + Tailwind frontend |
| `data/` | Seed wards (synthetic or pseudonymized) and the SQLite file at runtime |
| `scripts/` | Seed loader and generator |
| `tests/` | pytest suite and fixtures |
| `evals/` | Fixed LLM eval sets, runner, scorer and reports |
| `reference/` | Pre-build reference prototypes (specs, not app code) |
| `docs/` | Design document and design-decision log |

## Setup

Requirements: Python 3.12 via [uv](https://docs.astral.sh/uv/), Node 20+, and Docker (optional).

```bash
make setup     # uv sync, npm ci in web/, creates .env from .env.example
make seed      # recreate data/shiftsense.db and publish a solved baseline (SEED=mini|real_a)
uv run python -m app.solver.cli data/seed/ward.json   # solve a seed, print the grid
uv run python -m app.tools.replay RUN_ID              # re-solve a stored run, compare tiers
make dev       # API on :8000, Vite on :5173 (open /charge, and /nurse/N07 for a nurse's phone)
make api       # regenerate web/openapi.json and the TypeScript client types
uv run python -m app.llm.smoke --provider baseten   # live check of Muse Spark Open (needs a key)
make test      # ruff, mypy (strict for app/solver), pytest in mock mode
make eval      # live intake eval on Baseten (EVAL_ARGS="--mode replay" re-runs it for free)
make docker    # build the container image
```

`curl localhost:8000/api/health` should return `{"ok": true, "llm_mode": "mock", ...}`.

API keys live only in `.env` (git-ignored) and Railway variables; see `.env.example`.

## Demo

Coming in M11: a `/demo` page with the charge nurse view and the nurse's phone view side by side.

## Credits

Before the hackathon, the author studied CP-SAT using a personal CP-SAT learning guide. It is
credited here as prior study material; no code was copied from it into this repository.

## Licence

MIT, see [`LICENSE`](LICENSE).
