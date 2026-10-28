"""Checks for the real_a seed. Run: pytest -q test_real_ward_a.py

Moves to tests/fixtures/ in the app (M1 checksum test, M2 validator report, M7/M10 scenario).
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from totals import checksum, daily_totals  # noqa: E402

SEED = json.load(open(HERE.parents[1] / "data/seed/real_ward_a.json"))
PLAN = SEED["runs"]["planned"]["roster"]
UPD = SEED["runs"]["updated"]["roster"]
DAYS = SEED["ward"]["horizon_days"]


def test_planned_matches_printed_totals():
    # one known doubt: 29 Oct evening reads 5 cells against a printed 4 (faded cells "E?" in N12's row)
    assert checksum(PLAN, SEED["printed_totals"]) == [{"day": 29, "shift": "E", "cells": 5, "printed": 4}]


def test_hand_edit_left_nights_short():
    """The October case for doc 6.5c: after the leave extension, nights on 13 and 14 Oct are 2 of 3."""
    n = daily_totals(UPD, DAYS)["N"]
    assert n[12] == 2 and n[13] == 2
    assert SEED["printed_totals"]["N"][12] == 3 and SEED["printed_totals"]["N"][13] == 3


def test_ans_outside_coverage():
    assert all(s in ("G", None) for s in PLAN["ANS"])
    assert sum(PLAN["ANS"][d] == "G" for d in range(DAYS)) > 20


def test_no_real_names():
    names = [n["name"] for n in SEED["ward"]["nurses"]]
    assert len(names) == 22 and len(set(names)) == 21   # one deliberate duplicate ("Priya Nair")


def test_most_nurses_do_six_nights():
    six = [n for n, row in PLAN.items() if sum(s == "N" for s in row) == 6]
    assert len(six) >= 14
