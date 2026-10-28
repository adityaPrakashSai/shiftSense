# web/

React 18 + TypeScript + Vite 5 + Tailwind 3 frontend. `npm run dev` serves it on :5173 and proxies
`/api` and `/ws` to the FastAPI server on :8000 (`make dev` starts both). `npm run build` writes
`dist/`, which FastAPI serves in production. Screens are specified in design doc 10.3.
