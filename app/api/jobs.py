"""Background solves (design doc 3.4): a 2-thread executor and one asyncio lock per ward, so the
event loop keeps serving WebSockets while CP-SAT runs (it releases the GIL). Also the roster agent's
runtime (7.10): `env` lets agent tools start solves and wait for them, and `agent_start` /
`agent_answer` run tasks in the background, one at a time per task."""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from app.api.ws import hub
from app.config import get_settings
from app.domain.models import RunStatus
from app.evidence.counterfactual import explain_denied
from app.explain.narrate import narrate
from app.llm.gateway import get_gateway
from app.log.decision_log import DecisionLog
from app.planner.runs import PreparedRun, ResolvePlan, finish_full, start_full, start_resolve
from app.planner.sickcall import rank
from app.store import repo
from app.store.db import connect

executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="solver")
log = logging.getLogger(__name__)
_locks: dict[str, asyncio.Lock] = {}
tasks: set[asyncio.Task[None]] = set()
background: set[asyncio.Future[None]] = set()
finished: dict[str, asyncio.Event] = {}  # run id -> set once its job has written everything


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
    infeasible run, announce its conflict, let Muse narrate it (7.6) and start the conflict
    workflow (7.10)."""
    if status == RunStatus.INFEASIBLE.value:
        conn = connect(get_settings().db_path)
        try:
            c = repo.conflict_for_run(conn, run_id)
            if c is None:
                return
            msg = {"type": "conflict_created", "conflict_id": c["conflict_id"], "run_id": run_id}
            await hub.broadcast(ward_id, msg)
            agent_start(ward_id, "conflict", conflict_id=c["conflict_id"], run_id=run_id)
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


def _done(run_id: str) -> None:
    ev = finished.get(run_id)
    if ev is not None:
        ev.set()


async def solve_full(prep: PreparedRun) -> None:
    ward_id = prep.ward.id
    try:
        async with ward_lock(ward_id):
            loop = asyncio.get_running_loop()
            status = await loop.run_in_executor(executor, _solve_in_thread, prep)
    finally:
        _done(prep.run_id)
    await hub.broadcast(ward_id, {"type": "run_finished", "run_id": prep.run_id, "status": status})
    await after_run(ward_id, prep.run_id, status)


def spawn(prep: PreparedRun) -> None:
    finished[prep.run_id] = asyncio.Event()
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
    try:
        async with ward_lock(ward_id):
            loop = asyncio.get_running_loop()
            status = await loop.run_in_executor(executor, _resolve_in_thread, plan)
    finally:
        _done(plan.run_id)
    await hub.broadcast(ward_id, {"type": "run_finished", "run_id": plan.run_id, "status": status})
    await after_run(ward_id, plan.run_id, status)
    if status == RunStatus.PROPOSED.value:
        await hub.broadcast(
            ward_id,
            {"type": "proposal_created", "run_id": plan.run_id, "request_id": plan.request_id},
        )


def spawn_resolve(plan: ResolvePlan) -> None:
    finished[plan.run_id] = asyncio.Event()
    task = asyncio.get_running_loop().create_task(solve_resolve(plan))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


# ---------------------------------------------------------------- roster agent runtime (7.10)


class JobsEnv:
    """The agent's view of the app (`app.agent.env.Env`)."""

    async def start_resolve(
        self,
        conn: sqlite3.Connection,
        ward_id: str,
        *,
        trigger: str,
        affected: Iterable[date],
        request_id: str | None,
        extra_unavailable: Iterable[tuple[str, int, str | None]] = (),
    ) -> str:
        plan = start_resolve(conn, ward_id, trigger=trigger, affected=affected,
                             request_id=request_id, extra_unavailable=extra_unavailable,
                             log=live_log(conn, ward_id))  # fmt: skip
        await hub.broadcast(ward_id, {"type": "run_started", "run_id": plan.run_id})
        spawn_resolve(plan)
        return plan.run_id

    async def start_full(self, conn: sqlite3.Connection, ward_id: str, *, trigger: str) -> str:
        prep = start_full(conn, ward_id, trigger=trigger, log=live_log(conn, ward_id))
        await hub.broadcast(ward_id, {"type": "run_started", "run_id": prep.run_id})
        spawn(prep)
        return prep.run_id

    async def await_run(self, run_id: str, timeout: float = 60.0) -> None:
        ev = finished.get(run_id)
        if ev is not None:
            await asyncio.wait_for(ev.wait(), timeout)
            return
        conn = connect(get_settings().db_path)  # a run started outside this process's jobs
        try:
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                run = repo.get_run(conn, run_id)
                if run is None or run["status"] != RunStatus.SOLVING.value:
                    return
                await asyncio.sleep(0.05)
        finally:
            conn.close()
        raise TimeoutError(f"run {run_id} still solving after {timeout:.0f} s")

    async def broadcast(self, ward_id: str, msg: dict[str, Any]) -> None:
        await hub.broadcast(ward_id, msg)


