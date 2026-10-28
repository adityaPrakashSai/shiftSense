"""Local rule check without CP-SAT (design doc 5.6, 6.4 step 2).

`rule_check` answers "which hard rules break if only this one cell changes?" in milliseconds, for
the sick-call candidate filter. It re-checks only what the cell can affect: the nurse's own rules
and the cover rules of that day. A test compares it with the full validator on random edits.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.solver.input import SolveInput
from app.solver.validate import cover_violations, nurse_violations

RosterGrid = Mapping[tuple[str, int], str | None]


def _local(inp: SolveInput, roster: RosterGrid, nurse: str, day: int) -> set[str]:
    return set(nurse_violations(inp, roster, nurse)) | set(cover_violations(inp, roster, [day]))


def rule_check(
    roster: RosterGrid, inp: SolveInput, nurse: str, day: int, shift: str | None
) -> list[str]:
    """Hard-rule names broken by setting (nurse, day) to `shift` with all other cells fixed.

    Rules that were already broken before the change are not reported.
    """
    if roster.get((nurse, day)) == shift:
        return []
    edited = dict(roster)
    edited[nurse, day] = shift
    return sorted(_local(inp, edited, nurse, day) - _local(inp, roster, nurse, day))
