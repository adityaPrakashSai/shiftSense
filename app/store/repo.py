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
        "counterpart_id, hard, weight, status, source_message_id, edit_json, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def delete_request(conn: sqlite3.Connection, request_id: str) -> None:
    conn.execute("DELETE FROM request WHERE id = ?", (request_id,))
    conn.commit()


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
) -> None:
    """Write one row per nurse x day. `roster` maps nurse id -> shift code per day (None = off)."""
    rows = []
    for nurse_id, cells in roster.items():
        prev_cells = None if previous is None else previous.get(nurse_id)
        for d, shift in enumerate(cells):
            day = date.fromordinal(horizon_start.toordinal() + d).isoformat()
            prev = None if prev_cells is None else prev_cells[d]
            changed = None if prev_cells is None else int(prev != shift)
            rows.append((run_id, nurse_id, day, shift, prev, changed))
    conn.executemany(
        "INSERT INTO assignment(run_id, nurse_id, day, shift, prev_shift, changed) "
        "VALUES(?, ?, ?, ?, ?, ?)",
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
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update run fields {sorted(bad)}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE run SET {cols} WHERE run_id = ?", (*fields.values(), run_id))  # noqa: S608
    conn.commit()


def set_published(conn: sqlite3.Connection, ward_id: str, run_id: str, by: str) -> str | None:
    """Publish `run_id` and supersede the previously published run, in one transaction.
    Returns the superseded run id."""
    old = get_published_run(conn, ward_id)
    ts = now_iso()
    with conn:
        if old is not None:
            conn.execute(
                "UPDATE run SET status = ? WHERE run_id = ?",
                (RunStatus.SUPERSEDED.value, old["run_id"]),
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
