"""Semantic validation of a parsed ConstraintEdit (design doc 7.3 step 5). Code only, no LLM.

Each edit is resolved (nurse names to ids) and checked: dates inside the intake window and not
past, the shift not already started, `evidence_span` really in the message, charge-only ops only
from the charge nurse, and swap cells present in the published roster. Every failure is a
`Problem` whose code names a clarification template.
"""

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain.edits import CHARGE_ID, CHARGE_ONLY, Edit, EditOp
from app.domain.models import RequestStatus, ShiftCode, Ward
from app.intake.dates import fmt_day, shift_start, window
from app.intake.resolve_names import resolve
from app.intake.templates import cell
from app.planner.build_input import horizon_dates
from app.store import repo

NEEDS_DATES = {op for op in EditOp} - {EditOp.CANCEL_REQUEST}
NEEDS_SHIFTS = {EditOp.REQUEST_SHIFT, EditOp.AVOID_SHIFT, EditOp.CHANGE_DEMAND}
OPEN = {RequestStatus.PENDING_CONFIRM, RequestStatus.ACTIVE, RequestStatus.PENDING_APPROVAL}


@dataclass(frozen=True)
class Problem:
    code: str  # a key of [clarify] in the templates
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedEdit:
    """An edit after name resolution: what is stored and what the eval scores."""

    edit: Edit
    nurse_id: str | None  # None for change_demand (a ward-level edit)
    counterpart_id: str | None = None
    for_other: bool = False  # sent by the charge nurse about another nurse

    def key(self) -> dict[str, Any]:
        e = self.edit
        return {
            "op": e.op.value,
            "nurse": self.nurse_id,
            "dates": sorted(d.isoformat() for d in e.dates),
            "shifts": None if e.shifts is None else sorted(s.value for s in e.shifts),
            "counterpart": self.counterpart_id,
            "counterpart_dates": sorted(d.isoformat() for d in e.counterpart_dates),
            "counterpart_shifts": (
                None
                if e.counterpart_shifts is None
                else sorted(s.value for s in e.counterpart_shifts)
            ),
            "request_id": e.request_id,
            "target_total": e.target_total,
        }


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s).casefold()
    return re.sub(r"\s+", " ", s).strip()


def evidence_ok(span: str, message: str) -> bool:
    """`evidence_span` is a substring of the message (case and spacing ignored)."""
    return bool(span.strip()) and _norm(span) in _norm(message)


def _lang(lang: str) -> str:
    return "en" if lang == "en" else "hi"


def _who(ward: Ward, nurse_id: str) -> str:
    n = next((x for x in ward.nurses if x.id == nurse_id), None)
    return n.name if n else nurse_id


