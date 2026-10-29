"""What the agent needs from the running app: start solves in the background and wait for them,
and nudge the ward's views. `app/api/jobs.py` provides the real one; tests may pass their own."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date
from typing import Any, Protocol


class Env(Protocol):
    async def start_resolve(
        self,
        conn: sqlite3.Connection,
        ward_id: str,
        *,
        trigger: str,
        affected: Iterable[date],
        request_id: str | None,
        extra_unavailable: Iterable[tuple[str, int, str | None]] = (),
    ) -> str:
        """Start a ranked re-solve (6.2, 6.4) in the background; returns its run id."""
        ...

    async def start_full(self, conn: sqlite3.Connection, ward_id: str, *, trigger: str) -> str:
        """Start a full solve in the background; returns its run id."""
        ...

    async def await_run(self, run_id: str, timeout: float = 60.0) -> None:
        """Return once the run's job has written everything (proposal, conflict)."""
        ...

    async def broadcast(self, ward_id: str, msg: dict[str, Any]) -> None: ...
