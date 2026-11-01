# ShiftSense — system design (ASCII)

High-level behavior of every module, in wireframes. Complements
`architecture.md` (components, data, deployment) and `decisions.md`
(why each non-obvious choice). Current as of the PR #2 merge
(cascade, counter-offers, open swap, command agent, PR fixes).

A charge nurse spends ~2 hours building a fortnight roster by hand.
ShiftSense generates it with CP-SAT in ~9 s, then negotiates sick
calls, leave, swaps and conflicts with nurses over a phone UI — with
a human Publish behind every roster change.

## 1. Big picture

```
 ┌──────────────┐  HTTPS/WS   ┌──────────────────────────────────────────┐
 │  React 18    │◄───────────►│  FastAPI (`app/main.py`, `app/api/`)     │
 │  Vite + TW   ├─┐           │  ┌────────┐ ┌────────┐ ┌────────────────┐ │
 │  /charge     │ │           │  │ routes │ │ jobs   │ │ ws hub         │ │
 │  /nurse/:id  │ │           │  │(12)    │ │(locks, │ │(per-ward ..   │ │
 │  /demo       │ │           │  │        │ │ bg)    │ │ sockets)       │ │
 └──────────────┘ │           │  └────┬───┘ └───┬────┘ └────────────────┘ │
                  │           └───────┼─────────┼────────────────────────┘
                  │                   │         │
        ┌─────────▼───────────────────▼─────────▼────────┐
        │              domain + planner                   │
        │  intake → requests → runs → publish             │
        │  explain (ask) · evidence · conflicts           │
        └────┬──────────────┬──────────────┬─────────────┘
             │              │              │
   ┌─────────▼──────┐ ┌─────▼───────┐ ┌────▼─────────────┐
   │ solver (CP-SAT)│ │ agent loop  │ │ LLM gateway      │
   │ OR-Tools, pure │ │ tools+      │ │ live/replay/mock │
   │ no DB imports  │ │ fallback    │ │ cache + cap      │
   └─────────┬──────┘ └─────┬───────┘ └────┬─────────────┘
             │              │              │
        ┌────▼──────────────▼──────────────▼─────────────┐
        │  SQLite (`app/store/`): ward, request, run,   │
        │  assignment, proposal, conflict, agent_task,   │
        │  nurse_message, event (decision log), llm_*   │
        └───────────────────────────────────────────────┘
```

Requests flow top-down; rosters are computed only by the
solver and published only by the charge nurse.

## 2. The trust boundary (what code — never the model — enforces)

```
  MODEL MAY                      CODE MUST
  ─────────                      ─────────
  parse a message          ──►   validate the edit (semantic.py), confirm first
  rank / investigate       ──►   allowlist per workflow; no publish tool exists
  follow the cascade       ──►   send_message rejects out-of-order nurses
  draft an ask             ──►   single-shot writer; templates for chips/notes
  summarise for approval   ──►   claims checked vs tool results (claims.py)
  answer a why-question    ──►   unverified answer withheld, describe() shown
  stop / escalate          ──►   budgets (12 calls, 4 msgs, 3 declines), tick

  Nothing in app/agent, app/api/jobs or any tick sets run.status =
  published or calls the publish route. Publishing is exactly one
  thing: POST /api/runs/{id}/publish (or choose → same publish) from
  the charge nurse's button. A test walks the AST to prove it.
```

## 3. Module map

