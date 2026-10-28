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
