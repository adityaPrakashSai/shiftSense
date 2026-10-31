"""FastAPI entry point: API routes, the ward WebSocket, and the built React app from `web/dist`."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    errors,
    jobs,
    routes_agent,
    routes_conflicts,
    routes_explain,
    routes_import,
    routes_insight,
    routes_intake,
    routes_llm,
    routes_misc,
    routes_proposals,
    routes_runs,
    routes_staffing,
    routes_ward,
    ws,
)
from app.config import get_settings
from app.llm import cache as llm_cache
from app.store import repo
from app.store.db import open_db

ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = ROOT / "web" / "dist"
LLM_CACHE_FILE = ROOT / "data" / "llm_cache.jsonl"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Migrate the schema; seed (and snapshot for demo reset) when the DB holds no ward yet."""
    s = get_settings()
    ws.hub.bind(asyncio.get_running_loop())
    conn = open_db(s.db_path)
    try:
        if repo.get_ward(conn) is None:  # an existing DB is never overwritten
            routes_misc.reset_db(conn, s.seed, s.demo_start)
        # committed responses (data/llm_cache.jsonl) let a fresh clone run in replay mode (9.1)
        llm_cache.import_jsonl(conn, LLM_CACHE_FILE)
    finally:
        conn.close()
    tick = asyncio.get_running_loop().create_task(jobs.timeout_tick_loop(), name="timeout-tick")
    try:
        yield
    finally:
        tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)


app = FastAPI(title="ShiftSense", version="0.0.0", lifespan=lifespan)
errors.install(app)
app.add_middleware(GZipMiddleware, minimum_size=1000)  # the JS bundle; /demo loads cold in < 3 s
app.include_router(routes_misc.router)
app.include_router(routes_ward.router)
app.include_router(routes_runs.router)
app.include_router(routes_llm.router)
app.include_router(routes_intake.router)
app.include_router(routes_proposals.router)
app.include_router(routes_explain.router)
app.include_router(routes_conflicts.router)
app.include_router(routes_agent.router)
app.include_router(routes_insight.router)
app.include_router(routes_import.router)
app.include_router(routes_staffing.router)
app.include_router(ws.router)

if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False, response_model=None)
def spa(path: str) -> FileResponse | HTMLResponse:
    """Serve files from `web/dist`, and `index.html` for client-side routes."""
    if path.startswith(("api/", "ws/")):
        raise HTTPException(status_code=404, detail=f"No route /{path}")
    candidate = (WEB_DIST / path).resolve()
    if path and candidate.is_file() and candidate.is_relative_to(WEB_DIST):
        return FileResponse(candidate)
    index = WEB_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return HTMLResponse(
        "<h1>ShiftSense</h1><p>Web app not built. Run <code>make web</code> "
        "or use <code>make dev</code>.</p>"
    )
