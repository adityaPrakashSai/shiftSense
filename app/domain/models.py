"""Domain models shared across module boundaries (design doc 4.2).

Dates are ISO `YYYY-MM-DD` at the edges; the solver works with day indices
(see app/solver/input.py).
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel

DEFAULT_PRIORITIES: tuple[str, ...] = ("coverage", "stability", "contract", "requests", "fairness")


class ShiftCode(StrEnum):
    M = "M"
    E = "E"
    N = "N"
    G = "G"  # General duty: daytime admin shift (09:00-17:00, Mon-Sat) worked by the
    # ANS (assistant nursing superintendent); not counted in ward coverage or fairness


class DayOffCode(StrEnum):  # display and fairness only; the solver sees "off"
    NO = "N/O"  # the off day right after a night block
    DO = "D/O"  # any other day off


class Grade(StrEnum):
    SENIOR = "senior"
    STAFF = "staff"
    TRAINEE = "trainee"


class Role(StrEnum):
    STAFF = "staff"
    SENIOR = "senior"
    IN_CHARGE = "in_charge"  # fixed morning Mon-Sat, counts toward coverage
    ANS = "ans"  # assistant nursing superintendent: G Mon-Sat, not in coverage


class RosterStyle(StrEnum):
    GRID = "grid"  # nurse x day grid, any shift any day (default)
    FIXED_ASSIGNMENT = "fixed_assignment"  # one shift + one fixed weekday off per period (5.7, M15)


class HorizonPreset(StrEnum):
    WEEK = "week"
    FORTNIGHT = "fortnight"
    HALF_MONTH = "half_month"  # 1st-15th or 16th-end
    MONTH = "month"  # calendar month, 28-31 days


class ShiftType(BaseModel):
    code: ShiftCode
    name: str  # "Morning"
    start: str  # "07:00"
    end: str  # "15:00"; end < start means overnight
    hours: float = 8.0  # unequal lengths allowed (e.g. 7 / 6 / 11)


class Nurse(BaseModel):
    id: str  # "N07" stable id; names can change
    name: str  # "Meena Sharma" (synthetic); may repeat, UI adds "(N07)"
    aliases: list[str] = []  # "Meena", "मीना", "Meena di" for intake matching
    grade: Grade
    role: Role = Role.STAFF
    standby: bool = False  # never rostered in a full run; first sick-call candidates
    can_night: bool = True
    night_exempt_until: date | None = None  # hard no-nights until this date (medical etc.)
    fixed_duty: dict[int, ShiftCode] = {}  # weekday (0=Mon) -> shift, e.g. ANS {0..5: G}
    min_shifts_per_week: float  # scaled to the horizon in build_input
    max_shifts_per_week: float
    max_hours_per_week: float | None = None  # optional hours limit, hard when set
    language: str = "hi"  # reply language: "hi" | "en"


class Demand(BaseModel):
    day: date
    shift: ShiftCode
    min_total: int  # hard floor (safety)
    target_total: int  # soft target (coverage tier)
    max_total: int | None = None  # hard ceiling; None means target_total
    min_senior: int = 0  # hard skill mix; off by default (all nurses share skills)


class Ward(BaseModel):
    id: str = "ward-7"
    name: str
    tz: str = "Asia/Kolkata"
    roster_style: RosterStyle = RosterStyle.GRID
    horizon_preset: HorizonPreset = HorizonPreset.FORTNIGHT
    horizon_start: date
    horizon_days: int = 14  # derived from the preset (month = days in that month)
    shifts: list[ShiftType]
    nurses: list[Nurse]
    demand: list[Demand]
    max_consecutive_days: int = 6
    night_block_min: int = 2  # soft (SINGLE_NIGHT)
    night_block_max: int = 3  # hard (max_consecutive_nights); a ward may set 4
    max_nights_per_month: int = 6  # hard cap, scaled to the horizon (5.2)
    priorities: list[str] = list(DEFAULT_PRIORITIES)  # tier order (5.3), editable (P1)

    def nurse(self, nurse_id: str) -> Nurse:
        for n in self.nurses:
            if n.id == nurse_id:
                return n
        raise KeyError(nurse_id)


class LeaveType(StrEnum):
    CL = "CL"
    EL = "EL"
    RH = "RH"
    GH = "GH"
    ML = "ML"
    CCL = "CCL"
    STUDY = "STUDY"


class RequestKind(StrEnum):
    UNAVAILABLE = "unavailable"  # sick, emergency: HARD, for given shifts or whole day
    LEAVE = "leave"  # approved leave: HARD
    DAY_OFF = "day_off"  # preference: SOFT
    SHIFT_WANTED = "shift_wanted"  # preference: SOFT
    SHIFT_AVOID = "shift_avoid"  # preference: SOFT ("no nights this week")
    SWAP = "swap"  # two nurses exchange cells: becomes forced edits after approval


HARD_KINDS = frozenset({RequestKind.UNAVAILABLE, RequestKind.LEAVE})


class RequestStatus(StrEnum):
    PENDING_CONFIRM = "pending_confirm"  # parsed, waiting for the nurse to confirm
    ACTIVE = "active"  # confirmed, used by the next solve
    REJECTED = "rejected"  # nurse said the parse was wrong
    WITHDRAWN = "withdrawn"


class Request(BaseModel):
    id: str  # "R-0042"
    nurse_id: str
    kind: RequestKind
    dates: list[date]  # may run past the horizon end (leave carries over)
    shifts: list[ShiftCode] | None = None  # None = whole day
    leave_type: LeaveType | None = None  # LEAVE only; shown as a banner on the grid
    counterpart_id: str | None = None  # for SWAP
    hard: bool  # derived from kind, never from the LLM
    weight: int = 1  # within the preferences tier
    status: RequestStatus
    source_message_id: str | None = None
    created_at: datetime


class Assignment(BaseModel):
    nurse_id: str
    day: date
    shift: ShiftCode | None  # None = off (shown as N/O, D/O or a leave code)


class RunStatus(StrEnum):
    SOLVING = "solving"
    PROPOSED = "proposed"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    INFEASIBLE = "infeasible"
    FAILED = "failed"


class RunKind(StrEnum):
    FULL = "full"
    RESOLVE = "resolve"
    COUNTERFACTUAL = "counterfactual"
    IMPORT = "import"  # a photographed or printed plan loaded as a published run


class NurseHistoryRecord(BaseModel):
    """Carry-in from earlier published periods (4.3 `NurseHistory`, at the domain edge)."""

    last_days: list[ShiftCode | None] = [None, None, None]  # days -3, -2, -1; None = off
    consecutive_days: int = 0
    consecutive_nights: int = 0
    nights: int = 0
    mornings: int = 0
    evenings: int = 0
    weekends: int = 0
    denied: int = 0


class WardHistory(BaseModel):
    periods: int = 0  # number of earlier published periods summed into the totals
    nurses: dict[str, NurseHistoryRecord] = {}
