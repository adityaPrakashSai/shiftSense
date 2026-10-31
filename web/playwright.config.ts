// Playwright config for the end-to-end demo test (design doc 11.3): `make e2e` builds the web app,
// and this starts the API in mock mode on the mini ward with "now" pinned to the sick-call morning.
// Two servers: the demo specs share the publish-first one, cascade.spec.ts uses the cascade one.
import { defineConfig } from "@playwright/test";

const PORT = 8765;
const CASCADE_PORT = 8766;

export default defineConfig({
  testDir: "../tests/e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  // full Chromium in new headless mode (the same binary `playwright install chromium` fetches)
  use: { baseURL: `http://127.0.0.1:${PORT}`, viewport: { width: 1440, height: 900 }, channel: "chromium" },
  webServer: [
    {
      command:
        `rm -f data/e2e.db data/e2e.db-* data/e2e.snapshot-*.db && ` +
        `LLM_MODE=mock SEED=mini DEMO_NOW=2026-11-03T06:05:00+05:30 DB_PATH=data/e2e.db ` +
        // the demo script below approves one candidate, so the server pins the old flow
        `AGENT_CASCADE=0 EXPLAIN_DENIED=false SOLVER_STAGE_SECONDS=2 uv run uvicorn app.main:app --port ${PORT}`,
      cwd: "..",
      url: `http://127.0.0.1:${PORT}/api/health`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command:
        `rm -f data/e2e-cascade.db* && ` +
        `LLM_MODE=mock SEED=mini DEMO_NOW=2026-11-03T06:05:00+05:30 DB_PATH=data/e2e-cascade.db ` +
        `AGENT_CASCADE=1 AGENT_REPLY_TIMEOUT_SECONDS=5 EXPLAIN_DENIED=false SOLVER_STAGE_SECONDS=2 uv run uvicorn app.main:app --port ${CASCADE_PORT}`,
      cwd: "..",
      url: `http://127.0.0.1:${CASCADE_PORT}/api/health`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
