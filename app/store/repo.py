"""SQLite access: one small function per query, no ORM (design doc 11.4 rule 8)."""

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from app.domain.models import (
    Nurse,
    Request,
    RequestStatus,
    RunKind,
    RunStatus,
    ShiftCode,
    Ward,
    WardHistory,
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- ward and nurses


def save_ward(conn: sqlite3.Connection, ward: Ward) -> None:
    conn.execute(
        "INSERT INTO ward(id, json) VALUES(?, ?) "
        "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
        (ward.id, ward.model_dump_json()),
    )
    conn.commit()


def get_ward(conn: sqlite3.Connection, ward_id: str | None = None) -> Ward | None:
    """The ward with this id, or the only ward in the database when no id is given."""
    if ward_id is None:
        row = conn.execute("SELECT json FROM ward ORDER BY id LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT json FROM ward WHERE id = ?", (ward_id,)).fetchone()
    return None if row is None else Ward.model_validate_json(row["json"])


def list_nurses(conn: sqlite3.Connection, ward_id: str) -> list[Nurse]:
    ward = get_ward(conn, ward_id)
    return [] if ward is None else ward.nurses


def get_nurse(conn: sqlite3.Connection, ward_id: str, nurse_id: str) -> Nurse | None:
    for n in list_nurses(conn, ward_id):
        if n.id == nurse_id:
            return n
    return None


def save_history(conn: sqlite3.Connection, ward_id: str, history: WardHistory) -> None:
    conn.execute(
        "INSERT INTO ward_history(ward_id, periods, json) VALUES(?, ?, ?) "
        "ON CONFLICT(ward_id) DO UPDATE SET periods = excluded.periods, json = excluded.json",
        (ward_id, history.periods, history.model_dump_json()),
    )
    conn.commit()


def get_history(conn: sqlite3.Connection, ward_id: str) -> WardHistory:
    row = conn.execute("SELECT json FROM ward_history WHERE ward_id = ?", (ward_id,)).fetchone()
    return WardHistory() if row is None else WardHistory.model_validate_json(row["json"])


# ---------------------------------------------------------------- requests


def _request_row(r: sqlite3.Row) -> Request:
    return Request(
        id=r["id"],
        nurse_id=r["nurse_id"],
        kind=r["kind"],
        dates=json.loads(r["dates_json"]),
        shifts=json.loads(r["shifts_json"]) if r["shifts_json"] else None,
        leave_type=r["leave_type"],
        counterpart_id=r["counterpart_id"],
        counterpart_dates=json.loads(r["counterpart_dates_json"] or "[]"),
        counterpart_shifts=(
            json.loads(r["counterpart_shifts_json"]) if r["counterpart_shifts_json"] else None
        ),
        target_total=r["target_total"],
        ref_request_id=r["ref_request_id"],
        hard=bool(r["hard"]),
        weight=r["weight"],
        status=r["status"],
        source_message_id=r["source_message_id"],
        created_at=r["created_at"],
    )


def insert_request(
    conn: sqlite3.Connection, ward_id: str, req: Request, *, edit_json: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO request(id, ward_id, nurse_id, kind, dates_json, shifts_json, leave_type, "
        "counterpart_id, hard, weight, status, source_message_id, edit_json, created_at, "
        "counterpart_dates_json, counterpart_shifts_json, target_total, ref_request_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            req.id,
            ward_id,
            req.nurse_id,
            req.kind.value,
            json.dumps([d.isoformat() for d in req.dates]),
            None if req.shifts is None else json.dumps([s.value for s in req.shifts]),
            None if req.leave_type is None else req.leave_type.value,
            req.counterpart_id,
            int(req.hard),
            req.weight,
            req.status.value,
            req.source_message_id,
            edit_json,
            req.created_at.isoformat(),
            json.dumps([d.isoformat() for d in req.counterpart_dates]),
            None
            if req.counterpart_shifts is None
            else json.dumps([s.value for s in req.counterpart_shifts]),
            req.target_total,
            req.ref_request_id,
        ),
    )
    conn.commit()


def get_request(conn: sqlite3.Connection, request_id: str) -> Request | None:
    row = conn.execute("SELECT * FROM request WHERE id = ?", (request_id,)).fetchone()
    return None if row is None else _request_row(row)


def list_requests(
    conn: sqlite3.Connection,
    ward_id: str,
    *,
    nurse_id: str | None = None,
    status: RequestStatus | None = None,
) -> list[Request]:
    sql = "SELECT * FROM request WHERE ward_id = ?"
    args: list[Any] = [ward_id]
    if nurse_id is not None:
        sql += " AND nurse_id = ?"
        args.append(nurse_id)
    if status is not None:
        sql += " AND status = ?"
        args.append(status.value)
    return [_request_row(r) for r in conn.execute(sql + " ORDER BY id", args)]


def set_swap_counterpart(
    conn: sqlite3.Connection, request_id: str, nurse_id: str, day: date, shift: str
) -> None:
    """An open swap gains its partner: the accepter's cell becomes the return leg."""
    conn.execute(
        "UPDATE request SET counterpart_id = ?, counterpart_dates_json = ?, "
        "counterpart_shifts_json = ? WHERE id = ?",
        (nurse_id, json.dumps([day.isoformat()]), json.dumps([shift]), request_id),
    )
    conn.commit()


def set_request_status(conn: sqlite3.Connection, request_id: str, status: RequestStatus) -> None:
    confirmed = now_iso() if status is RequestStatus.ACTIVE else None
    cur = conn.execute(
        "UPDATE request SET status = ?, confirmed_at = COALESCE(?, confirmed_at) WHERE id = ?",
        (status.value, confirmed, request_id),
    )
    if cur.rowcount == 0:
        raise KeyError(request_id)
    conn.commit()


def next_request_id(conn: sqlite3.Connection) -> str:
    """Next "R-0042"-style id, after every numeric id already stored."""
    best = 0
    for (rid,) in conn.execute("SELECT id FROM request"):
        tail = rid.rsplit("-", 1)[-1]
        if tail.isdigit():
            best = max(best, int(tail))
    return f"R-{best + 1:04d}"


def get_request_edit(conn: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT edit_json FROM request WHERE id = ?", (request_id,)).fetchone()
    return None if row is None or not row[0] else dict(json.loads(row[0]))


def requests_from_message(conn: sqlite3.Connection, message_id: str) -> list[Request]:
    rows = conn.execute(
        "SELECT * FROM request WHERE source_message_id = ? ORDER BY id", (message_id,)
    )
    return [_request_row(r) for r in rows]


def latest_outcomes(conn: sqlite3.Connection, ward_id: str) -> dict[str, dict[str, Any]]:
    """request_id -> its outcome in the newest solved run that has one."""
    rows = conn.execute(
        "SELECT o.* FROM request_outcome o JOIN run r ON r.run_id = o.run_id "
        "WHERE r.ward_id = ? ORDER BY r.created_at, r.run_id",
        (ward_id,),
    )
    return {str(r["request_id"]): {**dict(r)} for r in rows}


def delete_request(conn: sqlite3.Connection, request_id: str) -> None:
    conn.execute("DELETE FROM request WHERE id = ?", (request_id,))
    conn.commit()


# ---------------------------------------------------------------- messages (intake, 7.3)


def insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    ward_id: str,
    nurse_id: str,
    body: str,
    created_at: str,
    channel: str = "text",
) -> None:
    conn.execute(
        "INSERT INTO message(id, ward_id, nurse_id, channel, body, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (message_id, ward_id, nurse_id, channel, body, created_at),
    )
    conn.commit()


def set_message_reply(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    language: str | None,
    parse: dict[str, Any] | None,
    reply: str,
    reply_kind: str,
    request_ids: list[str],
    explanation_id: str | None = None,
) -> None:
    conn.execute(
        "UPDATE message SET language = ?, parse_json = ?, reply = ?, reply_kind = ?, "
        "request_ids_json = ?, explanation_id = ? WHERE id = ?",
        (
            language,
            None if parse is None else json.dumps(parse, ensure_ascii=False),
            reply,
            reply_kind,
            json.dumps(request_ids),
            explanation_id,
            message_id,
        ),
    )
    conn.commit()


def _message_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "message_id": r["id"],
        "nurse_id": r["nurse_id"],
        "channel": r["channel"],
        "body": r["body"],
        "created_at": r["created_at"],
        "language": r["language"],
        "parse": json.loads(r["parse_json"]) if r["parse_json"] else None,
        "reply": r["reply"],
        "reply_kind": r["reply_kind"],
        "request_ids": json.loads(r["request_ids_json"] or "[]"),
        "explanation_id": r["explanation_id"],
    }