```
  app/domain/    Ward/Nurse/Shift/Request/Run models, ConstraintEdit,
                 seed parsing. Pure; imported everywhere.
  app/store/     SQLite: schema, repo (all SQL), seed, snapshots,
                 db (migrations). Only layer that touches the DB file.
  app/solver/    RosterModel (hard groups), lexicographic + weighted
                 solve, independent validator, staffing model,
                 unsat diagnosis. NEVER imports store/api/agent.
  app/planner/   build_input (requests+history → SolveInput), runs
                 (full/resolve/publish), sickcall (rank 3), requests
                 (lifecycle), conflicts, importer, roster_view/totals.
  app/intake/    chat pipeline: parse → resolve names → semantic
                 check → template confirm → store request. Voice +
                 photo call the same pipeline afterwards.
  app/llm/       gateway (live/replay/mock), providers (Baseten /
                 Meta / Modal OpenAI-compat), response cache, spend
                 cap, JSON-tool-protocol fallback, mock answers.
  app/agent/     roster-agent loop, tool registry + allowlists,
                 deterministic fallback, runtime (start/resume),
                 message writer, timeout helpers. No solver changes.
  app/explain/   ask agent (tools in, checked answer out), claim
                 checker, describe() fallback texts, narrate.
  app/evidence/  counterfactual what-ifs, denial reasons, evidence
                 rows behind every published cell.
  app/log/       decision log (event table + socket fan-out).
  app/api/       12 route files, jobs (locks, background spans,
                 timeout tick), ws hub, schemas (→ openapi.json).
  app/tools/     replay helper (event log → state).
  web/src/       charge grid + panel tabs, nurse phone, /demo
                 split view. api/client.ts is generated from
                 openapi.json (`make api`); drift fails a test.
  evals/         suites (intake/explain/writer/agent/adversarial/
                 vision) × modes (live/replay/mock) + reports.
  tests/         pytest (326) + Playwright (8 tests, 2 servers).
  reference/     spec code to diff against, not app code.
  scripts/       demo baseline, vision sheets, seed generator.
```

## 4. Data at rest (simplified)

```
  ward ──1:N──▶ request ──N:1──▶ run ──1:N──▶ assignment
    │              │               │  ▲             (nurse,day,shift,
    │              │               │  │              changed?, cause?)
    │              │               │  │ parent_run
    │              │               ▼  │
    │              └─────▶ proposal (ranking JSON: candidates[N],
    │                           excluded[rules], open_cell)
    ├──────────▶ conflict (unsat core + options, resolved_run)
    ├──────────▶ agent_task (workflow, state JSON: calls/trace/
    │                        approved_cascade/waiting, outcome)
    ├──────────▶ nurse_message (kind: replacement|swap|note;
    │                           status: asked|accepted|declined|
    │                           conditional|expired|withdrawn|noted)
    ├──────────▶ event (decision log, seq-ordered, socket fan-out)
    ├──────────▶ explanation (ask answers + claims)
    └──────────▶ llm_call (every call + cost), llm_cache (keyed
                 answers; run ids normalised to run-ref-N)
```

## 5. Flows

### 5a. Generate → Publish (charge)

```
  charge          API/routes_runs      jobs/planner         solver        store/ws
    │ POST /runs        │                  │                  │             │
    │──────────────────▶│ start_full: insert run(solving)    │             │
    │ 202 run_id        │─────────────────▶│                  │             │
    │◄──────────────────│  + worker thread │                  │             │
    │                   │                  │──SolveInput─────▶│             │
    │                   │                  │◄──5 tiers────────│             │
    │                   │                  │  evidence rows   │             │
    │                   │                  │──────────────────────────────▶│
    │◄── run_finished (socket) ───────────────────────────────────────────│
    │ grid shows draft  │                  │                  │             │
    │ POST /runs/{id}/publish (the ONLY publish path)                     │
    │──────────────────▶│ publish(): status=published + event         │
```

With nothing published yet: empty grid + Generate card; intake
queues; Ask shows "generate first". `DEMO_START=empty` opens there.

### 5b. Intake: message → stored request (nurse chat)

