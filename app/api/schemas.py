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


ResetMode = Literal["published", "empty"]


class ResetBody(BaseModel):
    mode: ResetMode | None = None


class ResetOut(BaseModel):
    ok: bool
    seed: str
    secs: float
    run_id: str | None
    mode: str


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
    explanation_id: str | None = None  # a question answered by the explainer (7.5)
    claims: list[dict[str, Any]] = []
    transcript: str | None = None  # a voice note's transcript (the message body)


class MessageRecord(BaseModel):
    message_id: str
    nurse_id: str
    body: str
    channel: str = "text"  # text | voice
    created_at: str
    language: str | None
    reply: str | None
    reply_kind: str | None
    request_ids: list[str]
    requests: list[RequestOut]  # their current status, so a reloaded chat shows Confirm only once
    explanation_id: str | None = None
    claims: list[dict[str, Any]] = []


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
    replacement_status: str | None = None  # asked | accepted | declined
    agent: "ApprovalAsk | None" = None  # the agent task waiting for approval of this proposal


class ChooseBody(BaseModel):
    nurse_id: str | None = None


class ClaimOut(BaseModel):
    text: str
    call_id: str
    path: str
    value: Any = None


class ToolCallOut(BaseModel):
    call_id: str
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]


class CellContext(BaseModel):
    nurse_id: str
    day: date
    shift: str | None = None


class ExplainBody(BaseModel):
    question: str
    asker_id: str = "charge"
    run_id: str | None = None  # default: the published run
    context: CellContext | None = None  # the clicked cell


class ExplainOut(BaseModel):
    explanation_id: str
    answer: str
    claims: list[ClaimOut]
    tool_calls: list[ToolCallOut]
    grounded: bool  # the model's own answer passed the claim check
    fallback_used: bool  # the answer shown is the deterministic describe() text
    problems: list[str]
    language: str
    run_id: str | None = None
    latency_ms: int | None = None


class ConflictOption(BaseModel):
    option_id: str
    kind: str  # waive | float
    label: str  # template text from code (5.5)
    waive: str | None  # the hard-constraint name a person waives
    nurse: str | None
    day: date | None
    shift: str | None


class Blocked(BaseModel):
    nurse: str
    name: str
    rules: list[str]  # non-waivable rules that stop this nurse filling the short cell


class NarrativeOut(BaseModel):
    summary: str
    options: list[dict[str, str]]  # option_id + reworded label, same ids and order as `options`
    source: str  # model | template
    problems: list[str] = []


class ConflictOut(BaseModel):
    conflict_id: str
    run_id: str
    core: list[str]  # waivable hard-constraint names that cannot all hold
    context: list[Blocked]
    options: list[ConflictOption]
    narrative: NarrativeOut | None
    chosen_option: str | None
    chosen_by: str | None
    chosen_at: str | None
    resolved_run: str | None


class ResolveBody(BaseModel):
    option_id: str
    by: str = "charge"


class ResolveOut(BaseModel):
    conflict_id: str
    option_id: str
    run_id: str  # the new run that applies the choice


# ---------------------------------------------------------------- roster agent (7.10)


class ApprovalAsk(BaseModel):
    task_id: str
    workflow: str
    kind: str  # run | conflict | request
    ref: str
    summary: str
    claims: list[dict[str, Any]] = []
    summary_source: str  # agent | describe (the agent's claims failed the check)
    cascade: list[str] | None = None  # the contact plan, when the approval is for one


class TraceStep(BaseModel):
    n: int
    ts: str
    kind: str  # tool | wait | resumed | error | fallback | done | escalated
    mode: str  # agent | fallback
    tool: str | None = None
    plan: str | None = None
    brief: str | None = None
    call_id: str | None = None


class CascadeProgress(BaseModel):
    order: list[str]  # the approved (or proposed) contact plan, in order
    asked: dict[str, str]  # nurse id -> accepted | declined | timeout | waiting
    approved: bool
    deadline_at: str | None = None  # the open ask's real-UTC deadline, when waiting
    since: str | None = None  # when the open ask was sent, for the countdown


class AgentTaskOut(BaseModel):
    id: str
    workflow: str
    trigger_ref: str | None
    status: str  # running | waiting_reply | waiting_approval | done | escalated | failed
    waiting_on: str | None
    mode: str  # agent | fallback
    steps: int
    outcome: str | None
    created_at: str
    updated_at: str | None
    trace: list[TraceStep]
    waiting: dict[str, Any] | None
    counters: dict[str, int]
    fallback_reason: str | None
    calls: list[ToolCallOut] = []
    cascade: CascadeProgress | None = None


class ConditionIn(BaseModel):
    kind: str  # day_off | avoid_shift
    day: date
    shift: str | None = None  # M | E | N, required for avoid_shift


