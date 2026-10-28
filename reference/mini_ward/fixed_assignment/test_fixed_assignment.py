"""Checks for the fixed-assignment fixture (doc 5.7, M15). Run: pytest -q test_fixed_assignment.py"""
import json
import pathlib

SEED = json.load(open(pathlib.Path(__file__).resolve().parents[3] / "tests/fixtures/fixed_assignment.json"))
EXP = SEED["expected"]


def test_one_shift_and_one_weekday_off_each():
    a = EXP["assignment"]
    assert len(a) == 8
    assert all(v["shift"] in ("M", "E", "N") and v["weekday_off"] for v in a.values())


def test_golden_tier_values():
    # 8 slots short over 16 days is forced: 8 nurses for 7 daily slots, each with a fixed weekday off, plus 3 days of leave
    assert EXP["tier_values"] == {"coverage": 8, "rotation": 0, "stability": 5}


def test_non_night_nurse_not_on_nights():
    no_night = [n["id"] for n in SEED["ward"]["nurses"] if not n["can_night"]]
    assert all(EXP["assignment"][n]["shift"] != "N" for n in no_night)
