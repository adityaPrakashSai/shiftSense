# tests/

pytest suite, run by `make test` with `LLM_MODE=mock` (no network, no keys). Layout follows the
design doc test plan (11.3): `unit/`, `solver/`, `api/`, `e2e/` (Playwright), `fixtures/`.

`fixtures/mini_expected.json` holds the proven golden values for the mini ward; never edit
expected values to make a test pass.
