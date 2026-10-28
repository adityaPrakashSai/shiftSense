"""API request and response models (design doc 10.1). They drive the OpenAPI schema that the
web client's types are generated from (`make api`)."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel


class Health(BaseModel):
    ok: bool
    llm_mode: str
    provider: str
    model: str | None
    db: str


class RunSummary(BaseModel):
    run_id: str
    ward_id: str
    parent_run: str | None
    kind: str
    status: str
    trigger: str | None
    created_at: str
    horizon_start: date
    priorities: list[str]
    frozen_before_day: int | None
    input_hash: str | None
    total_secs: float | None
    changed_cells: int | None
    published_at: str | None
    published_by: str | None


class StageOut(BaseModel):
    rank: int
    tier: str
    status: str
    best: int
    bound: int
    proven: bool
    secs: float
    locked: str | None


class Cell(BaseModel):
    nurse_id: str
    day: date
    shift: str | None  # M | E | N | G, None = off
    display: str  # M/E/N/G, N/O, D/O or a leave code (CL, EL, ML, ...)
    leave_type: str | None
    prev_shift: str | None
    changed: bool | None
    cause: str | None  # request | replacement | knock_on (M7)


class DayTotal(BaseModel):
    day: date
    shift: str
    assigned: int
    target: int
    min: int
    max: int


class RosterOut(BaseModel):
    run: RunSummary
    days: list[date]
    today: date | None  # outlined in the grid when inside the horizon
    grid: list[Cell]
    totals: list[DayTotal]
    stages: list[StageOut]


class CostSummary(BaseModel):
    tier: str
    rule: str
    total: int  # sum of amount x weight
    rows: int


class Change(BaseModel):
    nurse_id: str
    day: date
    from_shift: str | None
    to_shift: str | None
    cause: str | None


class Outcome(BaseModel):
    request_id: str
    status: str
    reason_code: str | None
    detail: str | None


class RunDetail(BaseModel):
    run: RunSummary
    stages: list[StageOut]
    costs: list[CostSummary]
    change_set: list[Change]
    request_outcomes: list[Outcome]


class CreateRun(BaseModel):
    kind: Literal["full"] = "full"


class RunAccepted(BaseModel):
    run_id: str


class PublishBody(BaseModel):
    by: str = "charge"


class EventOut(BaseModel):
    seq: int
    ward_id: str
    run_id: str | None
    ts: str
    actor: str
    kind: str
    stage: str | None
    payload: dict[str, Any]


class ResetOut(BaseModel):
    ok: bool
    seed: str
    secs: float
    run_id: str | None


class WsOff(BaseModel):
    seconds: float = 30.0


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody
