# web/

React 18 + TypeScript + Vite 5 + Tailwind 3 frontend (design doc 10.3, 10.4). `npm run dev` serves
it on :5173 and proxies `/api` and `/ws` to the FastAPI server on :8000 (`make dev` starts both).
`npm run build` writes `dist/`, which FastAPI serves in production.

- `src/api/client.ts`: typed fetch wrappers; types come from `src/api/schema.ts`, generated from
  `openapi.json` by openapi-typescript (`make api` after changing a route).
- `src/api/ws.ts`: `useWardSocket`, reconnect with backoff and a polling fallback.
- `src/pages/DemoPage.tsx` (`/demo`, where `/` lands): `ChargeView` at 65% and `NurseView` in a
  `PhoneFrame` at 35%, with Reset demo, the phone's identity, the model banner and three suggested
  messages (one sends a recorded Hindi voice note); below 1024 px one view at a time. The Fairness tab (Recharts) loads on first open.
- `src/pages/ChargePage.tsx` (`/charge`): header with Import photo, Generate and the live run
  timer, roster grid, coverage footer, stage strip, Publish. `src/components/roster/PhotoImport.tsx`
  is the photo dialog: upload, the editable preview ("?" cells red, days that miss the printed
  totals amber, names to check), Import.
- `src/lib/voice.ts`: the mic recorder (MediaRecorder audio/webm, re-encoded to 16 kHz mono WAV).
- `public/samples/`: the synthetic Hindi voice note the /demo chip sends.
- Right panel tabs (`src/components/panel/`): Alerts (approvals, proposals with the agent's
  claim-checked summary, conflicts, messages to nurses), Ask, Agent (live roster-agent traces),
  Fairness (Recharts), Log (decision timeline by run with lineage) and Versions (planned vs
  current, 6.5c). Priority chips in the header reorder the tiers.
- Nurse view: Chat (text or a voice note from the mic), Inbox (replacement and swap asks with Accept / Decline) and My shifts.
- `playwright.config.ts` + `../tests/e2e/`: the demo script in mock mode on /charge and /nurse
  (`demo.spec.ts`), on /demo, including Reset and phone width (`demo_page.spec.ts`), and the voice
  chip and photo import (`voice_photo.spec.ts`); `make e2e`.

