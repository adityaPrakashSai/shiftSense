"""Reply deadlines: real-wall-clock helpers shared by `wait_for_reply` and the timeout tick.

Deadlines deliberately use the wall clock, never DEMO_NOW (frozen demo time would never
pass a deadline). Tests fake time by monkeypatching `utcnow`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def deadline_after(seconds: float) -> str:
    """ISO Z timestamp `seconds` after now (same shape as `repo.now_iso_ms`)."""
    return (
        (utcnow() + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def past(deadline_iso: str, now: datetime | None = None) -> bool:
    return datetime.fromisoformat(deadline_iso.replace("Z", "+00:00")) <= (now or utcnow())
