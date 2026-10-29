# ShiftSense

A nurse scheduling assistant. Meta's Muse Spark models handle the plain-language, voice and
photo requests; an OR-Tools CP-SAT solver builds every roster; every decision is explained from
the solver's actual results.

- **Live demo:** _(URL added after the Railway deployment)_ · open `/demo`
- **Video (3 min):** _(link to come)_
- **Write-up:** [`docs/writeup.md`](docs/writeup.md) · **Architecture:**
  [`docs/architecture.md`](docs/architecture.md) · **Design:** [`docs/DESIGN.md`](docs/DESIGN.md)

> **Status:** feature freeze (milestone M13 of the plan in the design doc, section 12). The build
> started on 28 Oct 2026.

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
- **Voice notes** (`app/intake/voice.py`): the nurse taps the mic, the browser records and converts
  to 16 kHz WAV, and Muse Spark Open's audio input transcribes it in the language and script spoken
  (Hindi in Devanagari). The transcript then follows the typed path unchanged, so the demo sick call
  spoken and typed store the same request.
- **Roster photos** (`app/intake/photo.py`, `app/planner/importer.py`): the charge nurse
  photographs the paper roster; Muse Spark Open copies each cell (M, E, N, G, N/O, off, leave codes
  or "?") and the printed totals rows. Code maps names to nurse ids, flags duplicate names, and
  checks the cells against the printed totals; the charge nurse fixes the highlighted cells in an
  editable preview, and only then is the grid imported as the published, planned run. On five
  synthetic sheets (14 and 31 days, rendered in handwriting fonts) it copied 98.2% of 1,742 cells
  (`evals/reports/2026-10-29-vision-baseten-live.md`).
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
and calls Meta models over HTTPS. State lives in one SQLite file. Model calls (intake, voice,
vision, explainer, roster agent) go through one gateway and only read, explain and draft; the
roster is written only by the planner through CP-SAT, and only a person publishes. Diagram,
components and the sick-call flow: [`docs/architecture.md`](docs/architecture.md).

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

## Evals

Fixed eval sets, scored by code (no LLM judge); every report is in `evals/reports/`. Latency and
cost are per model call, from runs with the cache off (`--fresh`).

<!-- eval-table -->
| Suite | Provider | Result | Latency per call (p50 / p95) | Cost per call | Report |
| --- | --- | --- | --- | --- | --- |
| intake: 50 messages → ConstraintEdit | Muse Spark Open (Baseten) | 100% first pass, 100% after repair | 3.6 / 6.7 s | $0.0036 | `2026-10-29-intake-baseten-live` |
| intake: 50 messages → ConstraintEdit | Muse Spark (Meta Model API) | pending (no key yet) | | | |
| explain: 15 why-questions, claim-checked | Muse Spark Open (Baseten) | 93% pass, 100% grounded first time | 2.1 / 6.7 s | $0.0032 | `2026-10-29-explain-baseten-live` |
| explain: 15 why-questions, claim-checked | Muse Spark (Meta Model API) | pending (no key yet) | | | |
| writer: 20 replacement asks | Muse Spark Open (Baseten) | 20/20 pass, 90% model drafts kept | 7.5 / 10.5 s | $0.0054 | `2026-10-29-writer-baseten-live` |
| writer: 20 replacement asks | Muse Spark (Meta Model API) | pending (no key yet) | | | |
| agent: 10 roster-agent workflows | Muse Spark Open (Baseten) | 10/10 done by the agent (fallback 10/10), 0 forbidden attempts | 1.5 / 4.7 s | $0.0030 | `2026-10-29-agent-baseten-live` |
| agent: 10 roster-agent workflows | Muse Spark (Meta Model API) | pending (no key yet) | | | |
| vision: 5 roster sheets, per cell | Muse Spark Open (Baseten) | 98.2% of cells, 5/5 (>= 95%) sheets | 16.0 / 57.3 s | $0.0222 | `2026-10-29-vision-baseten-live` |
| vision: 5 roster sheets, per cell | Muse Spark (Meta Model API) | pending (no key yet) | | | |

Sick calls (solver only, no model; 20 random sick calls on the default ward, `2026-10-29-sickcall-stats`): 20/20 re-solved, with every tier proven optimal in 100% of them, 0.13 s median and 0.14 s at most. Changed cells besides the sick nurse's own: median 1 (max 1) with the stability tier, median 50.5 (max 69) without it.
<!-- /eval-table -->

```bash
uv run python -m evals.run --suite intake --provider baseten --mode live --fresh  # one suite, live
uv run python -m evals.run --suite intake --mode replay       # from the committed cache, no keys
uv run python -m evals.run --suite intake --compare baseten,meta   # provider comparison table
uv run python -m evals.sickcall_stats                         # 20 random sick calls, solver only
uv run python -m evals.summary --readme                       # rebuild the table above
```

Suites: `intake`, `explain`, `writer`, `agent`, `vision`. Details in [`evals/README.md`](evals/README.md).

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
phone's identity selector, the model banner and four suggested messages:

1. **Sick call (Hindi), Meena**: *"Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai"*.
   Confirm in the phone; the charge view shows the alert, the ranked replacements (Arun the standby
   nurse first) and the roster agent's claim-checked summary. Ask *"Why Arun and not Priya?"* in
   the Ask tab, Approve, then switch the phone to Arun (S01): his Inbox has Muse's message in Hindi
   with Accept / Decline. The Agent and Log tabs show every step.
2. **Day off (English), Kavya**: a soft request for Sat 7 Nov, granted or denied with a reason at
   the next solve.
3. **Why every weekend? Priya**: a grounded answer from her fairness history, with claim chips.
4. **Voice note (Hindi), Meena**: the same sick call as a recorded voice note (synthetic speech,
   `web/public/samples/sick_call_hi.wav`); the bubble shows the transcript and the same
   confirmation. The mic button in the phone records your own. It is an alternative to chip 1:
   in replay mode, press Reset demo between the two (the cached answers assume a fresh ward).

**Import photo** in the charge view reads a photo of a paper roster into an editable preview
(try `evals/vision/mini-a.jpg`, cached for replay mode).

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
