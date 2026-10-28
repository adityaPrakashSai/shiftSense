# web/

React 18 + TypeScript + Vite 5 + Tailwind 3 frontend (design doc 10.3, 10.4). `npm run dev` serves
it on :5173 and proxies `/api` and `/ws` to the FastAPI server on :8000 (`make dev` starts both).
`npm run build` writes `dist/`, which FastAPI serves in production.

- `src/api/client.ts`: typed fetch wrappers; types come from `src/api/schema.ts`, generated from
  `openapi.json` by openapi-typescript (`make api` after changing a route).
- `src/api/ws.ts`: `useWardSocket`, reconnect with backoff and a polling fallback.
- `src/pages/ChargePage.tsx` (`/charge`): header with Generate and the live run timer, roster grid,
  coverage footer, stage strip, Publish.