```
  nurse text/voice transcript
        │  (voice: audio → transcript shown → same path)
        ▼
  ┌──────────┐   ┌───────────┐   ┌──────────┐   ┌─────────┐
  │ parse    │──▶│ resolve   │──▶│ semantic │──▶│ CONFIRM │──▶ store request
  │ (LLM or  │   │ names/    │   │ check:   │   │ template│   PENDING_CONFIRM
  │ mock)    │   │ dates to  │   │ roster   │   │ chip;   │   + reply to
  │ Constraint│   │ ids/days  │   │ reality? │   │ no model│   the nurse
  │ Edit     │   │           │   │ future?  │   │ text    │
  └──────────┘   └───────────┘   └──────────┘   └─────────┘
        │               │               │              │
        └────── clarify (template) ◄───┴── any failure ┘
```

Confirming flips the request ACTIVE (or PENDING_APPROVAL for
leave/swap), starts a re-solve when inputs changed, and hands
sick/leave/swap/demand to the agent (`workflow_for`).

### 5c. Sick call: the cascade (the demo spine)

```
  Meena sick Tue M ──confirm──▶ rank: Arun → Ritu → Kavya (+excluded)
                                         │
                    ┌────────────────────▼────────────────────┐
                    │ charge approves PLAN once (publishes    │
                    │ nothing). Direct "Publish Arun now"    │
                    │ stays, demoted.                         │
                    └────────────────────┬────────────────────┘
                                         ▼
            ┌──── ask Arun ──── decline ────┐
            │                               ▼
            │                    ┌──── ask Ritu ──── timeout/skip ────┐
            │                    │  (expired; Hindi line in inbox)    ▼
            │                    │                       ┌──── ask Kavya ── accept
            │                    │                       │
            ▼                    ▼                       ▼
       escalate w/ per-nurse outcomes        withdraw others; ONE approval:
                                             "Kavya accepted…" → Publish
```

Code owns: order, one open ask per cell, re-rank when the
published run moved under the ranking, budgets. The Agent tab
shows `Plan: A → R → K · asked Arun (declined) · asked Ritu
(waiting 0:42 / 1:00)` with a 1 s countdown while a deadline lives.

### 5d. "Yes, if…" counter-offer (one round per nurse)

```
  ask ──▶ conditional (day_off | avoid_shift + day)
              │
              ▼
  evaluate_condition: re-solve candidate run + condition HARD
  (this proposal only; never stored as a preference)
        ├── already_true ──▶ plain accept
        ├── feasible ──▶ approval ("…if he gets Mon 9 Nov off")
        │                    ├── Publish ──▶ done
        │                    └── Reject ──▶ template follow-up ──▶ Accept/Decline
        └── infeasible ──▶ template follow-up ──▶ Accept/Decline
  (a second conditional from the same nurse = decline → next)
```

### 5e. Swaps: named and open

```
  NAMED ("swap Tue M with Kavya")
    rule_check_swap → what_if → ask counterpart → accept →
    approval(request) → run_resolve → approval(run) → Publish

  OPEN ("can someone take my Sat N? I'll do Sun M")
    counterpart=null, offered cell optional, request stays ACTIVE
        │
        ▼
    find_swap_partners (pure code, no LLM):
      for each nurse: vacate-then-check BOTH moves, rank by
      fewest other changes → fairness → id; ≤3 + exclusions
        │
        ▼
    charge approves swap plan → ask in order → first accept →
    pairing written → what_if → Publish. Exhausted → template
    note to requester + escalate. Conditional accepts reuse 5d;
    "only if you take my Tue E too" → bigger-swap decline.
```

### 5f. Leave, demand, conflict, new period

```
  LEAVE / named SWAP request → PENDING_APPROVAL → charge approves
    → what_if shown → run_resolve → approval(run) → Publish.
  DEMAND (charge: "one more Thu E") → applied → sick_call-style
    cascade for the open slot.
  CONFLICT (re-solve infeasible): unsat core → diagnose →
    list options → charge picks → apply_option → run_resolve →
    approval(run) → Publish.
  NEW_PERIOD: run_full → summary → denied reasons → approval →
    Publish. COMMAND (Ask tab): read-only questions, §5h.
```

