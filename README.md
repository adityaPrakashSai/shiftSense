# ShiftSense

**A charge nurse spends about two hours building each fortnight's roster by hand, then rebuilds it
over the phone every time someone calls in sick. ShiftSense builds the roster in seconds and
handles the sick call with an AI agent, while a person approves every change.**

An OR-Tools CP-SAT solver builds every roster. Meta's **Muse Spark** understands nurses' messages
in Hindi or English (typed or spoken), writes each nurse's message, runs the replacement workflow
and explains every decision from the solver's own results. It never edits the roster.

- **Video (3 min):** _(link)_
- **Run it locally:** [Quick start](#quick-start), no API keys needed
- **Docs:** [Architecture](docs/architecture.md) · [System design](docs/system-design.md) ·
  [Decision log](docs/decisions.md) · [Design doc](docs/DESIGN.md)

## What it does

1. **Generate.** The ward opens on an empty fortnight. One click builds the full roster in seconds,
   with strict priorities: coverage, then stability, contracts, requests, then fairness of nights
   and weekends (carried over from earlier periods).
2. **A sick call in Hindi.** *"Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai"* ("I can't
   make this morning's duty, I have a fever"). Muse Spark turns it into a structured request; code
   checks it; the nurse taps Confirm.
3. **The replacement cascade.** The solver re-plans only the future and ranks replacements, each
   checked by a real forced re-solve. The charge nurse approves a contact plan once ("ask Arun,
   then Ritu, then Kavya"); approving it publishes nothing. The agent asks each nurse in their
   language. A decline, or no answer before a real deadline, moves on to the next nurse. The first
   nurse who accepts comes back to the charge nurse, who clicks **Publish**.
4. **Why?** Click any cell, or ask "Why Arun and not Priya?". The answer cites the solver's
   evidence, and code checks every claim before it is shown.

On the demo ward, Meena's sick call changes 1 cell besides her own; without the stability tier it
moves 27. Across 20 random sick calls on the 24-nurse ward: 1 vs a median of 50.5.

## The problem

From interviews with a charge nurse, a nurse and a nursing student at a government hospital in
India: a department of 24 nurses works three 8-hour shifts, rostered entirely by hand. **One
schedule takes the charge nurse about 2 hours.** Sudden gaps are covered by phoning standby
nurses one by one, and existing software is hard to update when the plan changes.

## How Muse Spark is used

The live model is **Muse Spark Open** (`meta-models/muse-spark-open-1026`) on Baseten; the Meta
Model API (Muse Spark) is configured as the alternative provider. The `openai` Python package is
used **only as the HTTP client** for these OpenAI-compatible endpoints. **No OpenAI (or any
non-Meta) model is ever called.**

| Role | What Muse Spark does | What code checks |
| --- | --- | --- |
| Intake | Hindi, English or Hinglish message → typed `ConstraintEdit` | schema, names, dates, quoted evidence; a template (not the model) writes the confirmation |
| Voice | Hindi voice note → transcript (audio input) | then the same intake path |
| Roster agent | runs sick calls, leave, swaps, conflicts and new periods with tools | per-workflow allowlist, budgets, contact order, no publish tool |
| Messages | drafts each ask in the nurse's language | date, shift, length, no health words |
| Explainer | answers "why?" with tool calls over the evidence | every claim checked against tool results; failures withheld |
| Vision | copies a photographed paper roster cell by cell | name mapping, printed-totals checksum, a person corrects the preview |

### The rules the code enforces (not the prompt)

- **The model never edits the roster.** Only CP-SAT writes assignments, and only a person publishes.
  The agent's tool registry has no publish tool; a test walks the agent code's AST to prove it.
- **Code decides who gets asked.** `send_message` rejects any nurse who is not next in the
  approved contact plan, and allows one open ask per shift.
- **Timeouts are real.** A deadline uses the wall clock; a background tick expires unanswered asks
  and moves on. The charge nurse can also skip or cancel.
- **Nothing a nurse writes reaches the agent as text.** The agent sees the structured request only;
  an adversarial eval (prompt injection, out-of-order sends, extra asks, late replies) must end
  with zero publishes by code.

More the agent can do: **"Yes, if…"** counter-offers ("I'll take the night if I get Monday off",
checked by a re-solve), **open swaps** ("can someone take my Saturday night?", partners found by
code), conflict diagnosis with options when no legal roster exists, a staffing check (minimum
headcount), and a read-only "Ask the agent" box.

## Quick start

No API keys needed. Requirements: Python 3.12 via [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
git clone https://github.com/adityaPrakashSai/shiftSense && cd shiftSense
make setup   # deps + .env from .env.example
DEMO_START=empty AGENT_REPLY_TIMEOUT_SECONDS=60 LLM_MODE=mock make dev   # open http://localhost:5173/demo
```

- `LLM_MODE=mock` runs everything offline with a rule-based stand-in for the model.
- `LLM_MODE=replay` serves recorded Muse Spark answers from `data/llm_cache.jsonl`; a call that
  isn't recorded (for example agent turns after a prompt change) falls back to the fixed-code
  path, so the workflow still finishes.
- `LLM_MODE=live` with `BASETEN_API_KEY` in `.env` calls Muse Spark Open; a spend cap
  (`LLM_SPEND_CAP_USD`) stops live calls.

With Docker: `make docker && docker run --rm -p 8000:8000 -e LLM_MODE=mock -e DEMO_START=empty
-e AGENT_REPLY_TIMEOUT_SECONDS=60 shiftsense`, then open http://localhost:8000/demo.

## Demo walkthrough

`/demo` shows the charge nurse view and a nurse's phone side by side (drag the divider).

1. **Reset demo** with Start = **Empty**, then **Generate roster** and **Publish**.
2. Click **Sick call (Hindi) · Meena** and **Confirm** on the phone.
3. On the proposal card, **Approve plan**.
4. Switch the phone to the first nurse and **Decline**. Let the second nurse's ask time out (or
   click **Skip to next** in the Agent tab). The third nurse **Accepts**.
5. **Publish** on the accepted nurse's card. Click the new cell and ask **Why?**.

The Agent tab shows the plan, each ask and its outcome, and a live countdown; the Log tab shows
every decision. Other chips: a voice note (synthetic Hindi speech), a day-off request, and "Why do
I get every weekend?". **Import photo** reads a paper roster (try `evals/vision/mini-a.jpg`).

## Evals

Fixed eval sets, scored by code (no LLM judge); reports in `evals/reports/`.

<!-- eval-table -->
| Suite | Provider | Result | Latency per call (p50 / p95) | Cost per call | Report |
| --- | --- | --- | --- | --- | --- |
| intake: 50 messages → ConstraintEdit | Muse Spark Open (Baseten) | 100% first pass, 100% after repair | 3.4 / 5.8 s | $0.0086 | `2026-11-02-intake-baseten-live` |
| intake: 50 messages → ConstraintEdit | Muse Spark 1.3 (Meta Model API) | 100% first pass, 100% after repair | 3.6 / 5.2 s | $0.0079 | `2026-11-02-intake-meta-live` |
| explain: 15 why-questions, claim-checked | Muse Spark Open (Baseten) | 93% pass, 100% grounded first time | 2.1 / 6.7 s | $0.0032 | `2026-10-29-explain-baseten-live` |
| explain: 15 why-questions, claim-checked | Muse Spark 1.3 (Meta Model API) | not run | | | |
| writer: 20 replacement asks | Muse Spark Open (Baseten) | 20/20 pass, 90% model drafts kept | 7.5 / 10.5 s | $0.0054 | `2026-10-29-writer-baseten-live` |
| writer: 20 replacement asks | Muse Spark 1.3 (Meta Model API) | not run | | | |
| agent: 10 roster-agent workflows | Muse Spark Open (Baseten) | 10/10 done by the agent (fallback 10/10), 0 forbidden attempts | 1.4 / 6.9 s | $0.0032 | `2026-10-29-agent-baseten-live` |
| agent: 10 roster-agent workflows | Muse Spark 1.3 (Meta Model API) | not run | | | |
| vision: 5 roster sheets, per cell | Muse Spark Open (Baseten) | 98.2% of cells, 5/5 (>= 95%) sheets | 16.0 / 57.3 s | $0.0222 | `2026-10-29-vision-baseten-live` |
| vision: 5 roster sheets, per cell | Muse Spark 1.3 (Meta Model API) | not run | | | |

Sick calls (solver only, no model; 20 random sick calls on the default ward, `2026-10-29-sickcall-stats`): 20/20 re-solved, with every tier proven optimal in 100% of them, 0.13 s median and 0.14 s at most. Changed cells besides the sick nurse's own: median 1 (max 1) with the stability tier, median 50.5 (max 69) without it.
<!-- /eval-table -->

Meta's $0.0079 was computed at the `[default]` placeholder rate, before `prices.toml` had a Meta
entry. At Meta's listed $1.25 / $4.25 per million tokens, the same run's 4357 in / 887 out tokens
come to about $0.0092 per call, so Meta is not cheaper than Baseten here.
Cost figures from reports dated before 2026-11-01 were computed at placeholder prices.

The agent suite adds 15 cascade, counter-offer, open-swap and command cases, and an adversarial
suite of 9 (prompt injection, out-of-order sends, off-allowlist tools, extra asks, late replies);
they run in the test suite in mock mode, and every forbidden attempt is blocked with 0 publishes
by code.

## Architecture

One FastAPI process serves the API, WebSocket and React app, runs CP-SAT in-process and calls
Muse Spark over HTTPS; state lives in one SQLite file. Diagram and sick-call flow:
[`docs/architecture.md`](docs/architecture.md); every module's behaviour:
[`docs/system-design.md`](docs/system-design.md).

| Folder | Contents |
| --- | --- |
| `app/` | backend: API, solver, planner, intake, agent, explainer, LLM gateway |
| `web/` | React + TypeScript + Vite + Tailwind frontend |
| `data/` | synthetic seed wards and the recorded model answers |
| `evals/` | eval sets, runner, scorer and reports |
| `tests/` | pytest and Playwright suites |
| `scripts/` | seed, baseline, cache and demo scripts |
| `reference/` | pre-build reference prototypes (specs, not app code) |
| `docs/` | design doc, architecture, system design, decision log |

## Development

```bash
make test      # ruff, mypy, pytest in mock mode
make e2e       # Playwright: demo, empty start, cascade, staffing, voice, photo
make eval      # live intake eval on Baseten (needs a key)
make dev       # API on :8000, Vite on :5173
```

Not deployed: run it locally. The repo still carries a `Dockerfile` and `railway.toml` for a
one-container deploy with a volume for SQLite.

## How it was built

Designed by the author from the interviews above. The solver, planner, intake, explainer,
first roster agent, evals and initial UI were built with **Claude Code** from the author's design,
with the author reviewing every milestone.
**Muse Code** built the UI polish pass, the empty-fortnight opening, and the agentic upgrade: reply
timeouts, the replacement cascade, steering, the adversarial eval, "Yes, if…" counter-offers, open
swaps, the read-only command agent and their review fixes (25 commits marked "(Muse Code)").
Additionally, **Muse Code** was used to create decision logs, document architecture and system design.
All data is synthetic or pseudonymized.

## Credits

Before the hackathon, the author studied CP-SAT using a personal CP-SAT learning guide. It is
credited here as prior study material; no code was copied from it into this repository.

## Licence

MIT, see [`LICENSE`](LICENSE).
