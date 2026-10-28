"""Explanations (design doc 7.4, 10.1): POST /api/explain, a stored explanation, and the evidence
behind one cell (the cell popover)."""

from datetime import date
from typing import Any

from fastapi import APIRouter

from app.api import jobs
from app.api.deps import Conn, require_run, require_ward
from app.api.errors import ApiError
from app.api.schemas import ExplainBody, ExplainOut
from app.config import get_settings
from app.explain.agent import ask, get_explanation
from app.explain.tools import ToolContext, get_assignment_evidence
from app.explain.tools import ToolError as ToolErr
from app.intake.dates import ward_now
from app.llm.gateway import get_gateway
from app.store import repo

router = APIRouter(prefix="/api", tags=["explain"])


@router.post("/explain", response_model=ExplainOut)
async def explain(conn: Conn, body: ExplainBody) -> ExplainOut:
    """Answer a why-question from tool results only; claims are checked (7.4)."""
    ward = require_ward(conn)
    if not body.question.strip():
        raise ApiError(400, "EMPTY_QUESTION", "Ask a question.")
    if body.run_id is not None:
        require_run(conn, body.run_id)
    cell = None if body.context is None else body.context.model_dump(mode="json")
    try:
        r = await ask(
            conn,
            ward,
            body.question,
            asker_id=body.asker_id,
            gateway=get_gateway(),
            run_id=body.run_id,
            cell=cell,
            today=ward_now(ward.tz, get_settings().demo_now).date().isoformat(),
            log=jobs.live_log(conn, ward.id),
        )
    except LookupError as exc:
        raise ApiError(404, "NO_RUN", "Nothing published to explain yet.") from exc
    return ExplainOut.model_validate({**r.__dict__, "run_id": body.run_id})


@router.get("/explanations/{explanation_id}", response_model=ExplainOut)
def get_one(conn: Conn, explanation_id: str) -> ExplainOut:
    ex = get_explanation(conn, explanation_id)
    if ex is None:
        raise ApiError(404, "EXPLANATION_NOT_FOUND", f"No explanation {explanation_id}.")
    return ExplainOut.model_validate(ex)


@router.get("/evidence/cell")
def cell_evidence(
    conn: Conn, nurse_id: str, day: date, run_id: str | None = None
) -> dict[str, Any]:
    """Shift, change, cost rows and blocked alternatives for one cell (the "Why?" popover)."""
    ward = require_ward(conn)
    rid = run_id or str((repo.get_published_run(conn, ward.id) or {}).get("run_id", ""))
    require_run(conn, rid)
    try:
        return get_assignment_evidence(ToolContext(conn, ward, rid, "charge"), nurse_id, day)
    except ToolErr as exc:
        raise ApiError(404, "CELL_NOT_FOUND", str(exc)) from exc