### 5g. Ask (why-questions, grounded or withheld)

```
  "Why Arun and not Priya?" ──▶ explainer agent ──▶ tools ──▶
      ┌──────────────────────────────────────────────────┐
      │ model answer + claims {text, call_id, path, value}│
      │ claims.check vs tool results                     │
      ├── pass ──▶ answer + ✓ chips (open the cited JSON)│
      └── fail ──▶ WITHHOLD; deterministic describe()    │
      │           "model answer withheld" banner         │
      └──────────────────────────────────────────────────┘
```

### 5h. Command agent (read-only goals)

```
  "tonight looks thin" ──▶ POST /api/agent/command ──▶ same
  agent loop, read-only allowlist (explainer + get_open_cells,
  get_nurse, staffing_check, what_if) ──▶ deterministic answer
  from tool results + checked chips. Change goals ("publish…")
  are refused with zero calls. The model's text is never shown.
```

### 5i. Voice note + roster photo (intake edges)

```
  VOICE: browser records webm → 16 kHz mono WAV → POST →
    Muse audio transcript (shown) → §5b pipeline. Fixture clips
    replay from cache; mock "hears" by sha256.
  PHOTO: charge dialog → POST /api/import/photo (one 32k call;
    tiled fallback lists left-tile names in right prompt) →
    editable preview (live totals checksum) → import as a
    parentless kind=import run, published at once. Leave cells
    become hard requests; rule breaks are warnings, "?" and
    unknown ids are refused.
```

### 5j. Timeout tick + demo reset (background)

```
  TICK (lifespan asyncio task, every 5 s, single process):
    waiting_reply + deadline passed (real UTC) → status expired
    → resume as timeout → next nurse / escalate. Idempotent:
    only still-asked rows fire. agent.timeout logged (system).

  RESET: cancel agent tasks → wait out ward-lock solves →
    restore seed snapshot table-by-table (llm_cache kept) →
    demo_reset nudge → every view refetches + clears timers,
    drafts, phone identity. < 1 s after the first.
```

## 6. Agent task states

```
  running ── tool waits ──┬──▶ waiting_reply ── Accept/Decline/Yes-if/Timeout/Skip
    ▲                     │      │                                  │
    │                     │      └── answer ──▶ running ────────────┘
    │                     │                                      (Cancel → done)
    │                     └──▶ waiting_approval ── Approve/Reject/Publish
    │                               │
    └────────── resume ─────────────┘
                              │
  done (outcome line) ◄── finished ──▶ escalated (reason + what was tried)
  failed (exception → visible, never hanging)
```

Every step is a trace row + decision-log event; the Agent tab
renders the trace live. Fallback (fixed code) mirrors the loop and
drives mock mode; the model may stop early only when the fallback
agrees the workflow is finished.

Per-workflow allowlists (registry classes READ / COMPUTE / ACTION):

```
  sick_call, demand_change: reads + rank_replacements +
      evaluate_condition + send/wait + request_approval + escalate
  swap:   reads + rule_check_swap + what_if + find_swap_partners +
      evaluate_condition + send/wait + request_approval + run_resolve
  leave:  reads + what_if + staffing_check + request_approval + run_resolve
  conflict: reads + diagnose + options + apply + staffing + run_resolve
  new_period: reads + run_full + denied reasons + request_approval
  command:  explainer + open_cells + nurse + staffing + what_if (no ACTION)
```

Unknown tool, off-allowlist tool, or order violation →
`agent.rejected` + error result + `forbidden` counter. Evals pin
forbidden = 0 and approvals respected = 100%.

## 7. Solver pipeline (one solve)

```
  SolveInput ──▶ RosterModel ──▶ 5 lexicographic tiers ──▶ validator ──▶ roster
  (availability,                  coverage → stability →    (independent;
   prefs, history,                contract → requests →     disagreement
   forcings,                      fairness (each: best      = solver bug,
   waivers)                       or best-found + proof)     never stored)
       │                                                        │
       └── stage.done events (tier ticks + secs) ──► evidence rows:
           cost rows per cell, denial reasons, conflict core
```

