"""API request and response models (design doc 10.1). They drive the OpenAPI schema that the
web client's types are generated from (`make api`)."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from app.domain.edits import ConstraintEdit


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


class ProviderUsage(BaseModel):
    provider: str
    model: str
    calls: int
    cache_hits: int
    errors: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int


class LastCall(BaseModel):
    role: str
    provider: str
    model: str
    cache_hit: bool
    ok: bool
    latency_ms: int
    ts: str


class LLMUsage(BaseModel):
    mode: str
    default_provider: str
    configured_providers: list[str]
    calls_today: int
    cache_hit_rate: float
    cost_today_usd: float
    spent_this_process_usd: float
    spend_cap_usd: float
    json_protocol_roles: list[str]
    by_provider: list[ProviderUsage]
    last_call: LastCall | None


class RequestOut(BaseModel):
    id: str
    nurse_id: str
    kind: str
    dates: list[date]
    shifts: list[str] | None
    leave_type: str | None
    counterpart_id: str | None
    counterpart_dates: list[date]
    counterpart_shifts: list[str] | None
    target_total: int | None
    ref_request_id: str | None
    hard: bool
    weight: int
    status: str  # pending_confirm | active | pending_approval | rejected | withdrawn | applied
    source_message_id: str | None
    created_at: str
    outcome: Outcome | None  # in the newest solved run that includes it


class MessageReply(BaseModel):
    message_id: str
    parse: ConstraintEdit | None
    request_id: str | None
    request_ids: list[str]
    reply: str
    reply_kind: Literal["confirm", "clarify", "answer", "refuse"]
    language: str


class MessageRecord(BaseModel):
    message_id: str
    nurse_id: str
    body: str
    created_at: str
    language: str | None
    reply: str | None
    reply_kind: str | None
    request_ids: list[str]
    requests: list[RequestOut]  # their current status, so a reloaded chat shows Confirm only once


class RequestAction(BaseModel):
    request: RequestOut
    requests: list[RequestOut]  # every request the action changed (a message confirms at once)
    run_id: str | None  # the solve it started, if the roster inputs changed


class Candidate(BaseModel):
    nurse: str
    rank: int
    standby: bool
    kind: str  # "call in" | "move from E" ...
    tiers: dict[str, int]
    delta_vs_base: dict[str, int]
    delta_vs_best: dict[str, int]
    changed_cells: int
    run_id: str
    proven: bool
    tie: bool = False
    tie_break: str | None = None
    reason_code: str | None = None  # TIER:<tier> | CHANGED_CELLS | TIE


class Excluded(BaseModel):
    nurse: str
    rules: list[str]  # hard-constraint names, e.g. rest_after_night:N05:d7


class OpenCell(BaseModel):
    nurse: str | None  # None for a slot a demand increase added
    day: date
    shift: str


class ProposalOut(BaseModel):
    run_id: str
    run: RunSummary
    request: RequestOut | None
    open_cells: list[OpenCell]
    open_cell: OpenCell | None  # the cell candidates were ranked for
    candidates: list[Candidate]
    excluded: list[Excluded]
    baseline: dict[str, int] | None
    coverage_worse: bool  # worse coverage than the published roster (6.2)
    published_coverage: int | None
    timing_ms: dict[str, int]
    change_set: list[Change]
    chosen_nurse: str | None
    message_draft: str | None


class ChooseBody(BaseModel):
    nurse_id: str | None = None
