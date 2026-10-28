"""Daily totals row and photo-import checksum (design doc 7.8, 10.3; app/planner/totals.py).

roster: {nurse_id: [shift code or None, ...]}. G (general duty) never counts toward coverage.
"""

COUNTED = ("M", "E", "N")


def daily_totals(roster: dict, days: int) -> dict[str, list[int]]:
    out = {s: [0] * days for s in COUNTED}
    for row in roster.values():
        for d, s in enumerate(row[:days]):
            if s in out:
                out[s][d] += 1
    return out


def checksum(roster: dict, printed: dict[str, list[int]]) -> list[dict]:
    """Days where the cells do not add up to the printed totals row: what the import preview highlights."""
    days = len(next(iter(printed.values())))
    got = daily_totals(roster, days)
    return [{"day": d + 1, "shift": s, "cells": got[s][d], "printed": printed[s][d]}
            for s in COUNTED for d in range(days) if got[s][d] != printed[s][d]]


def below_target(roster: dict, target: dict[tuple[int, str], int], days: int) -> list[tuple[int, str, int, int]]:
    """(day, shift, assigned, target) for every cell under target: the red cells in the totals row."""
    got = daily_totals(roster, days)
    return [(d, s, got[s][d], target[(d, s)]) for d in range(days) for s in COUNTED
            if got[s][d] < target[(d, s)]]
