"""Infeasibility diagnosis and conflict options (design doc 5.5).

When a solve cannot meet the hard rules, the planner diagnoses it: only the groups a person may
waive (table 5.2) are guarded, so the core names exactly what could be relaxed ("unavailable:
N01:d8:N", "min_cover:d8:N"); the rules nobody can waive (rest after night, night
qualification, shifts already worked) are listed separately as context ("Priya cannot work: rest
after night"). Options come from fixed templates, one per waivable name, plus "add a float nurse"
for a cover shortfall; at most four. Choosing one waives that name (or adds the float nurse) and
starts a new run linked through `conflict.resolved_run`. Muse only narrates (7.6).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from app.domain.models import Grade, Nurse, Request, RequestKind, RequestStatus, ShiftCode, Ward
from app.log.decision_log import DecisionLog
from app.planner.build_input import horizon_dates
from app.solver.diagnose import infeasible_core
from app.solver.input import SolveInput
from app.solver.rules import rule_check
from app.store import repo

WAIVABLE = frozenset(
    {
        "unavailable",
        "leave",
        "max_consecutive_days",
        "max_shifts",
        "min_cover",
        "senior_cover",
        "fixed_duty",
        "max_hours",
    }
)
TEMPLATES = {
    "unavailable": "Ask {name} to come in on {date}{shift_part}",
    "leave": "Cancel {name}'s leave on {date}",
    "max_consecutive_days": "Allow {name} one extra consecutive day",
    "max_shifts": "Allow {name} one shift over contract",
    "min_cover": "Run {date} {shift} with {k}",
    "senior_cover": "Run {date} {shift} without the senior minimum",
    "fixed_duty": "Move {name}'s fixed duty on {date}",
    "max_hours": "Allow {name} extra hours",
    "float": "Add a float nurse for {date} {shift}",
}
ORDER = ["unavailable", "leave", "max_consecutive_days", "max_shifts", "max_hours", "fixed_duty",
         "senior_cover", "min_cover"]  # fmt: skip
SHIFT = {"M": "Morning", "E": "Evening", "N": "Night"}
MAX_OPTIONS = 4


def parse(name: str) -> dict[str, Any]:
    """'unavailable:N01:d8:N' -> {group, nurse, day, shift}; 'min_cover:d8:N' -> {group, day,
    shift}; 'max_shifts:N05' -> {group, nurse}."""
    group, *parts = name.split(":")
    out: dict[str, Any] = {"group": group, "nurse": None, "day": None, "shift": None}
    for p in parts:
        if p.startswith("d") and p[1:].lstrip("-").isdigit():
            out["day"] = int(p[1:])
        elif p in SHIFT:
            out["shift"] = p
        elif out["nurse"] is None:
            out["nurse"] = p
    return out


def _fmt(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def diagnose(inp: SolveInput) -> list[str]:
    """Waivable names that cannot all hold together; every group if that finds nothing."""
    core = infeasible_core(inp, guard_groups=WAIVABLE)
    if not core:
        core = infeasible_core(inp) or []
    return core


def context(ward: Ward, inp: SolveInput, core: list[str]) -> list[dict[str, Any]]:
    """Why the other nurses cannot fill a short cell: their non-waivable rules (5.5)."""
    roster = dict(inp.previous or {})
    names = {n.id: n.name for n in ward.nurses}
    out: list[dict[str, Any]] = []
    for c in (parse(x) for x in core):
        if c["group"] not in ("min_cover", "senior_cover") or c["day"] is None:
            continue
        for spec in inp.nurses:
            if roster.get((spec.id, c["day"])) == c["shift"]:
                continue
            rules = [r for r in rule_check(roster, inp, spec.id, c["day"], c["shift"],
                                           nurse_only=True)
                     if r.split(":", 1)[0] not in WAIVABLE]  # fmt: skip
            if rules:
                out.append({"nurse": spec.id, "name": names.get(spec.id, spec.id), "rules": rules})
    return out[:8]


def options(ward: Ward, inp: SolveInput, core: list[str]) -> list[dict[str, Any]]:
    days = horizon_dates(ward)
    names = {n.id: n.name for n in ward.nurses}
    parsed = sorted(
        (parse(x) | {"name_": x} for x in core if parse(x)["group"] in WAIVABLE),
        key=lambda c: (ORDER.index(c["group"]) if c["group"] in ORDER else 99, c["name_"]),
    )
    out: list[dict[str, Any]] = []
    short = next((c for c in parsed if c["group"] in ("min_cover", "senior_cover")), None)
    room = MAX_OPTIONS - (1 if short else 0)
    for c in parsed[:room]:
        d = None if c["day"] is None else days[c["day"]]
        k = 0
        if c["group"] == "min_cover" and c["day"] is not None:
            k = max(0, inp.demand[c["day"], c["shift"]].min_total - 1)
        label = TEMPLATES[c["group"]].format(
            name=names.get(c["nurse"] or "", c["nurse"]),
            date="" if d is None else _fmt(d),
            shift=SHIFT.get(c["shift"] or "", ""),
            shift_part=f" ({SHIFT[c['shift']]})" if c["shift"] in SHIFT else "",
            k=k,
        )
        out.append({"kind": "waive", "waive": c["name_"], "label": label, "nurse": c["nurse"],
                    "day": None if d is None else d.isoformat(), "shift": c["shift"]})  # fmt: skip
    if short is not None and short["day"] is not None:
        d = days[short["day"]]
        out.append({"kind": "float", "waive": None, "nurse": None, "day": d.isoformat(),
                    "shift": short["shift"],
                    "label": TEMPLATES["float"].format(date=_fmt(d),
                                                       shift=SHIFT[short["shift"]])})  # fmt: skip
    for i, o in enumerate(out, 1):
        o["option_id"] = f"O{i}"
    return out


def create(
    conn: sqlite3.Connection, run_id: str, ward: Ward, inp: SolveInput, log: DecisionLog
) -> str:
    """Diagnose an infeasible run and store the conflict with its options (5.5)."""
    core = diagnose(inp)
    ctx = context(ward, inp, core)
    opts = options(ward, inp, core)
    cid = repo.next_conflict_id(conn)
    repo.insert_conflict(conn, cid, run_id, {"names": core, "context": ctx}, opts)
    log.emit(
        "conflict.created", "planner", run_id, None,
        conflict_id=cid, core=core, options=[{"option_id": o["option_id"], "label": o["label"]}
                                             for o in opts],
    )  # fmt: skip
    return cid


def add_float_nurse(conn: sqlite3.Connection, ward: Ward, day: date, shift: str) -> str:
    """A temporary nurse who can work only (day, shift): hard unavailable everywhere else."""
    n = 1 + sum(1 for x in ward.nurses if x.id.startswith("F"))
    nid = f"F{n:02d}"
    ward.nurses.append(
        Nurse(
            id=nid,
            name=f"Float nurse {n}",
            grade=Grade.STAFF,
            min_shifts_per_week=0,
            max_shifts_per_week=7,
            language="en",
        )  # fmt: skip
    )
    repo.save_ward(conn, ward)
    now = datetime.now().astimezone()
    others = [d for d in horizon_dates(ward) if d != day]
    rows = [
        Request(id=repo.next_request_id(conn), nurse_id=nid, kind=RequestKind.UNAVAILABLE,
                dates=others, shifts=None, hard=True, status=RequestStatus.ACTIVE,
                created_at=now),
    ]  # fmt: skip
    for r in rows:
        repo.insert_request(conn, ward.id, r)
    rest = [ShiftCode(s) for s in SHIFT if s != shift]
    r2 = Request(id=repo.next_request_id(conn), nurse_id=nid, kind=RequestKind.UNAVAILABLE,
                 dates=[day], shifts=rest, hard=True, status=RequestStatus.ACTIVE,
                 created_at=now)  # fmt: skip
    repo.insert_request(conn, ward.id, r2)
    return nid


def apply_option(
    conn: sqlite3.Connection, conflict_id: str, option_id: str, *, by: str, log: DecisionLog
) -> tuple[dict[str, Any], list[date]]:
    """Record the choice and change the inputs (waiver or float nurse). Returns the option and
    the dates it affects; the caller starts the new run and links it."""
    c = repo.get_conflict(conn, conflict_id)
    if c is None:
        raise LookupError(conflict_id)
    if c["chosen_option"] is not None:
        raise ValueError(f"conflict {conflict_id} is already resolved")
    opt = next((o for o in c["options"] if o["option_id"] == option_id), None)
    if opt is None:
        raise KeyError(option_id)
    run = repo.get_run(conn, c["run_id"])
    ward = repo.get_ward(conn, None if run is None else str(run["ward_id"]))
    assert ward is not None
    if opt["kind"] == "float":
        opt["float_nurse"] = add_float_nurse(conn, ward, date.fromisoformat(opt["day"]),
                                             opt["shift"])  # fmt: skip
    repo.set_conflict_resolved(conn, conflict_id, option_id, by, None)
    log.emit(
        "conflict.resolved", f"user:{by}", c["run_id"], None,
        conflict_id=conflict_id, option_id=option_id, waived=opt.get("waive"),
        float_nurse=opt.get("float_nurse"),
    )  # fmt: skip
    days = sorted({date.fromisoformat(o["day"]) for o in [opt] if o.get("day")})
    if not days:
        days = [ward.horizon_start + timedelta(days=0)]
    return opt, days


def link_run(conn: sqlite3.Connection, conflict_id: str, run_id: str) -> None:
    conn.execute(
        "UPDATE conflict SET resolved_run = ? WHERE conflict_id = ?", (run_id, conflict_id)
    )
    conn.commit()