class NurseMessageOut(BaseModel):
    id: str
    nurse_id: str
    kind: str  # replacement | swap
    run_id: str | None
    request_id: str | None
    task_id: str | None
    day: date | None
    shift: str | None
    text: str
    language: str | None
    source: str | None  # model | template
    status: str  # asked | accepted | declined | withdrawn | expired | conditional
    created_at: str
    answered_at: str | None
    condition: ConditionIn | None = None  # the counter-offer, when status is conditional
    can_condition: bool = True  # a fresh ask takes "yes, if..."; a follow-up does not


class ConditionCheckBody(BaseModel):
    text: str


class ConditionCheckOut(BaseModel):
    condition: ConditionIn
    chip: str  # the confirm chip, from templates: "Mon 9 Nov off. Correct?"


class RespondBody(BaseModel):
    accept: bool
    condition: ConditionIn | None = None  # "yes, if...": exactly one condition


class ProposalRespondBody(BaseModel):
    nurse_id: str
    accept: bool


class PrioritiesBody(BaseModel):
    priorities: list[str]


class FairnessRow(BaseModel):
    nurse_id: str
    name: str
    standby: bool
    earlier_nights: int
    earlier_weekends: int
    earlier_denied: int
    nights: int
    weekend_shifts: int
    total_shifts: int
    requests_denied: int


class FairnessOut(BaseModel):
    run_id: str | None
    earlier_periods: int
    nurses: list[FairnessRow]
    ward_average: dict[str, float]  # regular nurses: nights, weekends, ... (earlier + current)


class VersionChange(BaseModel):
    nurse_id: str
    day: date
    from_shift: str | None
    to_shift: str | None
    cause: str | None  # request | replacement | knock_on | hand_edit
    run_id: str  # the version that made this change
    trigger: str | None
    request_id: str | None
    approved_by: str | None
    approved_at: str | None


class VersionOut(BaseModel):
    run: RunSummary
    label: str  # planned | current | proposal
    changes: list[VersionChange]  # against the planned version
    night_totals: list[int]
    night_targets: list[int]


class VersionsOut(BaseModel):
    period_start: date
    days: list[date]
    planned: RunSummary | None
    versions: list[VersionOut]  # current first, then open proposals


ProposalOut.model_rebuild()


class PhotoRowOut(BaseModel):
    name: str
    nurse_id: str | None
    candidates: list[str]
    cells: list[str]  # M E N G N/O D/O L:<type> or "?" (unreadable), one per period day
    flagged: bool  # a shared or unknown name: the charge nurse checks the id


class PhotoReadOut(BaseModel):
    days: list[date]
    rows: list[PhotoRowOut]
    totals: dict[str, list[int | None]]  # the printed totals rows, as read
    mismatches: list[dict[str, Any]]  # {day (1-based), shift, cells, printed}
    unreadable: int
    tiles: int  # 1, or 2 when the sheet was read in two parts (7.8 fallback)
    sheet_days: list[int]  # day numbers the sheet shows; more than the period = another period
    call_ids: list[str]
    cache_hits: int


class ImportRow(BaseModel):
    nurse_id: str
    cells: list[str]


class ImportBody(BaseModel):
    rows: list[ImportRow]
    by: str = "charge"


class ImportOut(BaseModel):
    run_id: str
    superseded: str | None
    leave_requests: list[str]
    warnings: list[str]  # hard rules the imported plan breaks (validator names)


class StaffingBody(BaseModel):
    hires: int = 0  # hypothetical night-qualified nurses H1..Hk on the standard contract
    removed: list[str] = []  # nurse ids out for the whole period
    coverage: Literal["target", "floor"] = "target"
    standby: bool = False  # may standby nurses be rostered
    keep_staff: bool = True  # today's staff count as used ("how many extra?")


class StaffingStageOut(BaseModel):
    stage: str  # shortfall | nurses_used | standby_shifts | balance
    value: int
    proven: bool
    bound: int | None


class StaffedNurse(BaseModel):
    nurse_id: str
    name: str


class ShortCell(BaseModel):
    day: date
    shift: str
    assigned: int
    target: int


class NurseLoad(BaseModel):
    nurse_id: str
    name: str
    total: int
    M: int
    E: int
    N: int
    weekend: int


class StaffingOut(BaseModel):
    status: str
    inputs: dict[str, Any]
    stages: list[StaffingStageOut]
    shortfall: int | None
    headcount: int | None
    hires_needed: int
    standby_shifts: int | None
    used: list[StaffedNurse]
    short_cells: list[ShortCell]
    load: list[NurseLoad]
    target_slots: int
    secs: float
    caveat: str
