"""Conflicts (design doc 5.5, 10.1): read, list, resolve with an option."""

from typing import Any

from fastapi import APIRouter

from app.agent.runtime import answers_for_conflict
from app.api import jobs
from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.schemas import ConflictOut, ResolveBody, ResolveOut
from app.api.ws import hub
from app.planner import conflicts
from app.planner.runs import start_full, start_resolve
from app.store import repo

router = APIRouter(prefix="/api/conflicts", tags=["conflicts"])


def conflict_out(c: dict[str, Any]) -> ConflictOut:
    return ConflictOut.model_validate(
        {**c, "core": c["core"].get("names", []), "context": c["core"].get("context", [])}
    )


@router.get("", response_model=list[ConflictOut])
def list_conflicts(conn: Conn, open: bool = True) -> list[ConflictOut]:
    ward = require_ward(conn)
    return [conflict_out(c) for c in repo.list_conflicts(conn, ward.id, open_only=open)]


@router.get("/{conflict_id}", response_model=ConflictOut)
def get_conflict(conn: Conn, conflict_id: str) -> ConflictOut:
    c = repo.get_conflict(conn, conflict_id)
    if c is None:
        raise ApiError(404, "CONFLICT_NOT_FOUND", f"No conflict {conflict_id}.")
    return conflict_out(c)


@router.post("/{conflict_id}/resolve", response_model=ResolveOut)
async def resolve(conn: Conn, conflict_id: str, body: ResolveBody) -> ResolveOut:
    """Apply the chosen option (a waiver or a float nurse) and start a new run with it."""
    ward = require_ward(conn)
    log = jobs.live_log(conn, ward.id)
    try:
        _opt, days = conflicts.apply_option(conn, conflict_id, body.option_id, by=body.by, log=log)
    except LookupError as exc:
        raise ApiError(404, "CONFLICT_NOT_FOUND", f"No conflict {conflict_id}.") from exc
    except KeyError as exc:
        raise ApiError(404, "OPTION_NOT_FOUND", f"No option {body.option_id}.") from exc
    except ValueError as exc:
        raise ApiError(409, "CONFLICT_RESOLVED", str(exc)) from exc
    trigger = f"conflict:{conflict_id}"
    if repo.get_published_run(conn, ward.id) is None:
        prep = start_full(conn, ward.id, trigger=trigger, log=log)
        run_id = prep.run_id
        await hub.broadcast(ward.id, {"type": "run_started", "run_id": run_id})
        jobs.spawn(prep)
    else:
        plan = start_resolve(conn, ward.id, trigger=trigger, affected=days, log=log)
        run_id = plan.run_id
        await hub.broadcast(ward.id, {"type": "run_started", "run_id": run_id})
        jobs.spawn_resolve(plan)
    conflicts.link_run(conn, conflict_id, run_id)
    jobs.agent_answer(ward.id, answers_for_conflict(conn, ward.id, conflict_id, body.option_id,
                                                    run_id))  # fmt: skip
    return ResolveOut(conflict_id=conflict_id, option_id=body.option_id, run_id=run_id)
