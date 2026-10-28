"""Explainer tools (design doc 7.4): read-only views of the evidence store and the counterfactual
service. Each tool takes JSON arguments and returns a JSON-able dict; the agent wraps it as
{"call_id": "c3", "tool": ..., "result": {...}} so a claim can cite `path` inside `result`.

`nurse` arguments accept ids or names (the intake matcher). `run_id` defaults to the run the
question is about (the context run, else the published run).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.domain.models import Ward
from app.evidence.counterfactual import Question, what_if
from app.intake.resolve_names import resolve
from app.planner.build_input import day_index
from app.solver.input import SolveInput
from app.solver.rules import rule_check
from app.store import repo


class ToolError(Exception):
    """A bad argument; returned to the model as {"error": ...} so it can correct itself."""


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    ward: Ward
    run_id: str  # default run for tools that take one
    asker_id: str


def _nurse(ctx: ToolContext, name: Any) -> str:
    m = resolve(str(name or ""), ctx.ward.nurses, None)
    if m.nurse_id is None:
        hint = f"; candidates {m.candidates}" if m.candidates else ""
        raise ToolError(f"no single nurse matches {name!r}{hint}")
    return m.nurse_id


def _name(ctx: ToolContext, nurse_id: str | None) -> str | None:
    return next((n.name for n in ctx.ward.nurses if n.id == nurse_id), nurse_id)


def _run(ctx: ToolContext, run_id: Any) -> dict[str, Any]:
    rid = str(run_id) if run_id else ctx.run_id
    run = repo.get_run(ctx.conn, rid)
    if run is None:
        raise ToolError(f"no run {rid}")
    return run


def _day(ctx: ToolContext, iso: Any) -> tuple[date, int]:
    try:
        d = date.fromisoformat(str(iso))
    except ValueError as exc:
        raise ToolError(f"date must be YYYY-MM-DD, got {iso!r}") from exc
    i = day_index(ctx.ward, d)
    if not 0 <= i < ctx.ward.horizon_days:
        raise ToolError(f"{d} is outside the roster period")
    return d, i


def _shift(s: Any) -> str:
    v = str(s or "").upper()[:1]
    if v not in ("M", "E", "N"):
        raise ToolError(f"shift must be M, E or N, got {s!r}")
    return v


def _roster(ctx: ToolContext, run_id: str) -> dict[tuple[str, int], str | None]:
    grid = repo.get_roster(ctx.conn, run_id)
    return {(n, d): s for n, row in grid.items() for d, s in enumerate(row)}


# ---------------------------------------------------------------- the 7 tools


def get_run_summary(ctx: ToolContext, run_id: Any = None) -> dict[str, Any]:
    run = _run(ctx, run_id)
    rid = str(run["run_id"])
    stages = repo.get_stages(ctx.conn, rid)
    outcomes = repo.get_request_outcomes(ctx.conn, rid)
    soft = [o for o in outcomes if o["reason_code"] != "HARD"]
    return {
        "run_id": rid,
        "status": run["status"],
        "kind": run["kind"],
        "priorities": list(run["priorities"]),
        "stages": [
            {
                "tier": s["tier"],
                "best": s["best"],
                "bound": s["bound"],
                "proven": bool(s["proven"]),
                "skipped": s["status"] == "skip",
            }
            for s in stages
        ],  # fmt: skip
        "proven": all(bool(s["proven"]) for s in stages),
        "changed_cells": run["changed_cells"],
        "requests": {
            "granted": sum(o["status"] == "GRANTED" for o in soft),
            "denied": sum(o["status"] == "DENIED" for o in soft),
        },
    }


def get_assignment_evidence(
    ctx: ToolContext, nurse: Any, date: Any, run_id: Any = None
) -> dict[str, Any]:
    run = _run(ctx, run_id)
    rid = str(run["run_id"])
    n = _nurse(ctx, nurse)
    d, i = _day(ctx, date)
    row = ctx.conn.execute(
        "SELECT shift, prev_shift, changed, cause FROM assignment WHERE run_id = ? AND "
        "nurse_id = ? AND day = ?",
        (rid, n, d.isoformat()),
    ).fetchone()
    costs = [
        {"tier": c["tier"], "rule": c["rule"], "amount": c["amount"], "weight": c["weight"],
         "detail": c["detail"]}
        for c in ctx.conn.execute(
            "SELECT * FROM cost WHERE run_id = ? AND day = ? AND (nurse_id = ? OR nurse_id IS NULL)"
            " ORDER BY tier, rule", (rid, d.isoformat(), n))
    ]  # fmt: skip
    outcomes = {o["request_id"]: o for o in repo.get_request_outcomes(ctx.conn, rid)}
    reqs = [
        {"id": r.id, "kind": r.kind.value, "status": r.status.value,
         "outcome": (outcomes.get(r.id) or {}).get("status"),
         "reason_code": (outcomes.get(r.id) or {}).get("reason_code")}
        for r in repo.list_requests(ctx.conn, ctx.ward.id, nurse_id=n)
        if d in r.dates
    ]  # fmt: skip
    inp = SolveInput.from_json(run["input_json"])
    roster = _roster(ctx, rid)
    current = None if row is None else row["shift"]
    others = []
    for s in ("M", "E", "N", None):
        if s == current:
            continue
        rules = rule_check(roster, inp, n, i, s)
        others.append({"shift": s or "off", "allowed": not rules, "rules": rules})
    return {
        "run_id": rid,
        "nurse": n,
        "name": _name(ctx, n),
        "date": d.isoformat(),
        "shift": current or "off",
        "prev_shift": None if row is None else row["prev_shift"],
        "changed": None if row is None or row["changed"] is None else bool(row["changed"]),
        "cause": None if row is None else row["cause"],
        "costs": costs,
        "requests": reqs,
        "other_shifts": others,
    }


def get_request_outcome(
    ctx: ToolContext, request_id: Any = None, nurse: Any = None, run_id: Any = None
) -> dict[str, Any]:
    run = _run(ctx, run_id)
    rid = str(run["run_id"])
    outcomes = {o["request_id"]: o for o in repo.get_request_outcomes(ctx.conn, rid)}
    if request_id:
        r = repo.get_request(ctx.conn, str(request_id))
        if r is None:
            raise ToolError(f"no request {request_id}")
        reqs = [r]
    elif nurse:
        reqs = repo.list_requests(ctx.conn, ctx.ward.id, nurse_id=_nurse(ctx, nurse))
    else:
        raise ToolError("give request_id or nurse")

    def one(r: Any) -> dict[str, Any]:
        o = outcomes.get(r.id) or {}
        cf = None
        detail = str(o.get("detail") or "")
        if "granting it:" in detail:
            cf = detail.split("granting it:", 1)[1].strip()
        return {
            "request_id": r.id,
            "nurse": r.nurse_id,
            "name": _name(ctx, r.nurse_id),
            "kind": r.kind.value,
            "dates": [x.isoformat() for x in r.dates],
            "shifts": None if r.shifts is None else [s.value for s in r.shifts],
            "request_status": r.status.value,
            "outcome": o.get("status"),
            "reason_code": o.get("reason_code"),
            "detail": o.get("detail"),
            "counterfactual": cf,
        }

    return {"run_id": rid, "requests": [one(r) for r in reqs]}


def compare_candidates(
    ctx: ToolContext,
    date: Any,
    shift: Any,
    nurse_a: Any,
    nurse_b: Any,
    scope: Any = "local",
    run_id: Any = None,
) -> dict[str, Any]:
    """Why A (on the cell) and not B: B takes (date, shift) and A is off that day instead."""
    run = _run(ctx, run_id)
    rid = str(run["run_id"])
    d, i = _day(ctx, date)
    s = _shift(shift)
    a, b = _nurse(ctx, nurse_a), _nurse(ctx, nurse_b)
    sc = "full" if str(scope) == "full" else "local"
    roster = _roster(ctx, rid)
    inp = SolveInput.from_json(run["input_json"])
    q = Question(forcings=((b, i, s), (a, i, None)), scope=sc)  # type: ignore[arg-type]
    v = what_if(ctx.conn, rid, q)
    delta = {} if v.alt is None else {
        t: v.alt.get(t, 0) - v.base.get(t, 0) for t in inp.priorities
        if v.alt.get(t, 0) != v.base.get(t, 0)
    }  # fmt: skip
    return {
        "run_id": rid,
        "date": d.isoformat(),
        "shift": s,
        "nurse_a": a,
        "name_a": _name(ctx, a),
        "nurse_b": b,
        "name_b": _name(ctx, b),
        "a_assigned": roster.get((a, i)) == s,
        "b_current_shift": roster.get((b, i)) or "off",
        "b_rule_check": rule_check(roster, inp, b, i, s, nurse_only=True),
        "verdict": v.verdict,
        "reason_code": v.reason_code,
        "base": v.base,
        "alt": v.alt,
        "delta": delta,
        "core": v.core,
        "changed_cells": v.changed_cells,
        "scope": v.scope,
        "note": v.note,
        "proven": v.base_proven and bool(v.alt_proven),
    }


def get_proposal(ctx: ToolContext, run_id: Any = None) -> dict[str, Any]:
    p = None
    if run_id:
        p = repo.proposal_for_run(ctx.conn, str(run_id))
    if p is None:
        p = repo.proposal_for_run(ctx.conn, ctx.run_id)
    if p is None:
        opened = repo.list_proposals(ctx.conn, ctx.ward.id, open_only=True)
        p = opened[0] if opened else None
    if p is None:
        raise ToolError("no proposal for this run")
    rk = p["ranking"]
    return {
        "run_id": p["run_id"],
        "open_cell": rk.get("open_cell"),
        "candidates": [
            {
                k: c.get(k)
                for k in (
                    "nurse",
                    "rank",
                    "standby",
                    "kind",
                    "tiers",
                    "delta_vs_best",
                    "changed_cells",
                    "proven",
                    "tie",
                    "tie_break",
                    "reason_code",
                )
            }
            | {"name": _name(ctx, c.get("nurse"))}
            for c in rk.get("candidates", [])
        ],  # fmt: skip
        "excluded": [e | {"name": _name(ctx, e["nurse"])} for e in rk.get("excluded", [])],
        "baseline": rk.get("baseline"),
        "coverage_worse": rk.get("coverage_worse", False),
        "chosen_nurse": p["chosen_nurse"],
    }


def get_fairness_history(ctx: ToolContext, nurse: Any) -> dict[str, Any]:
    n = _nurse(ctx, nurse)
    hist = repo.get_history(ctx.conn, ctx.ward.id)
    pub = repo.get_published_run(ctx.conn, ctx.ward.id)
    cur = (
        {}
        if pub is None
        else {r["nurse_id"]: r for r in repo.get_fairness(ctx.conn, pub["run_id"])}
    )
    regular = [x.id for x in ctx.ward.nurses if not x.standby and x.role.value != "ans"]

    def avg(key: str, rows: dict[str, Any]) -> float:
        vals = [float((rows.get(i) or {}).get(key, 0) or 0) for i in regular]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    h = hist.nurses.get(n)
    hist_rows = {i: r.model_dump() for i, r in hist.nurses.items()}
    me = cur.get(n) or {}
    return {
        "nurse": n,
        "name": _name(ctx, n),
        "earlier_periods": {
            "periods": hist.periods,
            "nights": 0 if h is None else h.nights,
            "weekends": 0 if h is None else h.weekends,
            "denied": 0 if h is None else h.denied,
            "ward_average": {k: avg(k, hist_rows) for k in ("nights", "weekends", "denied")},
        },
        "current_period": {
            "run_id": None if pub is None else pub["run_id"],
            "nights": me.get("nights", 0),
            "weekend_shifts": me.get("weekend_shifts", 0),
            "total_shifts": me.get("total_shifts", 0),
            "requests_denied": me.get("requests_denied", 0),
            "ward_average": {k: avg(k, cur) for k in ("nights", "weekend_shifts", "total_shifts")},
        },
    }


def find_nurse(ctx: ToolContext, name: Any) -> dict[str, Any]:
    m = resolve(str(name or ""), ctx.ward.nurses, None)
    ids = [m.nurse_id] if m.nurse_id else m.candidates
    return {"query": name, "matches": [{"id": i, "name": _name(ctx, i)} for i in ids]}


# ---------------------------------------------------------------- registry and JSON schemas


def _spec(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


STR = {"type": "string"}
RUN = {"type": "string", "description": "run id; default: the run being discussed"}
NURSE = {"type": "string", "description": "nurse id or name"}
DATE = {"type": "string", "description": "YYYY-MM-DD"}
SHIFT = {"type": "string", "enum": ["M", "E", "N"]}

SPECS: list[dict[str, Any]] = [
    _spec("get_run_summary", "Tier results (best, bound, proven), changed cells and request "
          "counts for a run.", {"run_id": RUN}, []),
    _spec("get_assignment_evidence", "One nurse's cell on one date: shift, previous shift, "
          "whether it changed and why, cost rows that day, the nurse's requests that day, "
          "and which hard rules each other shift would break.",
          {"nurse": NURSE, "date": DATE, "run_id": RUN}, ["nurse", "date"]),
    _spec("get_request_outcome", "Outcome of a request (or all of a nurse's requests): granted "
          "or denied, reason code, and what granting it would cost.",
          {"request_id": STR, "nurse": NURSE, "run_id": RUN}, []),
    _spec("compare_candidates", "Why nurse_a and not nurse_b on (date, shift): re-solves with "
          "nurse_b on that shift and nurse_a off. scope 'local' keeps everyone else's roster "
          "fixed (fast); 'full' frees all non-frozen cells.",
          {"date": DATE, "shift": SHIFT, "nurse_a": NURSE, "nurse_b": NURSE,
           "scope": {"type": "string", "enum": ["local", "full"]}, "run_id": RUN},
          ["date", "shift", "nurse_a", "nurse_b"]),
    _spec("get_proposal", "A sick-call proposal: the open cell, ranked replacement candidates "
          "with tier vectors and changed cells, and excluded nurses with the hard rules that "
          "block them.", {"run_id": RUN}, []),
    _spec("get_fairness_history", "Nights, weekend shifts and denied requests for a nurse in "
          "earlier periods and the current one, with the ward averages.", {"nurse": NURSE},
          ["nurse"]),
    _spec("find_nurse", "Nurse ids matching a name.", {"name": STR}, ["name"]),
]  # fmt: skip

TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_run_summary": get_run_summary,
    "get_assignment_evidence": get_assignment_evidence,
    "get_request_outcome": get_request_outcome,
    "compare_candidates": compare_candidates,
    "get_proposal": get_proposal,
    "get_fairness_history": get_fairness_history,
    "find_nurse": find_nurse,
}


def call(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool; bad arguments come back as {"error": ...} instead of raising."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}; tools: {sorted(TOOLS)}"}
    try:
        return fn(ctx, **{k: v for k, v in args.items() if v is not None})
    except ToolError as exc:
        return {"error": str(exc)}
    except TypeError as exc:
        return {"error": f"bad arguments: {exc}"}