Re-solves freeze the published roster and minimise `CHANGED`
(stability tier): a sick call moves ~1 cell besides the sick
nurse's own. Ranking = baseline re-solve + up to 6 forced
`assign(candidate, d, s)` re-solves in parallel (3 s each);
ranked by tier vector, changed cells, shifts, id; top 3 kept.

## 8. LLM gateway: modes, cache, money

```
  Role (intake/explain/agent/writer/vision/audio)
        │
        ├── LIVE: cache? hit : provider call → store → llm_call row
        ├── REPLAY: cache hit or CacheMiss (agent turns → fallback)
        └── MOCK: deterministic rule/mock answers, no network, no keys
```

Cache key = provider + model + role + messages (run ids
normalised) + tools + schema + prompt-file hash. Spend cap +
per-call cost in `llm_call`; native tool calls with a JSON
protocol fallback after 2 malformed turns. Prompt text lives in
`app/llm/prompts/` — including the gateway's own instructions.

## 9. Screens (wireframes)

Charge (`/charge`): grid left, panel right.

```
 ┌───────────────────────────────────────────────────────────────┐
 │ Mini ward · 26 Oct – 8 Nov · 9 nurses     [connected] [timer] │
 │ By hand: about 2 hours                        [Generate]      │
 ├──────────────────────────────────┬────────────────────────────┤
 │ [Draft (proposed)] run-…78bf     │ Alerts|Ask|Agent|Fairness… │
 │ [Publish] Back to published      │ ┌────────────────────────┐ │
 │  M  T  W  T  F  S  S …           │ │ Kavya accepted; …      │ │
 │ Arun      M▶[M]                  │ │ ● Kavya [accepted]     │ │
 │ Ritu         E                   │ │   [Publish] [Reject]   │ │
 │ Kavya     [M]                    │ │ Messages to nurses…    │ │
 │ …cells (amber M·sky E·indigo N)  │ │ Plan: A → R → K · …    │ │
 └──────────────────────────────────┴────────────────────────────┘
```

Nurse phone (`/nurse/:id`): chat + shifts + inbox, Hindi/English.

```
 ┌──────────────┐
 │ Meena Sharma │  Chat: "Sunita di, aaj…" → Confirm chip
 │ Chat|Shifts| │  Inbox: cover ask → [Haan] [Yes,if…] [Nahi]
 │ Inbox (1)    │  expired → "Yeh request samay…" (no buttons)
 └──────────────┘
```

Demo (`/demo`): charge ‖ phone with draggable split (65%,
remembers), try-chips (sick call, voice note, day off, why
weekends?) gated on a published roster.

## 10. Concurrency + deployment + verification

```
  CONCURRENCY: one asyncio ward lock per ward; solves run in a
    2-thread executor with their own connections; CP-SAT stage
    events cross threads via run_coroutine_threadsafe; sockets
    fall back to 3 s polling (event cursor replays the gap).
  DEPLOY: Docker on Railway, /api/health check, /data volume
    (db + logs + cache-carrying image), SOLVER_WORKERS=4,
    LLM_MODE=live. Snapshots carry the baseline hash.
  VERIFY: make test = ruff + format + mypy + pytest (326);
    make e2e = build + 8 Playwright tests (6 files) on 2
    servers (publish-first :8765, cascade :8766); evals/run
    over 6 suites × live/replay/mock with forbidden/approvals
    gates.
```

Module → test map: solver (golden vectors + validator duel),
planner (scenario tests), intake (golden parses + voice/photo),
agent (unit cascade/swap/timeout/command + api flows), explain
(grounding + withhold), evals (25 agent + 9 adversarial cases),
e2e (demo page ×2, demo, empty start, cascade, staffing,
voice note, photo import).