env = JobsEnv()
_task_locks: dict[str, asyncio.Lock] = {}


def _agent_on() -> bool:
    return get_settings().agent_enabled


async def _run_agent(ward_id: str, task_id: str, fn: Any) -> None:
    """Run a start or resume with the task's lock and its own connection; an unexpected error
    marks the task failed instead of leaving it running."""
    from app.agent import runtime

    lock = _task_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        conn = connect(get_settings().db_path)
        try:
            ward = repo.get_ward(conn, ward_id)
            if ward is None:
                return
            ctx_log = live_log(conn, ward_id)
            await fn(runtime, conn, ward, ctx_log)
        except Exception as exc:  # noqa: BLE001 - the workflow must end visibly, never hang
            log.exception("agent task %s failed", task_id)
            task = repo.get_agent_task(conn, task_id)
            if task is not None and task["status"] not in ("done", "escalated"):
                task.update(status="failed", outcome=f"{type(exc).__name__}: {exc}"[:300])
                repo.save_agent_task(conn, task)
                live_log(conn, ward_id).emit("agent.failed", "planner", None, None,
                                             task_id=task_id, error=str(exc)[:300])  # fmt: skip
            await hub.broadcast(ward_id, {"type": "agent_updated", "task_id": task_id})
        finally:
            conn.close()


def _spawn_agent(coro: Any) -> None:
    task = asyncio.get_running_loop().create_task(coro, name="agent")
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def cancel_agents() -> None:
    """Demo reset: stop every running agent task and wait until each has unwound. Solves are not
    cancelled; the reset waits for them on the ward lock instead."""
    running = [t for t in tasks if t.get_name() == "agent" and not t.done()]
    for t in running:
        t.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    _task_locks.clear()


def agent_start(ward_id: str, workflow: str, *, prefetch: str | None = None,
                **trigger: Any) -> str | None:  # fmt: skip
    """Create the task row now (so its id is known) and run it in the background."""
    if not _agent_on():
        return None
    from app.agent import runtime

    conn = connect(get_settings().db_path)
    try:
        task_id = runtime.next_task_id(conn)
        conn.execute("INSERT INTO agent_task(id, ward_id, workflow, status, created_at) "
                     "VALUES(?, ?, ?, 'starting', ?)", (task_id, ward_id, workflow,
                                                        repo.now_iso_ms()))  # fmt: skip
        conn.commit()
    finally:
        conn.close()

    async def go(rt: Any, conn: sqlite3.Connection, ward: Any, lg: DecisionLog) -> None:
        await rt.start(conn, ward, workflow, env=env, gateway=get_gateway(), log=lg,
                       prefetch=prefetch, task_id=task_id, **trigger)  # fmt: skip

    _spawn_agent(_run_agent(ward_id, task_id, go))
    return task_id


def agent_answer(ward_id: str, answers: list[tuple[str, dict[str, Any]]]) -> None:
    """Resume each waiting task with a person's answer, in the background."""
    for task_id, answer in answers:

        async def go(rt: Any, conn: sqlite3.Connection, ward: Any, lg: DecisionLog,
                     task_id: str = task_id, answer: dict[str, Any] = answer) -> None:  # fmt: skip
            await rt.resume(conn, ward, task_id, answer, env=env, gateway=get_gateway(), log=lg)

        _spawn_agent(_run_agent(ward_id, task_id, go))
