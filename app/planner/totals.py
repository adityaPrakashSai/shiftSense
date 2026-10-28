"""Daily totals row and photo-import checksum (design doc 7.8, 10.3).

Ported from reference/real_rosters/totals.py. `roster` maps nurse id -> shift code per day
(None = off). G (general duty) never counts toward coverage.
"""

from collections.abc import Mapping, Sequence
from typing import TypedDict

COUNTED = ("M", "E", "N")

Roster = Mapping[str, Sequence[str | None]]


class Mismatch(TypedDict):
    day: int  # 1-based day of the period, as printed on the sheet
    shift: str
    cells: int
    printed: int


def daily_totals(roster: Roster, days: int) -> dict[str, list[int]]:
    out = {s: [0] * days for s in COUNTED}
    for row in roster.values():
        for d, s in enumerate(row[:days]):
            if s in out:
                out[s][d] += 1
    return out


def checksum(roster: Roster, printed: Mapping[str, Sequence[int]]) -> list[Mismatch]:
    """Days where the cells do not add up to the printed totals row (highlighted in the import)."""
    days = len(next(iter(printed.values())))
    got = daily_totals(roster, days)
    return [
        Mismatch(day=d + 1, shift=s, cells=got[s][d], printed=printed[s][d])
        for s in COUNTED
        for d in range(days)
        if got[s][d] != printed[s][d]
    ]


def below_target(
    roster: Roster, target: Mapping[tuple[int, str], int], days: int
) -> list[tuple[int, str, int, int]]:
    """(day, shift, assigned, target) for every cell under target (red in the totals row)."""
    got = daily_totals(roster, days)
    return [
        (d, s, got[s][d], target[(d, s)])
        for d in range(days)
        for s in COUNTED
        if got[s][d] < target[(d, s)]
    ]
