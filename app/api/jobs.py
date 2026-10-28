"""Background solves (design doc 3.4): a 2-thread executor and one asyncio lock per ward, so the
event loop keeps serving WebSockets while CP-SAT runs (it releases the GIL)."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.api.ws import hub
from app.config import get_settings
from app.domain.models import RunStatus
from app.log.decision_log import DecisionLog
from app.planner.runs import PreparedRun, finish_full
from app.store import repo
from app.store.db import connect

executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="solver")
_locks: dict[str, asyncio.Lock] = {}
tasks: set[asyncio.Task[None]] = set()


def ward_lock(ward_id: str) -> asyncio.Lock:
    return _locks.setdefault(ward_id, asyncio.Lock())


def live_log(conn: sqlite3.Connection, ward_id: str) -> DecisionLog:
    """A DecisionLog whose every event is pushed to the ward's sockets as {type: event, seq}."""
    log = DecisionLog(conn, ward_id)
    log.listeners.append(lambda msg: hub.notify(ward_id, msg))
    return log


def _solve_in_thread(prep: PreparedRun) -> str:
    conn = connect(get_settings().db_path)
    try:
        out = finish_full(conn, prep, log=live_log(conn, prep.ward.id))
        return out.status.value
    except Exception as exc:  # never leave a run stuck in `solving`
        repo.update_run(conn, prep.run_id, status=RunStatus.FAILED.value)
        live_log(conn, prep.ward.id).emit("run.failed", "planner", prep.run_id, error=str(exc))
        return RunStatus.FAILED.value
    finally:
        conn.close()


async def solve_full(prep: PreparedRun) -> None:
    ward_id = prep.ward.id
    async with ward_lock(ward_id):
        status = await asyncio.get_running_loop().run_in_executor(executor, _solve_in_thread, prep)
    await hub.broadcast(ward_id, {"type": "run_finished", "run_id": prep.run_id, "status": status})


def spawn(prep: PreparedRun) -> None:
    task = asyncio.get_running_loop().create_task(solve_full(prep))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
