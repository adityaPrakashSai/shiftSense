"""Health, decision-log events, demo reset and a dev switch for the polling fallback."""

import sqlite3
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query

from app.api import jobs
from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.schemas import EventOut, Health, ResetBody, ResetMode, ResetOut, WsOff
from app.api.ws import hub
from app.config import get_settings
from app.store import repo, snapshot
from app.store.seed import clear, load_baseline, load_seed, seed_db, snapshot_tag

router = APIRouter(prefix="/api", tags=["misc"])


def db_status(path: str) -> str:
    if not Path(path).exists():
        return "absent"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.execute("SELECT 1")
    except sqlite3.Error:
        return "error"
    return "ok"


@router.get("/health", response_model=Health)
def health() -> Health:
    s = get_settings()
    return Health(
        ok=True,
        llm_mode=s.llm_mode,
        provider=s.llm_provider_default,
        model=s.model_for(s.llm_provider_default),
        db=db_status(s.db_path),
    )


@router.get("/events", response_model=list[EventOut])
def events(
    conn: Conn,
    run_id: str | None = None,
    after_seq: int = 0,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    last: Annotated[int | None, Query(ge=1, le=2000)] = None,
) -> list[EventOut]:
    """Events after `after_seq` in order; `last=N` returns the newest N instead (oldest first)."""
    ward = require_ward(conn)
    if last is not None:
        rows = repo.list_last_events(conn, ward.id, run_id=run_id, n=last)
    else:
        rows = repo.list_events(conn, ward.id, run_id=run_id, after_seq=after_seq, limit=limit)
    return [EventOut.model_validate(r) for r in rows]


def reset_db(conn: sqlite3.Connection, seed: str, mode: ResetMode = "published") -> None:
    """Restore the seed snapshot (fast); the first time, load the committed baseline or seed +
    solve, and save the snapshot. Mode "empty" keeps its own snapshot with nurses, history and
    requests but no roster for this period."""
    tag = snapshot_tag(seed)
    if mode == "empty":
        if load_seed(seed).runs:
            raise ApiError(
                409,
                "EMPTY_NOT_SUPPORTED",
                f"Seed {seed!r} brings its own published plan, so empty mode is not supported.",
            )
        tag = f"{tag}-empty"
    path = snapshot.snapshot_path(get_settings().db_path, tag)
    if path.exists():
        snapshot.restore(conn, path)
    elif mode == "empty":
        clear(conn)
        seed_db(conn, seed, solve=False)
        snapshot.save(conn, path)
    else:
        if not load_baseline(conn, seed):
            seed_db(conn, seed)
        snapshot.save(conn, path)


@router.post("/demo/reset", response_model=ResetOut)
async def demo_reset(conn: Conn, body: ResetBody | None = None) -> ResetOut:
    s = get_settings()
    mode: ResetMode = body.mode if body and body.mode else s.demo_start
    t0 = time.perf_counter()
    old = repo.get_ward(conn)
    await jobs.cancel_agents()  # a running task would write into the restored database
    lock = jobs.ward_lock(old.id) if old is not None else None
    if lock is not None:
        async with lock:
            reset_db(conn, s.seed, mode)
    else:
        reset_db(conn, s.seed, mode)
    ward = require_ward(conn)
    secs = round(time.perf_counter() - t0, 3)
    published = repo.get_published_run(conn, ward.id)
    run_id = None if published is None else str(published["run_id"])
    jobs.live_log(conn, ward.id).emit(
        "demo.reset", "system", run_id, seed=s.seed, secs=secs, mode=mode
    )
    await hub.broadcast(ward.id, {"type": "demo_reset", "run_id": run_id, "mode": mode})
    return ResetOut(ok=True, seed=s.seed, secs=secs, run_id=run_id, mode=mode)


@router.post("/dev/ws-off", response_model=dict[str, int])
async def ws_off(body: WsOff) -> dict[str, int]:
    """Close every WebSocket and refuse new ones for a while, to check the polling fallback."""
    return {"closed": await hub.disable(body.seconds)}
