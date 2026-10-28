"""FastAPI entry point: API routes plus the built React app from `web/dist`."""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import routes_ward
from app.api.errors import ApiError, api_error_handler
from app.config import get_settings
from app.store import repo
from app.store.db import open_db
from app.store.seed import seed_db

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Migrate the schema, and seed the database when it holds no ward yet."""
    s = get_settings()
    conn = open_db(s.db_path)
    try:
        if repo.get_ward(conn) is None:
            seed_db(conn, s.seed)
    finally:
        conn.close()
    yield


app = FastAPI(title="ShiftSense", version="0.0.0", lifespan=lifespan)
app.add_exception_handler(ApiError, api_error_handler)
app.include_router(routes_ward.router)


def _db_status(path: str) -> str:
    if not Path(path).exists():
        return "absent"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.execute("SELECT 1")
    except sqlite3.Error:
        return "error"
    return "ok"


@app.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "ok": True,
        "llm_mode": s.llm_mode,
        "provider": s.llm_provider_default,
        "model": s.model_for(s.llm_provider_default),
        "db": _db_status(s.db_path),
    }


if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False, response_model=None)
def spa(path: str) -> FileResponse | HTMLResponse:
    """Serve files from `web/dist`, and `index.html` for client-side routes."""
    if path.startswith(("api/", "ws/")):
        raise HTTPException(status_code=404)
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
