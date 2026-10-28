"""Change set of a re-solve against the published roster (design doc 6.3).

Each differing cell gets a cause: `request` (the published cell the request removed, now off),
`replacement` (a nurse who was off moved onto a removed cell's shift, or onto a slot a demand
increase added), or `knock_on` (anything else). `changed_cells` counts replacement and knock-on
changes only, so "1 changed cell" means the replacement took the shift and nobody else moved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.solver.input import SolveInput

RosterGrid = Mapping[tuple[str, int], str | None]


@dataclass(frozen=True)
class Change:
    nurse: str
    day: int
    before: str | None
    after: str | None
    cause: str  # request | replacement | knock_on


def away_cells(inp: SolveInput) -> dict[tuple[str, int], set[str] | None]:
    """(nurse, day) -> shifts the nurse cannot work (None = the whole day)."""
    away: dict[tuple[str, int], set[str] | None] = {}
    for n, d, sh in inp.unavailable:
        cur = away.get((n, d), set())
        away[n, d] = None if sh is None or cur is None else cur | {sh}
    for k in inp.leave:
        away[k] = None
    return away


def change_set(
    inp: SolveInput, roster: RosterGrid, added_slots: Iterable[tuple[int, str]] = ()
) -> list[Change]:
    if inp.previous is None:
        return []
    away = away_cells(inp)
    added = set(added_slots)
    removed = {
        (d, p)
        for (n, d), p in inp.previous.items()
        if p is not None and (n, d) in away and (away[n, d] is None or p in (away[n, d] or set()))
    }
    out = []
    for (n, d), before in sorted(inp.previous.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        after = roster.get((n, d))
        if after == before:
            continue
        blocked = (n, d) in away and (away[n, d] is None or before in (away[n, d] or set()))
        if before is not None and blocked and after is None:
            cause = "request"
        elif before is None and after is not None and (d, after) in removed | added:
            cause = "replacement"
        else:
            cause = "knock_on"
        out.append(Change(n, d, before, after, cause))
    return out


def count_changed(changes: Iterable[Change]) -> int:
    return sum(1 for c in changes if c.cause != "request")
