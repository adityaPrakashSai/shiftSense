"""Port of reference/real_rosters/test_real_ward_a.py against app/planner/totals.py."""

import json

from app.planner.totals import below_target, checksum, daily_totals
from app.store.seed import SEED_DIR

SEED = json.loads((SEED_DIR / "real_ward_a.json").read_text(encoding="utf-8"))
PLAN = SEED["runs"]["planned"]["roster"]
UPD = SEED["runs"]["updated"]["roster"]
DAYS = SEED["ward"]["horizon_days"]


def test_planned_matches_printed_totals() -> None:
    # one known doubt: 29 Oct evening reads 5 cells against a printed 4
    # (faded cells "E?" in N12's row)
    assert checksum(PLAN, SEED["printed_totals"]) == [
        {"day": 29, "shift": "E", "cells": 5, "printed": 4}
    ]


def test_totals_match_fixture_checks() -> None:
    assert daily_totals(PLAN, DAYS) == SEED["checks"]["planned_totals"]
    assert daily_totals(UPD, DAYS) == SEED["checks"]["updated_totals"]


def test_hand_edit_left_nights_short() -> None:
    """Doc 6.5c: after the October leave extension, nights on 13 and 14 Oct are 2 of 3."""
    n = daily_totals(UPD, DAYS)["N"]
    assert n[12] == 2 and n[13] == 2
    assert SEED["printed_totals"]["N"][12] == 3 and SEED["printed_totals"]["N"][13] == 3
    target = {(d, s): 3 for d in range(DAYS) for s in ("M", "E", "N")}
    assert (12, "N", 2, 3) in below_target(UPD, target, DAYS)


def test_ans_outside_coverage() -> None:
    assert all(s in ("G", None) for s in PLAN["ANS"])
    assert sum(PLAN["ANS"][d] == "G" for d in range(DAYS)) > 20
    without_ans = {k: v for k, v in PLAN.items() if k != "ANS"}
    assert daily_totals(PLAN, DAYS) == daily_totals(without_ans, DAYS)


def test_no_real_names() -> None:
    names = [n["name"] for n in SEED["ward"]["nurses"]]
    assert len(names) == 22 and len(set(names)) == 21  # one deliberate duplicate ("Priya Nair")


def test_most_nurses_do_six_nights() -> None:
    six = [n for n, row in PLAN.items() if sum(s == "N" for s in row) == 6]
    assert len(six) >= 14
