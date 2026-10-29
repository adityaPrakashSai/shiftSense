# tests/

pytest suite, run by `make test` with `LLM_MODE=mock` (no network, no keys). Layout follows the
design doc test plan (11.3): `unit/`, `solver/`, `api/`, `e2e/` (Playwright), `fixtures/`.

`fixtures/mini_expected.json` holds the proven golden values for the mini ward; never edit
expected values to make a test pass.

`e2e/demo.spec.ts` is the Playwright demo script (design doc 2.2); `make e2e` builds the web app,
starts the API in mock mode on the mini ward and runs it (browsers: `npx --prefix web playwright
install chromium` once).

