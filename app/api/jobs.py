"""Background solves (design doc 3.4): a 2-thread executor and one asyncio lock per ward, so the
event loop keeps serving WebSockets while CP-SAT runs (it releases the GIL)."""

import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.api.ws import hub
from app.config import get_settings
from app.domain.models import RunStatus
from app.evidence.counterfactual import explain_denied
from app.explain.narrate import narrate
from app.llm.gateway import get_gateway
from app.log.decision_log import DecisionLog
from app.planner.runs import PreparedRun, ResolvePlan, finish_full
from app.planner.sickcall import rank
from app.store import repo
from app.store.db import connect

executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="solver")
log = logging.getLogger(__name__)
_locks: dict[str, asyncio.Lock] = {}
tasks: set[asyncio.Task[None]] = set()
background: set[asyncio.Future[None]] = set()


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


def _denied_in_thread(run_id: str) -> None:
    conn = connect(get_settings().db_path)
    try:
        explain_denied(conn, run_id)
    except Exception as exc:  # noqa: BLE001 - a background nicety must never break a run
        log.warning("denied-request reasons for %s failed: %s", run_id, exc)
    finally:
        conn.close()


async def after_run(ward_id: str, run_id: str, status: str) -> None:
    """Fill denied-request reason codes in the background (M8, time-boxed to 20 s); for an
    infeasible run, announce its conflict and let Muse narrate it (7.6)."""
    if status == RunStatus.INFEASIBLE.value:
        conn = connect(get_settings().db_path)
        try:
            c = repo.conflict_for_run(conn, run_id)
            if c is None:
                return
            msg = {"type": "conflict_created", "conflict_id": c["conflict_id"], "run_id": run_id}
            await hub.broadcast(ward_id, msg)
            try:
                await narrate(conn, c["conflict_id"], gateway=get_gateway(),
                              log=live_log(conn, ward_id))  # fmt: skip
            except Exception as exc:  # noqa: BLE001 - the template text stays
                log.warning("narration of %s failed: %s", c["conflict_id"], exc)
            await hub.broadcast(ward_id, msg)
        finally:
            conn.close()
        return
    if status == RunStatus.PROPOSED.value and get_settings().explain_denied:
        fut = asyncio.get_running_loop().run_in_executor(executor, _denied_in_thread, run_id)
        background.add(fut)
        fut.add_done_callback(background.discard)


async def solve_full(prep: PreparedRun) -> None:
    ward_id = prep.ward.id
    async with ward_lock(ward_id):
        status = await asyncio.get_running_loop().run_in_executor(executor, _solve_in_thread, prep)
    await hub.broadcast(ward_id, {"type": "run_finished", "run_id": prep.run_id, "status": status})
    await after_run(ward_id, prep.run_id, status)


def spawn(prep: PreparedRun) -> None:
    task = asyncio.get_running_loop().create_task(solve_full(prep))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _resolve_in_thread(plan: ResolvePlan) -> str:
    conn = connect(get_settings().db_path)
    try:
        return rank(conn, plan, log=live_log(conn, plan.ward.id)).status.value
    except Exception as exc:  # never leave a run stuck in `solving`
        repo.update_run(conn, plan.run_id, status=RunStatus.FAILED.value)
        live_log(conn, plan.ward.id).emit("run.failed", "planner", plan.run_id, error=str(exc))
        return RunStatus.FAILED.value
    finally:
        conn.close()


async def solve_resolve(plan: ResolvePlan) -> None:
    """Minimal-change re-solve with ranked replacements (6.2, 6.4)."""
    ward_id = plan.ward.id
    async with ward_lock(ward_id):
        loop = asyncio.get_running_loop()
        status = await loop.run_in_executor(executor, _resolve_in_thread, plan)
    await hub.broadcast(ward_id, {"type": "run_finished", "run_id": plan.run_id, "status": status})
    await after_run(ward_id, plan.run_id, status)
    if status == RunStatus.PROPOSED.value:
        await hub.broadcast(
            ward_id,
            {"type": "proposal_created", "run_id": plan.run_id, "request_id": plan.request_id},
        )


def spawn_resolve(plan: ResolvePlan) -> None:
    task = asyncio.get_running_loop().create_task(solve_resolve(plan))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
