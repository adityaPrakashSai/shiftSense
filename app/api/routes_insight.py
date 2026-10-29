"""Fairness over time and planned-vs-current versions (design doc 10.1, 10.3, 6.5c, US-12)."""

import sqlite3
from datetime import date
from typing import Any

from fastapi import APIRouter

from app.api.deps import Conn, require_ward
from app.api.schemas import FairnessOut, RunSummary, VersionsOut
from app.domain.models import RunStatus, Ward
from app.planner.build_input import horizon_dates
from app.planner.roster_view import run_summary
from app.store import repo

router = APIRouter(prefix="/api", tags=["insight"])


@router.get("/fairness", response_model=FairnessOut)
def fairness(conn: Conn, nurse_id: str | None = None) -> FairnessOut:
    """Nights, weekend shifts and denied requests per nurse: earlier published periods (carry-in)
    and the current published roster, with the ward average over regular nurses."""
    ward = require_ward(conn)
    hist = repo.get_history(conn, ward.id)
    pub = repo.get_published_run(conn, ward.id)
    cur = {} if pub is None else {r["nurse_id"]: r for r in repo.get_fairness(conn, pub["run_id"])}
    rows: list[dict[str, Any]] = []
    for n in ward.nurses:
        if n.role.value == "ans" or (nurse_id and n.id != nurse_id):
            continue
        h = hist.nurses.get(n.id)
        c = cur.get(n.id) or {}
        rows.append({
            "nurse_id": n.id, "name": n.name, "standby": n.standby,
            "earlier_nights": 0 if h is None else h.nights,
            "earlier_weekends": 0 if h is None else h.weekends,
            "earlier_denied": 0 if h is None else h.denied,
            "nights": c.get("nights", 0) or 0, "weekend_shifts": c.get("weekend_shifts", 0) or 0,
            "total_shifts": c.get("total_shifts", 0) or 0,
            "requests_denied": c.get("requests_denied", 0) or 0,
        })  # fmt: skip
    regular = [r for r in rows if not r["standby"]]

    def avg(key: str) -> float:
        return round(sum(r[key] for r in regular) / len(regular), 2) if regular else 0.0

    keys = ("earlier_nights", "earlier_weekends", "nights", "weekend_shifts", "total_shifts")
    average = {k: avg(k) for k in keys}
    average["all_nights"] = round(average["earlier_nights"] + average["nights"], 2)
    average["all_weekends"] = round(average["earlier_weekends"] + average["weekend_shifts"], 2)
    return FairnessOut.model_validate({
        "run_id": None if pub is None else pub["run_id"], "earlier_periods": hist.periods,
        "nurses": rows, "ward_average": average,
    })  # fmt: skip


# ---------------------------------------------------------------- versions (6.5c)


def chain(conn: sqlite3.Connection, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The run and its ancestors, newest first; the last one is the planned version."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = run
    while cur is not None and all(cur["run_id"] != x["run_id"] for x in out):
        out.append(cur)
        cur = None if cur["parent_run"] is None else repo.get_run(conn, cur["parent_run"])
    return out


def _request_of(conn: sqlite3.Connection, run: dict[str, Any]) -> str | None:
    trig = str(run["trigger"] or "")
    if trig.startswith("candidate:"):
        parent = repo.get_run(conn, trig.split(":", 1)[1])
        trig = "" if parent is None else str(parent["trigger"] or "")
    if trig.startswith("request:"):
        return trig.split(":", 1)[1]
    prop = repo.proposal_for_run(conn, str(run["run_id"]))
    return None if prop is None else prop["request_id"]


def version(conn: sqlite3.Connection, ward: Ward, run: dict[str, Any], label: str,
            planned: dict[str, list[str | None]]) -> dict[str, Any]:  # fmt: skip
    days = horizon_dates(ward)
    grid = repo.get_roster(conn, run["run_id"])
    links = chain(conn, run)[:-1]  # every version between the plan and this one
    rows = {
        r["run_id"]: {(a["nurse_id"], a["day"]): a for a in conn.execute(
            "SELECT nurse_id, day, cause FROM assignment WHERE run_id = ? AND changed = 1",
            (r["run_id"],))}
        for r in links
    }  # fmt: skip
    changes = []
    for nid, row in sorted(grid.items()):
        before = planned.get(nid) or [None] * len(row)
        for d, (old, new) in enumerate(zip(before, row, strict=False)):
            if old == new:
                continue
            iso = days[d].isoformat()
            by = next((r for r in links if (nid, iso) in rows[r["run_id"]]), links[0] if links
                      else run)  # fmt: skip
            hit = rows.get(by["run_id"], {}).get((nid, iso))
            cause = None if hit is None else hit["cause"]
            if cause is None and by["kind"] == "import":
                cause = "hand_edit"
            changes.append({
                "nurse_id": nid, "day": iso, "from_shift": old, "to_shift": new, "cause": cause,
                "run_id": by["run_id"], "trigger": by["trigger"],
                "request_id": _request_of(conn, by), "approved_by": by["published_by"],
                "approved_at": by["published_at"],
            })  # fmt: skip
    target = {(dm.day, dm.shift.value): dm.target_total for dm in ward.demand}
    return {
        "run": run_summary(run), "label": label, "changes": changes,
        "night_totals": [sum(1 for r in grid.values() if r[d] == "N") for d in range(len(days))],
        "night_targets": [target.get((x, "N"), 0) for x in days],
    }  # fmt: skip


@router.get("/versions", response_model=VersionsOut)
def versions(conn: Conn, period_start: date | None = None) -> VersionsOut:
    """Planned vs current (6.5c): every cell the current roster and each open proposal change
    against the period's planned version, with the cause and the decision behind it."""
    ward = require_ward(conn)
    start = period_start or ward.horizon_start
    pub = repo.get_published_run(conn, ward.id)
    days = horizon_dates(ward)
    if pub is None or str(pub["horizon_start"]) != start.isoformat():
        return VersionsOut(period_start=start, days=days, planned=None, versions=[])
    planned_run = chain(conn, pub)[-1]
    planned = repo.get_roster(conn, planned_run["run_id"])
    out = [version(conn, ward, pub, "current", planned)]
    for r in reversed(repo.list_runs(conn, ward.id)):
        trig = str(r["trigger"] or "")
        if r["status"] == RunStatus.PROPOSED.value and not trig.startswith("candidate:"):
            out.append(version(conn, ward, r, "proposal", planned))
    return VersionsOut.model_validate({
        "period_start": start, "days": days,
        "planned": RunSummary.model_validate(run_summary(planned_run)), "versions": out,
    })  # fmt: skip
