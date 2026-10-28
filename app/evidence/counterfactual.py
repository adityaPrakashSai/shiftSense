"""Counterfactual service (design doc 6.6): "what if this cell were different?"

`what_if(run_id, question)` rebuilds the run's SolveInput from `input_json`, applies the question's
forcings (or extra unavailable cells), and re-solves starting from the run's roster.

- Infeasible: diagnose (5.5) and return `IMPOSSIBLE` with `HARD:{groups}` and the core.
- Otherwise compare tier vectors with the stored run, in priority order: first worse tier ->
  `WORSE` / `TIER:{tier}`; first better tier -> `BETTER` / `BASE_NOT_OPTIMAL`; equal -> `EQUAL`
  / `TIE`. If any base stage was unproven, `:UNPROVEN` is appended.

Scopes: `full` (every non-frozen cell free) and `local` (only the nurses named in the question are
free; everyone else keeps the run's roster). A local result is an upper bound on the alternative's
cost, and says so in `scope`, so the explainer can say "keeping everyone else's roster fixed".
Results are cached in `counterfactual` by (run_id, canonical question).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from app.config import get_settings
from app.domain.models import Request, RequestKind
from app.solver.diagnose import groups, infeasible_core
from app.solver.input import Forcing, SolveInput
from app.solver.solve import solve
from app.store import repo

Scope = Literal["full", "local"]
BUDGET = {"full": (1.5, 4.0), "local": (1.0, 2.0)}  # (stage seconds, total seconds)
# a local what-if is small: one worker with a fixed seed makes it deterministic (the same
# question gives the same answer, so the explainer's cached responses replay)
WORKERS = {"local": 1}


@dataclass(frozen=True)
class Question:
    forcings: tuple[tuple[str, int, str | None], ...] = ()  # (nurse, day, shift | None = off)
    blocked: tuple[tuple[str, int, str | None], ...] = ()  # extra unavailable (nurse, day, shift)
    scope: Scope = "full"
    free: tuple[str, ...] = ()  # local scope: nurses besides the ones named above

    def key(self) -> str:
        body = {
            "forcings": sorted(list(f) for f in self.forcings),
            "blocked": sorted(list(b) for b in self.blocked),
            "scope": self.scope,
            "free": sorted(self.free),
        }
        return json.dumps(body, sort_keys=True, default=str)

    def nurses(self) -> set[str]:
        return {f[0] for f in self.forcings} | {b[0] for b in self.blocked} | set(self.free)


@dataclass
class Verdict:
    verdict: str  # IMPOSSIBLE | WORSE | BETTER | EQUAL
    reason_code: str  # HARD:{groups} | TIER:{tier} | BASE_NOT_OPTIMAL | TIE, maybe :UNPROVEN
    scope: Scope
    base: dict[str, int]
    alt: dict[str, int] | None
    core: list[str] = field(default_factory=list)
    changed_cells: int = 0  # cells that differ from the run's roster
    secs: float = 0.0
    base_proven: bool = True
    alt_proven: bool | None = None
    cached: bool = False
    note: str = ""


def _base(conn: sqlite3.Connection, run_id: str) -> tuple[dict[str, int], bool]:
    stages = repo.get_stages(conn, run_id)
    vec = {s["tier"]: int(s["best"]) for s in stages}
    return vec, all(bool(s["proven"]) for s in stages if s["status"] != "skip")


def compare(
    base: dict[str, int], alt: dict[str, int], priorities: tuple[str, ...]
) -> tuple[str, str]:
    for t in priorities:
        a, b = alt.get(t, 0), base.get(t, 0)
        if a > b:
            return "WORSE", f"TIER:{t}"
        if a < b:
            return "BETTER", "BASE_NOT_OPTIMAL"
    return "EQUAL", "TIE"


def alt_input(
    inp: SolveInput, roster: dict[tuple[str, int], str | None], q: Question
) -> SolveInput:
    # a sick-call run was produced with its replacement forced in; that is how it was chosen,
    # not a rule, so a what-if may move that nurse again
    kept = tuple(f for f in inp.forced if f.kind not in ("replacement", "what_if"))
    forced = [Forcing("what_if", n, d, s) for n, d, s in q.forcings]
    if q.scope == "local":
        free = q.nurses()
        forced += [
            Forcing("local", n.id, d, roster.get((n.id, d)))
            for n in inp.nurses
            if n.id not in free
            for d in range(inp.num_days)
        ]
    return replace(inp, forced=(*kept, *forced), unavailable=inp.unavailable | set(q.blocked))


def what_if(conn: sqlite3.Connection, run_id: str, q: Question) -> Verdict:
    hit = conn.execute(
        "SELECT * FROM counterfactual WHERE run_id = ? AND qkey = ?", (run_id, q.key())
    ).fetchone()
    if hit is not None:
        extra = json.loads(hit["question_json"]).get("result", {})
        return Verdict(
            verdict=hit["verdict"],
            reason_code=hit["reason_code"],
            scope=q.scope,
            base=json.loads(hit["base_json"]),
            alt=json.loads(hit["alt_json"]) if hit["alt_json"] else None,
            core=json.loads(hit["core_json"] or "[]"),
            changed_cells=int(hit["changed_cells"] or 0),
            secs=float(hit["secs"] or 0),
            base_proven=bool(extra.get("base_proven", True)),
            alt_proven=extra.get("alt_proven"),
            cached=True,
            note=str(extra.get("note", "")),
        )
    run = repo.get_run(conn, run_id)
    if run is None or not run["input_json"]:
        raise LookupError(run_id)
    t0 = time.perf_counter()
    inp = SolveInput.from_json(run["input_json"])
    grid = repo.get_roster(conn, run_id)
    roster = {(n, d): s for n, row in grid.items() for d, s in enumerate(row)}
    base, base_proven = _base(conn, run_id)
    alt_inp = alt_input(inp, roster, q)
    stage_s, total_s = BUDGET[q.scope]
    workers = WORKERS.get(q.scope, get_settings().solver_workers)
    res = solve(alt_inp, stage_seconds=stage_s, workers=workers, total_seconds=total_s, hint=roster)
    if res.status == "solved":
        alt = res.tier_vector()
        verdict, code = compare(base, alt, inp.priorities)
        changed = sum(1 for k, s in res.roster.items() if roster.get(k) != s)
        out = Verdict(verdict, code, q.scope, base, alt, [], changed, 0.0, base_proven, res.proven)
    else:
        core = infeasible_core(alt_inp) or []
        gs = groups(core) or ["unknown"]
        out = Verdict("IMPOSSIBLE", "HARD:" + ",".join(gs), q.scope, base, None, core)
    if not base_proven and out.verdict != "IMPOSSIBLE":
        out.reason_code += ":UNPROVEN"
    if q.scope == "local":
        out.note = "keeping everyone else's roster fixed (an upper bound on the alternative)"
    out.secs = round(time.perf_counter() - t0, 3)
    conn.execute(
        "INSERT OR REPLACE INTO counterfactual(run_id, qkey, question_json, verdict, reason_code, "
        "base_json, alt_json, core_json, changed_cells, secs, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            q.key(),
            json.dumps(
                {
                    "question": asdict(q),
                    "result": {
                        "base_proven": base_proven,
                        "alt_proven": out.alt_proven,
                        "note": out.note,
                    },
                },
                default=str,
            ),  # fmt: skip
            out.verdict,
            out.reason_code,
            json.dumps(base),
            None if out.alt is None else json.dumps(out.alt),
            json.dumps(out.core),
            out.changed_cells,
            out.secs,
            repo.now_iso(),
        ),
    )
    conn.commit()
    return out


def grant_question(r: Request, inp: SolveInput, start: Any) -> Question | None:
    """The what-if that grants a soft request in full: day off -> off, avoid -> not that shift,
    wanted -> that shift."""
    days = [d for d in (day_index_from(start, x) for x in r.dates) if 0 <= d < inp.num_days]
    shifts = [s.value for s in r.shifts or []]
    if not days:
        return None
    if r.kind is RequestKind.DAY_OFF:
        return Question(blocked=tuple((r.nurse_id, d, None) for d in days))
    if r.kind is RequestKind.SHIFT_AVOID:
        return Question(blocked=tuple((r.nurse_id, d, s) for d in days for s in shifts))
    if r.kind is RequestKind.SHIFT_WANTED and shifts:
        return Question(forcings=tuple((r.nurse_id, d, shifts[0]) for d in days))
    return None


def day_index_from(start: Any, d: Any) -> int:
    return int((d - start).days)


def explain_denied(conn: sqlite3.Connection, run_id: str, budget_s: float = 20.0) -> int:
    """Fill reason codes for a run's denied soft requests (M8): each is granted in a what-if and
    the verdict says what granting it would cost. Time-boxed; returns how many were filled."""
    run = repo.get_run(conn, run_id)
    ward = repo.get_ward(conn, None if run is None else str(run["ward_id"]))
    if run is None or ward is None or not run["input_json"]:
        return 0
    inp = SolveInput.from_json(run["input_json"])
    t0, done = time.perf_counter(), 0
    for o in repo.get_request_outcomes(conn, run_id):
        unexplained = str(o["reason_code"] or "").startswith("PREF_")
        if o["status"] != "DENIED" or not unexplained or time.perf_counter() - t0 > budget_s:
            continue
        r = repo.get_request(conn, o["request_id"])
        q = None if r is None else grant_question(r, inp, ward.horizon_start)
        if q is None:
            continue
        v = what_if(conn, run_id, q)
        rule = str(o["reason_code"] or "")
        detail = f"{rule} {o['detail'] or ''}".strip()
        conn.execute(
            "UPDATE request_outcome SET reason_code = ?, detail = ? WHERE run_id = ? "
            "AND request_id = ?",
            (v.reason_code, f"{detail}; granting it: {v.verdict} {v.reason_code}", run_id,
             o["request_id"]),
        )  # fmt: skip
        conn.commit()
        done += 1
    return done
