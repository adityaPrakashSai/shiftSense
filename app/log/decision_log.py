"""Append-only decision log (design doc 8): one `event` row per solver stage or human decision.

`DecisionLog.emit` inserts the row and calls every listener with `{type: "event", seq}`; the
WebSocket hub registers itself as a listener in M4. `SolverEvents` adapts the solver's EventSink
calls to the 8.2 event kinds.
"""

import json
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

from app.store import repo

Listener = Callable[[dict[str, Any]], None]


class DecisionLog:
    def __init__(self, conn: sqlite3.Connection, ward_id: str) -> None:
        self.conn = conn
        self.ward_id = ward_id
        self.listeners: list[Listener] = []
        self._lock = threading.Lock()  # solution callbacks arrive on CP-SAT worker threads

    def emit(
        self,
        kind: str,
        actor: str,
        run_id: str | None = None,
        stage: str | None = None,
        /,
        **payload: Any,
    ) -> int:
        """Positional-only envelope fields, so payload keys such as `kind` never collide."""
        with self._lock:
            seq = repo.insert_event(
                self.conn,
                ward_id=self.ward_id,
                run_id=run_id,
                actor=actor,
                kind=kind,
                stage=stage,
                payload_json=json.dumps(payload, default=str, ensure_ascii=False),
            )
        for fn in self.listeners:
            fn({"type": "event", "seq": seq})
        return seq


class SolverEvents:
    """EventSink for one run: model.built, stage.start, stage.improved, stage.done (8.2)."""

    def __init__(self, log: DecisionLog, run_id: str) -> None:
        self.log, self.run_id = log, run_id

    def stage(self, tier: str, kind: str, **data: Any) -> None:
        if tier == "model":
            self.log.emit("model.built", "solver", self.run_id, None, **data)
            return
        rank = data.pop("rank", None)
        label = tier if rank is None else f"P{rank}:{tier}"
        if kind == "skip":
            self.log.emit("stage.done", "solver", self.run_id, label, status="skip", proven=True)
        else:
            self.log.emit(f"stage.{kind}", "solver", self.run_id, label, **data)
