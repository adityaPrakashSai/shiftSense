# ShiftSense

A nurse scheduling assistant. Meta's Muse Spark models handle the plain-language, voice and
photo requests; an OR-Tools CP-SAT solver builds every roster; every decision is explained from
the solver's actual results.

> **Status:** milestone M9 (unsat-core conflict diagnosis with charge-nurse options).
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
- **Replacements are ranked by the solver, not the model.** A confirmed sick call re-plans only the
  future from the published roster (every changed cell costs in the stability tier), and each
  candidate replacement is a real forced re-solve, ranked by tier vector. On the demo ward the
  proposal is on screen in about half a second with one changed cell; without the stability tier
  the same sick call moves 41 cells.
- **Explanations are grounded.** "Why Arun and not Priya?" goes to a Muse Spark agent with seven
  read-only tools over the evidence store and the counterfactual service, which re-solves the
  roster with Priya on that shift. Every fact in the answer is a claim that cites a tool result
  by path and value; code checks each one, and an answer that fails twice is withheld in favour
  of a deterministic explanation built from the same results.
- **Conflicts come with options, not excuses.** When no legal roster exists (three nurses who
  could cover tonight's night are unavailable), CP-SAT's unsat core names exactly the rules that
  cannot all hold; code turns each waivable one into an option ("Ask Arun to come in", "Add a
  float nurse"), Muse writes a two-sentence summary that is checked against the option list, and
  the charge nurse's choice starts a new solve.

## Architecture

One FastAPI process serves the API, the WebSocket and the built React app, runs CP-SAT in-process,
and calls Meta models over HTTPS. State lives in one SQLite file. Details: design doc section 3.

| Folder | Contents |
| --- | --- |
| `app/` | Python backend (FastAPI, solver, planner, LLM gateway) |
| `web/` | React + TypeScript + Vite + Tailwind frontend |
| `data/` | Seed wards (synthetic or pseudonymized) and the SQLite file at runtime |
| `scripts/` | Seed loader, demo baseline, cache export, demo check |
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
make dev       # API on :8000, Vite on :5173 (open /demo; also /charge and /nurse/N03)
make api       # regenerate web/openapi.json and the TypeScript client types
uv run python -m app.llm.smoke --provider baseten   # live check of Muse Spark Open (needs a key)
make test      # ruff, mypy (strict for app/solver), pytest in mock mode
make eval      # live intake eval on Baseten (EVAL_ARGS="--mode replay" re-runs it for free)
uv run python -m scripts.scenarios.night_crunch   # open a conflict (SEED=mini, DEMO_NOW at 22:00)
make e2e       # build the web app, run the Playwright demo tests in mock mode
make docker    # build the container image
```

`curl localhost:8000/api/health` should return `{"ok": true, "llm_mode": "mock", ...}`.

API keys live only in `.env` (git-ignored) and Railway variables; see `.env.example`.

## Quick start

No API keys needed: the scripted demo's Muse Spark answers are committed in `data/llm_cache.jsonl`,
and replay mode serves only those.

```bash
git clone <this repo> && cd shiftsense
make setup                  # creates .env from .env.example (SEED=mini, "now" = Tue 3 Nov 06:05)
LLM_MODE=replay make dev    # open http://localhost:5173/demo
```

With Docker instead (the built app is served by FastAPI on one port):

```bash
make docker
docker run --rm -p 8000:8000 -e LLM_MODE=replay -e SEED=mini \
  -e DEMO_NOW=2026-11-03T06:05:00+05:30 shiftsense      # open http://localhost:8000/demo
```

For live Muse Spark Open calls, put `BASETEN_API_KEY` in `.env` and use `LLM_MODE=live`; answers
already in the cache are still served from it, at no cost.

## Demo

Live URL: _(added after the Railway deployment)_

`/` opens `/demo`: the charge nurse view on the left (65%) and a nurse's phone on the right (35%).
The top bar has **Reset demo** (restores the published baseline in well under a second), the
phone's identity selector, the model banner and three suggested messages:

1. **Sick call (Hindi), Meena**: *"Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai"*.
   Confirm in the phone; the charge view shows the alert, the ranked replacements (Arun the standby
   nurse first) and the roster agent's claim-checked summary. Ask *"Why Arun and not Priya?"* in
   the Ask tab, Approve, then switch the phone to Arun (S01): his Inbox has Muse's message in Hindi
   with Accept / Decline. The Agent and Log tabs show every step.
2. **Day off (English), Kavya**: a soft request for Sat 7 Nov, granted or denied with a reason at
   the next solve.
3. **Why every weekend? Priya**: a grounded answer from her fairness history, with claim chips.

On a phone, `/demo` shows one view at a time with a switch at the top.
`uv run python -m scripts.demo_check --base <url> --reset` runs the same script over HTTP.

## Deploy to Railway

One Docker service (`railway.toml`: Dockerfile build, health check on `/api/health`), one Uvicorn
worker, and a volume for SQLite:

```bash
railway login && railway init        # or link an existing project: railway link
railway volume add --mount-path /data
railway variables --set LLM_MODE=live --set SEED=mini \
  --set DEMO_NOW=2026-11-03T06:05:00+05:30 --set DB_PATH=/data/shiftsense.db \
  --set LOG_DIR=/data/logs --set SOLVER_WORKERS=4 --set LLM_SPEND_CAP_USD=5.00
railway variables --set BASETEN_API_KEY=...   # the key goes only here and in .env
make deploy                                    # railway up
railway domain                                 # public URL
```

The first boot loads the committed baseline and saves the reset snapshot on the volume; later boots
keep the database as it is (use Reset demo).

## Credits

Before the hackathon, the author studied CP-SAT using a personal CP-SAT learning guide. It is
credited here as prior study material; no code was copied from it into this repository.

## Licence

MIT, see [`LICENSE`](LICENSE).