def check_edit(
    conn: sqlite3.Connection,
    ward: Ward,
    edit: Edit,
    *,
    sender_id: str,
    message: str,
    now: datetime,
    lang: str,
    check_roster: bool = True,
) -> tuple[ResolvedEdit | None, list[Problem]]:
    """Resolve one edit and list what is wrong with it (empty list = valid)."""
    rl = _lang(lang)
    is_charge = sender_id == CHARGE_ID
    today = now.date()
    first, last = window(today)
    problems: list[Problem] = []

    if not evidence_ok(edit.evidence_span, message):
        problems.append(Problem("no_evidence"))
    if edit.op in CHARGE_ONLY and not is_charge:
        return None, [Problem("charge_only")]

    nurse_id: str | None = None
    if edit.op is not EditOp.CHANGE_DEMAND:
        m = resolve(edit.nurse, ward.nurses, None if is_charge else sender_id)
        if m.nurse_id is None:
            if m.candidates:
                names = ", ".join(f"{_who(ward, c)} ({c})" for c in m.candidates)
                return None, [Problem("ambiguous_nurse", {"name": edit.nurse, "candidates": names})]
            if is_charge and edit.nurse.strip().casefold() in ("", "self", "me", "i"):
                return None, [Problem("generic")]
            return None, [Problem("unknown_nurse", {"name": edit.nurse})]
        nurse_id = m.nurse_id
        if not is_charge and nurse_id != sender_id:
            return None, [Problem("not_own")]

    if edit.op in NEEDS_DATES and not edit.dates:
        problems.append(Problem("missing_dates"))
    if edit.op in NEEDS_SHIFTS and not edit.shifts:
        problems.append(Problem("missing_shifts"))
    for d in sorted(set(edit.dates) | set(edit.counterpart_dates)):
        if d < first:
            problems.append(Problem("past_date", {"day": fmt_day(d, rl)}))
        elif d > last:
            problems.append(Problem("outside_window", {"last": fmt_day(last, rl)}))
    for d in edit.dates:
        if d != today or edit.op in (EditOp.CHANGE_DEMAND, EditOp.CANCEL_REQUEST):
            continue
        for s in edit.shifts or []:
            if s is not ShiftCode.G and shift_start(ward, s, d) <= now:
                shift = {"M": "Morning", "E": "Evening", "N": "Night"}[s.value]
                problems.append(Problem("shift_started", {"day": fmt_day(d, rl), "shift": shift}))

    counterpart_id = None
    if edit.op is EditOp.PROPOSE_SWAP:
        cm = resolve(edit.counterpart, ward.nurses, None)
        if cm.nurse_id is None or cm.nurse_id == nurse_id:
            problems.append(Problem("swap_self"))
        else:
            counterpart_id = cm.nurse_id
        one_cell = (
            len(edit.dates) == len(edit.counterpart_dates) == 1
            and len(edit.shifts or []) == len(edit.counterpart_shifts or []) == 1
        )
        if not one_cell:
            problems.append(Problem("swap_one_cell"))
        elif check_roster and counterpart_id and nurse_id:
            problems += _swap_cells(conn, ward, edit, nurse_id, counterpart_id, rl)

    if edit.op is EditOp.CANCEL_REQUEST:
        r = None if edit.request_id is None else repo.get_request(conn, edit.request_id)
        owner_ok = r is not None and (is_charge or r.nurse_id == sender_id)
        if r is None or not owner_ok or r.status not in OPEN:
            problems.append(Problem("unknown_request", {"request_id": edit.request_id or "?"}))

    if edit.op is EditOp.CHANGE_DEMAND:
        if edit.target_total is None or not 0 <= edit.target_total <= len(ward.nurses):
            problems.append(Problem("demand_target"))
        period = horizon_dates(ward)
        for d in edit.dates:
            if d not in period:
                problems.append(
                    Problem(
                        "outside_period",
                        {
                            "day": fmt_day(d, rl),
                            "first": fmt_day(period[0], rl),
                            "last": fmt_day(period[-1], rl),
                        },
                    )
                )

    return ResolvedEdit(edit, nurse_id, counterpart_id), problems


def _swap_cells(
    conn: sqlite3.Connection, ward: Ward, edit: Edit, a: str, b: str, rl: str
) -> list[Problem]:
    """Each side of a swap must hold the cell it gives away in the published roster."""
    run = repo.get_published_run(conn, ward.id)
    if run is None:
        return [Problem("swap_cells", {"who": "-", "cell": "-"})]
    roster = repo.get_roster(conn, str(run["run_id"]))
    days = [d.isoformat() for d in horizon_dates(ward)]
    out = []
    assert edit.shifts and edit.counterpart_shifts
    for nurse, d, s in (
        (a, edit.dates[0], edit.shifts[0]),
        (b, edit.counterpart_dates[0], edit.counterpart_shifts[0]),
    ):
        iso = d.isoformat()
        row = roster.get(nurse, [])
        ok = iso in days and days.index(iso) < len(row) and row[days.index(iso)] == s.value
        if not ok:
            out.append(Problem("swap_cells", {"who": _who(ward, nurse), "cell": cell(rl, d, [s])}))
    return out
