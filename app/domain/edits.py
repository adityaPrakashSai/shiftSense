"""The constraint edit language (design doc 7.2): the only structure the intake model may emit.

It is a closed set of operations; anything else is `out_of_scope` or a clarification. A validated
edit becomes a stored `Request` through `Request.from_edit` (kind from OP_KIND, `hard` from
HARD_KINDS).
"""

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain.models import RequestKind, ShiftCode


class EditOp(StrEnum):
    MARK_UNAVAILABLE = "mark_unavailable"  # sick / emergency -> Request(kind=UNAVAILABLE, hard)
    REQUEST_LEAVE = "request_leave"  # planned leave -> needs charge-nurse approval -> LEAVE (hard)
    REQUEST_DAY_OFF = "request_day_off"  # soft
    REQUEST_SHIFT = "request_shift"  # soft
    AVOID_SHIFT = "avoid_shift"  # soft
    PROPOSE_SWAP = "propose_swap"  # two nurses, two cells -> forcings after approval
    CANCEL_REQUEST = "cancel_request"
    CHANGE_DEMAND = "change_demand"  # charge nurse only: target for (date, shift)


OP_KIND: dict[EditOp, RequestKind] = {
    EditOp.MARK_UNAVAILABLE: RequestKind.UNAVAILABLE,
    EditOp.REQUEST_LEAVE: RequestKind.LEAVE,
    EditOp.REQUEST_DAY_OFF: RequestKind.DAY_OFF,
    EditOp.REQUEST_SHIFT: RequestKind.SHIFT_WANTED,
    EditOp.AVOID_SHIFT: RequestKind.SHIFT_AVOID,
    EditOp.PROPOSE_SWAP: RequestKind.SWAP,
    EditOp.CANCEL_REQUEST: RequestKind.CANCEL,
    EditOp.CHANGE_DEMAND: RequestKind.DEMAND,
}
CHARGE_ONLY = frozenset({EditOp.CHANGE_DEMAND})
CHARGE_ID = "charge"  # sender id of the charge nurse (demo: no auth, 10.1)


class Edit(BaseModel):
    op: EditOp
    nurse: str = "self"  # "self" = sender, or a nurse id / name as written
    dates: list[date] = []
    shifts: list[ShiftCode] | None = None  # None = whole day
    counterpart: str | None = None  # swap partner
    counterpart_dates: list[date] = []
    counterpart_shifts: list[ShiftCode] | None = None
    request_id: str | None = None  # cancel_request
    target_total: int | None = None  # change_demand
    reason_category: Literal["sick", "family", "personal", "training", "other"] | None = None
    evidence_span: str  # exact substring of the message that supports this edit


class ConstraintEdit(BaseModel):
    intent: Literal["edit", "question", "greeting", "out_of_scope"]
    edits: list[Edit] = []
    question: str | None = None  # intent == question -> routed to the explainer
    needs_clarification: bool = False
    clarification: str | None = None  # one short question, in the sender's language
    language: Literal["en", "hi", "hinglish"]
    confidence: float = Field(ge=0, le=1)


def constraint_edit_schema() -> dict[str, Any]:
    """JSON schema passed as `response_schema` to the intake role."""
    return ConstraintEdit.model_json_schema()