def get_message(conn: sqlite3.Connection, message_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM message WHERE id = ?", (message_id,)).fetchone()
    return None if row is None else _message_row(row)


def list_messages(
    conn: sqlite3.Connection, ward_id: str, nurse_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    """A sender's newest messages, oldest first."""
    rows = conn.execute(
        "SELECT * FROM (SELECT rowid AS rid, * FROM message WHERE ward_id = ? AND nurse_id = ? "
        "ORDER BY rid DESC LIMIT ?) ORDER BY rid",
        (ward_id, nurse_id, limit),
    ).fetchall()
    return [_message_row(r) for r in rows]


# ---------------------------------------------------------------- runs and assignments


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ward_id: str,
    kind: RunKind,
    status: RunStatus,
    trigger: str,
    horizon_start: date,
    priorities: Iterable[str],
    input_json: str,
    input_hash: str,
    parent_run: str | None = None,
    frozen_before_day: int = 0,
    total_secs: float | None = None,
    changed_cells: int | None = None,
    published_by: str | None = None,
) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO run(run_id, ward_id, parent_run, kind, status, trigger, created_at, "
        "horizon_start, priorities, frozen_before_day, input_json, input_hash, total_secs, "
        "changed_cells, published_at, published_by) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?)",
        (
            run_id,
            ward_id,
            parent_run,
            kind.value,
            status.value,
            trigger,
            ts,
            horizon_start.isoformat(),
            json.dumps(list(priorities)),
            frozen_before_day,
            input_json,
            input_hash,
            total_secs,
            changed_cells,
            ts if status in (RunStatus.PUBLISHED, RunStatus.SUPERSEDED) else None,
            published_by,
        ),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else dict(row)


def list_runs(conn: sqlite3.Connection, ward_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM run WHERE ward_id = ? ORDER BY created_at, run_id", (ward_id,)
    )
    return [dict(r) for r in rows]


def get_published_run(conn: sqlite3.Connection, ward_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM run WHERE ward_id = ? AND status = ?", (ward_id, RunStatus.PUBLISHED.value)
    ).fetchone()
    return None if row is None else dict(row)


def insert_assignments(
    conn: sqlite3.Connection,
    run_id: str,
    roster: dict[str, list[str | None]],
    horizon_start: date,
    *,
    previous: dict[str, list[str | None]] | None = None,
    causes: dict[tuple[str, int], str] | None = None,
) -> None:
    """Write one row per nurse x day. `roster` maps nurse id -> shift code per day (None = off);
    `causes` maps (nurse id, day index) of a changed cell to its 6.3 cause."""
    rows = []
    for nurse_id, cells in roster.items():
        prev_cells = None if previous is None else previous.get(nurse_id)
        for d, shift in enumerate(cells):
            day = date.fromordinal(horizon_start.toordinal() + d).isoformat()
            prev = None if prev_cells is None else prev_cells[d]
            changed = None if prev_cells is None else int(prev != shift)
            cause = None if causes is None else causes.get((nurse_id, d))
            rows.append((run_id, nurse_id, day, shift, prev, changed, cause))
    conn.executemany(
        "INSERT INTO assignment(run_id, nurse_id, day, shift, prev_shift, changed, cause) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def get_roster(conn: sqlite3.Connection, run_id: str) -> dict[str, list[str | None]]:
    """Nurse id -> shift code per day, in day order."""
    out: dict[str, list[str | None]] = {}
    rows = conn.execute(
        "SELECT nurse_id, shift FROM assignment WHERE run_id = ? ORDER BY nurse_id, day", (run_id,)
    )
    for r in rows:
        out.setdefault(r["nurse_id"], []).append(
            None if r["shift"] is None else ShiftCode(r["shift"]).value
        )
    return out


def update_run(conn: sqlite3.Connection, run_id: str, **fields: Any) -> None:
    allowed = {
        "status",
        "total_secs",
        "changed_cells",
        "published_at",
        "published_by",
        "parent_run",
        "input_json",
        "input_hash",
        "trigger",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update run fields {sorted(bad)}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE run SET {cols} WHERE run_id = ?", (*fields.values(), run_id))  # noqa: S608
    conn.commit()


def set_published(conn: sqlite3.Connection, ward_id: str, run_id: str, by: str) -> str | None:
    """Publish `run_id` and supersede the previously published run, in one transaction.
    Every other proposed run of the ward is superseded too: its parent is no longer the published
    run (4.4 invariant). Returns the superseded run id."""
    old = get_published_run(conn, ward_id)
    ts = now_iso()
    with conn:
        if old is not None:
            conn.execute(
                "UPDATE run SET status = ? WHERE run_id = ?",
                (RunStatus.SUPERSEDED.value, old["run_id"]),
            )
        conn.execute(
            "UPDATE run SET status = ? WHERE ward_id = ? AND status = ? AND run_id != ?",
            (RunStatus.SUPERSEDED.value, ward_id, RunStatus.PROPOSED.value, run_id),
        )
        conn.execute(
            "UPDATE run SET status = ?, published_at = ?, published_by = ? WHERE run_id = ?",
            (RunStatus.PUBLISHED.value, ts, by, run_id),
        )
    return None if old is None else str(old["run_id"])


def insert_stages(conn: sqlite3.Connection, run_id: str, rows: Iterable[tuple[Any, ...]]) -> None:
    """rows: (rank, tier, status, best, bound, proven, secs, locked)."""
    conn.executemany(
        "INSERT INTO stage(run_id, rank, tier, status, best, bound, proven, secs, locked) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_id, *r) for r in rows],
    )
    conn.commit()


def get_stages(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM stage WHERE run_id = ? ORDER BY rank", (run_id,))
    return [dict(r) for r in rows]


def insert_costs(conn: sqlite3.Connection, run_id: str, rows: Iterable[tuple[Any, ...]]) -> None:
    """rows: (nurse_id, day, tier, rule, amount, weight, ref, detail)."""
    conn.executemany(
        "INSERT INTO cost(run_id, nurse_id, day, tier, rule, amount, weight, ref, detail) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_id, *r) for r in rows],
    )
    conn.commit()


def get_costs(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM cost WHERE run_id = ?", (run_id,))]


def insert_request_outcomes(
    conn: sqlite3.Connection, run_id: str, rows: Iterable[tuple[Any, ...]]
) -> None:
    """rows: (request_id, status, reason_code, detail)."""
    conn.executemany(
        "INSERT INTO request_outcome(run_id, request_id, status, reason_code, detail) "
        "VALUES(?, ?, ?, ?, ?)",
        [(run_id, *r) for r in rows],
    )
    conn.commit()


def get_request_outcomes(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM request_outcome WHERE run_id = ? ORDER BY request_id", (run_id,)
    )
    return [dict(r) for r in rows]


def insert_fairness(conn: sqlite3.Connection, run_id: str, rows: Iterable[tuple[Any, ...]]) -> None:
    """rows: (nurse_id, nights, weekend_shifts, total_shifts, requests_made, requests_denied)."""
    conn.executemany(
        "INSERT INTO fairness(run_id, nurse_id, nights, weekend_shifts, total_shifts, "
        "requests_made, requests_denied) VALUES(?, ?, ?, ?, ?, ?, ?)",
        [(run_id, *r) for r in rows],
    )
    conn.commit()


def get_fairness(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM fairness WHERE run_id = ? ORDER BY nurse_id", (run_id,))
    return [dict(r) for r in rows]


def insert_event(
    conn: sqlite3.Connection,
    *,
    ward_id: str,
    run_id: str | None,
    actor: str,
    kind: str,
    stage: str | None,
    payload_json: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO event(ward_id, run_id, ts, actor, kind, stage, payload_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (ward_id, run_id, now_iso_ms(), actor, kind, stage, payload_json),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_events(
    conn: sqlite3.Connection,
    ward_id: str,
    *,
    run_id: str | None = None,
    after_seq: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM event WHERE ward_id = ? AND seq > ?"
    args: list[Any] = [ward_id, after_seq]
    if run_id is not None:
        sql += " AND run_id = ?"
        args.append(run_id)
    rows = conn.execute(sql + " ORDER BY seq LIMIT ?", (*args, limit))
    return [{**dict(r), "payload": json.loads(r["payload_json"] or "{}")} for r in rows]


def list_last_events(
    conn: sqlite3.Connection, ward_id: str, *, run_id: str | None = None, n: int = 1
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM event WHERE ward_id = ?"
    args: list[Any] = [ward_id]
    if run_id is not None:
        sql += " AND run_id = ?"
        args.append(run_id)
    rows = conn.execute(sql + " ORDER BY seq DESC LIMIT ?", (*args, n)).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload_json"] or "{}")} for r in reversed(rows)]


def now_iso_ms() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- proposals (6.4)


def insert_proposal(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    request_id: str | None,
    open_cells: list[dict[str, Any]],
    ranking: dict[str, Any],
) -> None:
    """`ranking` is the 6.4 JSON (open cell, ranked candidates, excluded nurses, timing)."""
    conn.execute(
        "INSERT INTO proposal(run_id, request_id, open_cells_json, candidates_json) "
        "VALUES(?, ?, ?, ?)",
        (
            run_id,
            request_id,
            json.dumps(open_cells, ensure_ascii=False),
            json.dumps(ranking, ensure_ascii=False),
        ),
    )
    conn.commit()


def _proposal_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": r["run_id"],
        "request_id": r["request_id"],
        "open_cells": json.loads(r["open_cells_json"] or "[]"),
        "ranking": json.loads(r["candidates_json"] or "{}"),
        "chosen_nurse": r["chosen_nurse"],
        "message_draft": r["message_draft"],
        "replacement_status": r["replacement_status"],
    }


def get_proposal(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM proposal WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else _proposal_row(row)


def list_proposals(
    conn: sqlite3.Connection, ward_id: str, *, open_only: bool = False
) -> list[dict[str, Any]]:
    """Newest first. Open = its run is still proposed."""
    sql = (
        "SELECT p.*, r.status AS run_status, r.created_at FROM proposal p "
        "JOIN run r ON r.run_id = p.run_id WHERE r.ward_id = ?"
    )
    if open_only:
        sql += " AND r.status = 'proposed'"
    rows = conn.execute(sql + " ORDER BY r.created_at DESC, p.run_id DESC", (ward_id,))
    return [{**_proposal_row(r), "run_status": r["run_status"]} for r in rows]


def proposal_for_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """The proposal whose top run or one of whose candidate runs is `run_id`."""
    hit = get_proposal(conn, run_id)
    if hit is not None:
        return hit
    run = get_run(conn, run_id)
    trigger = "" if run is None else str(run["trigger"] or "")
    return (
        get_proposal(conn, trigger.split(":", 1)[1]) if trigger.startswith("candidate:") else None
    )


def set_proposal_chosen(conn: sqlite3.Connection, run_id: str, nurse_id: str | None) -> None:
    conn.execute("UPDATE proposal SET chosen_nurse = ? WHERE run_id = ?", (nurse_id, run_id))
    conn.commit()


# ---------------------------------------------------------------- conflicts (5.5)


def next_conflict_id(conn: sqlite3.Connection) -> str:
    best = 0
    for (cid,) in conn.execute("SELECT conflict_id FROM conflict"):
        tail = str(cid).rsplit("-", 1)[-1]
        if tail.isdigit():
            best = max(best, int(tail))
    return f"C-{best + 1:04d}"


def insert_conflict(
    conn: sqlite3.Connection,
    conflict_id: str,
    run_id: str,
    core: dict[str, Any],
    options: list[dict[str, Any]],
) -> None:
    conn.execute(
        "INSERT INTO conflict(conflict_id, run_id, core_json, options_json) VALUES(?, ?, ?, ?)",
        (
            conflict_id,
            run_id,
            json.dumps(core, ensure_ascii=False),
            json.dumps(options, ensure_ascii=False),
        ),
    )
    conn.commit()


def _conflict_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "conflict_id": r["conflict_id"],
        "run_id": r["run_id"],
        "core": json.loads(r["core_json"] or "{}"),
        "options": json.loads(r["options_json"] or "[]"),
        "narrative": json.loads(r["narrative"]) if r["narrative"] else None,
        "chosen_option": r["chosen_option"],
        "chosen_by": r["chosen_by"],
        "chosen_at": r["chosen_at"],
        "resolved_run": r["resolved_run"],
    }


def get_conflict(conn: sqlite3.Connection, conflict_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM conflict WHERE conflict_id = ?", (conflict_id,)).fetchone()
    return None if row is None else _conflict_row(row)


def list_conflicts(
    conn: sqlite3.Connection, ward_id: str, *, open_only: bool = False
) -> list[dict[str, Any]]:
    sql = (
        "SELECT c.* FROM conflict c JOIN run r ON r.run_id = c.run_id WHERE r.ward_id = ?"
        + (" AND c.chosen_option IS NULL" if open_only else "")
        + " ORDER BY c.conflict_id DESC"
    )
    return [_conflict_row(r) for r in conn.execute(sql, (ward_id,))]


def conflict_for_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM conflict WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else _conflict_row(row)


def set_conflict_narrative(
    conn: sqlite3.Connection, conflict_id: str, narrative: dict[str, Any]
) -> None:
    conn.execute(
        "UPDATE conflict SET narrative = ? WHERE conflict_id = ?",
        (json.dumps(narrative, ensure_ascii=False), conflict_id),
    )
    conn.commit()


def set_conflict_resolved(
    conn: sqlite3.Connection, conflict_id: str, option_id: str, by: str, resolved_run: str | None
) -> None:
    conn.execute(
        "UPDATE conflict SET chosen_option = ?, chosen_by = ?, chosen_at = ?, resolved_run = ? "
        "WHERE conflict_id = ?",
        (option_id, by, now_iso(), resolved_run, conflict_id),
    )
    conn.commit()


def active_waivers(conn: sqlite3.Connection, ward_id: str) -> frozenset[str]:
    """Hard-constraint names a person has waived by choosing a conflict option."""
    out: set[str] = set()
    for c in list_conflicts(conn, ward_id):
        chosen = next((o for o in c["options"] if o["option_id"] == c["chosen_option"]), None)
        if chosen and chosen.get("waive"):
            out.add(str(chosen["waive"]))
    return frozenset(out)


def set_proposal_message(
    conn: sqlite3.Connection, run_id: str, draft: str | None, status: str | None
) -> None:
    conn.execute(
        "UPDATE proposal SET message_draft = ?, replacement_status = ? WHERE run_id = ?",
        (draft, status, run_id),
    )
    conn.commit()


def set_replacement_status(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    conn.execute("UPDATE proposal SET replacement_status = ? WHERE run_id = ?", (status, run_id))
    conn.commit()


# ---------------------------------------------------------------- roster agent (7.10)


def insert_agent_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ward_id: str,
    workflow: str,
    trigger_ref: str,
    state: dict[str, Any],
) -> None:
    now = now_iso_ms()
    conn.execute(
        "INSERT OR REPLACE INTO agent_task(id, ward_id, workflow, trigger_ref, status, waiting_on, "
        "messages_json, pending_call_json, steps, created_at, updated_at, mode, state_json, "
        "outcome) VALUES(?, ?, ?, ?, 'running', NULL, '[]', NULL, 0, ?, ?, 'agent', ?, NULL)",
        (task_id, ward_id, workflow, trigger_ref, now, now, json.dumps(state, default=str)),
    )
    conn.commit()


def _task_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ward_id": r["ward_id"],
        "workflow": r["workflow"],
        "trigger_ref": r["trigger_ref"],
        "status": r["status"],
        "waiting_on": r["waiting_on"],
        "messages": json.loads(r["messages_json"] or "[]"),
        "pending_call": json.loads(r["pending_call_json"]) if r["pending_call_json"] else None,
        "steps": r["steps"] or 0,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "mode": r["mode"] or "agent",
        "state": json.loads(r["state_json"] or "{}"),
        "outcome": r["outcome"],
    }


def get_agent_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM agent_task WHERE id = ?", (task_id,)).fetchone()
    return None if row is None else _task_row(row)


def list_agent_tasks(
    conn: sqlite3.Connection, ward_id: str, *, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Newest first."""
    sql, args = "SELECT * FROM agent_task WHERE ward_id = ?", [ward_id]
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    rows = conn.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*args, limit))
    return [_task_row(r) for r in rows]


def save_agent_task(conn: sqlite3.Connection, task: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE agent_task SET status = ?, waiting_on = ?, messages_json = ?, "
        "pending_call_json = ?, steps = ?, updated_at = ?, mode = ?, state_json = ?, outcome = ? "
        "WHERE id = ?",
        (
            task["status"],
            task["waiting_on"],
            json.dumps(task["messages"], ensure_ascii=False, default=str),
            None if task["pending_call"] is None else json.dumps(task["pending_call"], default=str),
            task["steps"],
            now_iso_ms(),
            task["mode"],
            json.dumps(task["state"], ensure_ascii=False, default=str),
            task["outcome"],
            task["id"],
        ),
    )
    conn.commit()


# ---------------------------------------------------------------- nurse inbox (7.7)


def next_nurse_message_id(conn: sqlite3.Connection) -> str:
    (n,) = conn.execute("SELECT COUNT(*) FROM nurse_message").fetchone()
    return f"NM-{int(n) + 1:04d}"


def insert_nurse_message(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = ["id", "ward_id", "nurse_id", "kind", "run_id", "request_id", "task_id", "day",
            "shift", "text", "language", "source", "status", "created_at"]  # fmt: skip
    vals = [row.get(c) for c in cols]
    vals[cols.index("created_at")] = row.get("created_at") or now_iso_ms()
    conn.execute(
        f"INSERT INTO nurse_message({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",  # noqa: S608
        vals,
    )
    conn.commit()


def get_nurse_message(conn: sqlite3.Connection, message_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM nurse_message WHERE id = ?", (message_id,)).fetchone()
    return None if row is None else dict(row)


def list_nurse_messages(
    conn: sqlite3.Connection, ward_id: str, *, nurse_id: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:  # fmt: skip
    """Newest first."""
    sql, args = "SELECT * FROM nurse_message WHERE ward_id = ?", [ward_id]
    if nurse_id is not None:
        sql += " AND nurse_id = ?"
        args.append(nurse_id)
    if run_id is not None:
        sql += " AND run_id = ?"
        args.append(run_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY created_at DESC, id DESC", args)]


def set_nurse_message_status(conn: sqlite3.Connection, message_id: str, status: str) -> None:
    conn.execute(
        "UPDATE nurse_message SET status = ?, answered_at = ? WHERE id = ?",
        (status, now_iso_ms(), message_id),
    )
    conn.commit()


def redraft_nurse_message(conn: sqlite3.Connection, message_id: str, text: str) -> None:
    """Reopen an answered ask with new template text (a condition follow-up reuses its ask,
    so the conditional round still counts as one message)."""
    conn.execute(
        "UPDATE nurse_message SET text = ?, source = 'template', status = 'asked', "
        "answered_at = NULL WHERE id = ?",
        (text, message_id),
    )
    conn.commit()
