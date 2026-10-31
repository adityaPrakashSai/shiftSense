-- ShiftSense SQLite schema (design doc 4.4, plus agent_task from 7.10 and ward_history).
-- All ids are TEXT; timestamps are ISO strings in UTC. Idempotent: run on every startup.
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS ward(id TEXT PRIMARY KEY, json TEXT NOT NULL);          -- full Ward model
CREATE TABLE IF NOT EXISTS ward_history(                                           -- carry-in (4.3)
  ward_id TEXT PRIMARY KEY, periods INT, json TEXT NOT NULL);                      -- WardHistory model
CREATE TABLE IF NOT EXISTS message(
  id TEXT PRIMARY KEY, ward_id TEXT, nurse_id TEXT, channel TEXT,                  -- text|voice|photo
  body TEXT, media_path TEXT, transcript TEXT, created_at TEXT,
  language TEXT, parse_json TEXT, reply TEXT, reply_kind TEXT,                     -- intake (7.3)
  request_ids_json TEXT, explanation_id TEXT);
CREATE TABLE IF NOT EXISTS request(
  id TEXT PRIMARY KEY, ward_id TEXT, nurse_id TEXT, kind TEXT, dates_json TEXT,
  shifts_json TEXT, leave_type TEXT, counterpart_id TEXT, hard INT, weight INT, status TEXT,
  source_message_id TEXT, edit_json TEXT, created_at TEXT, confirmed_at TEXT,
  counterpart_dates_json TEXT, counterpart_shifts_json TEXT,                       -- swap
  target_total INT, ref_request_id TEXT);                                          -- demand, cancel

CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, ward_id TEXT, parent_run TEXT, kind TEXT, status TEXT,
  trigger TEXT,                         -- "generate" | "request:R-0042" | "conflict:C-.." | "cf:..." | "import"
  created_at TEXT, horizon_start TEXT, priorities TEXT, frozen_before_day INT,
  input_json TEXT,                      -- serialised SolveInput: makes every run replayable
  input_hash TEXT, total_secs REAL, changed_cells INT, published_at TEXT, published_by TEXT);
CREATE TABLE IF NOT EXISTS stage(
  run_id TEXT, rank INT, tier TEXT, status TEXT, best INT, bound INT, proven INT,
  secs REAL, locked INT, PRIMARY KEY(run_id, rank));
CREATE TABLE IF NOT EXISTS assignment(
  run_id TEXT, nurse_id TEXT, day TEXT, shift TEXT, prev_shift TEXT, changed INT,
  cause TEXT,                           -- request | replacement | knock_on (6.3), changed cells only
  PRIMARY KEY(run_id, nurse_id, day));
CREATE TABLE IF NOT EXISTS cost(
  run_id TEXT, nurse_id TEXT, day TEXT, tier TEXT, rule TEXT, amount INT, weight INT,
  ref TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS request_outcome(
  run_id TEXT, request_id TEXT, status TEXT,       -- GRANTED | DENIED
  reason_code TEXT, detail TEXT, PRIMARY KEY(run_id, request_id));
CREATE TABLE IF NOT EXISTS fairness(
  run_id TEXT, nurse_id TEXT, nights INT, weekend_shifts INT, total_shifts INT,
  requests_made INT, requests_denied INT, PRIMARY KEY(run_id, nurse_id));
CREATE TABLE IF NOT EXISTS conflict(
  conflict_id TEXT PRIMARY KEY, run_id TEXT, core_json TEXT, options_json TEXT,
  narrative TEXT, chosen_option TEXT, chosen_by TEXT, chosen_at TEXT, resolved_run TEXT);
CREATE TABLE IF NOT EXISTS counterfactual(
  run_id TEXT, qkey TEXT, question_json TEXT, verdict TEXT, reason_code TEXT,
  base_json TEXT, alt_json TEXT, core_json TEXT, changed_cells INT, secs REAL,
  created_at TEXT, PRIMARY KEY(run_id, qkey));
CREATE TABLE IF NOT EXISTS proposal(
  run_id TEXT PRIMARY KEY, request_id TEXT, open_cells_json TEXT,
  candidates_json TEXT,                 -- ranked list, section 6.4
  chosen_nurse TEXT, message_draft TEXT, replacement_status TEXT);  -- asked|accepted|declined

CREATE TABLE IF NOT EXISTS event(       -- the decision log, section 8
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ward_id TEXT, run_id TEXT, ts TEXT,
  actor TEXT,                           -- solver | llm:<role> | user:<nurse_id or charge>
  kind TEXT, stage TEXT, payload_json TEXT);

CREATE TABLE IF NOT EXISTS llm_call(    -- every call, cached or not (section 9)
  id TEXT PRIMARY KEY, ts TEXT, role TEXT, provider TEXT, model TEXT, cache_key TEXT,
  cache_hit INT, prompt_tokens INT, completion_tokens INT, cost_usd REAL, latency_ms INT,
  ok INT, error TEXT, run_id TEXT, request_id TEXT,
  reasoning TEXT, reasoning_tokens INT);  -- Muse Spark Open's reasoning: stored, never shown
CREATE TABLE IF NOT EXISTS llm_cache(
  cache_key TEXT PRIMARY KEY, provider TEXT, model TEXT, request_json TEXT,
  response_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS explanation(
  id TEXT PRIMARY KEY, run_id TEXT, question TEXT, answer TEXT, claims_json TEXT,
  tool_calls_json TEXT, grounded INT, problems_json TEXT, created_at TEXT,
  asker_id TEXT, fallback_used INT, latency_ms INT, language TEXT);

CREATE TABLE IF NOT EXISTS agent_task(  -- roster agent (7.10)
  id TEXT PRIMARY KEY, ward_id TEXT, workflow TEXT, trigger_ref TEXT,
  status TEXT,                -- running | waiting_reply | waiting_approval | done | escalated | failed
  waiting_on TEXT,            -- nurse id or "charge"
  messages_json TEXT,         -- full chat + tool history, so a resume continues the same plan
  pending_call_json TEXT, steps INT, created_at TEXT, updated_at TEXT,
  mode TEXT,                  -- agent | fallback (7.10 deterministic fallback)
  state_json TEXT,            -- counters, step trace, results by call id, waiting details
  outcome TEXT);              -- one line: what the task ended with
CREATE TABLE IF NOT EXISTS nurse_message(  -- replacement asks and swap asks (7.7), nurse inbox
  id TEXT PRIMARY KEY, ward_id TEXT, nurse_id TEXT, kind TEXT,   -- replacement | swap
  run_id TEXT, request_id TEXT, task_id TEXT, day TEXT, shift TEXT,
  text TEXT, language TEXT, source TEXT,                         -- model | template
  status TEXT,                -- asked | accepted | declined | withdrawn | expired
  created_at TEXT, answered_at TEXT);

CREATE INDEX IF NOT EXISTS ix_assignment_day ON assignment(run_id, day, shift);
CREATE INDEX IF NOT EXISTS ix_event_run ON event(run_id, seq);
CREATE INDEX IF NOT EXISTS ix_request_ward ON request(ward_id, nurse_id, status);
CREATE INDEX IF NOT EXISTS ix_llm_call_ts ON llm_call(ts);
CREATE INDEX IF NOT EXISTS ix_nurse_message ON nurse_message(ward_id, nurse_id, status);
