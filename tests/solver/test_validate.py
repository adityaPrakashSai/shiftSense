"""Hand-made rosters that break each hard rule are caught with the right constraint name."""

from dataclasses import replace

from app.solver.input import DemandSpec, Forcing
from app.solver.validate import display_codes, violations

from .tiny import hist, nurse, tiny


def test_clean_roster() -> None:
    inp = tiny(days=2)
    assert violations(inp, {("A", 0): "M", ("A", 1): "M"}) == []


def test_each_rule_named() -> None:
    cases = [
        (tiny(unavailable=frozenset({("A", 0, None)})), {("A", 0): "M"}, "unavailable:A:d0"),
        (tiny(unavailable=frozenset({("A", 0, "M")})), {("A", 0): "M"}, "unavailable:A:d0:M"),
        (tiny(leave=frozenset({("A", 0)})), {("A", 0): "E"}, "leave:A:d0"),
        (tiny([nurse(can_night=False)]), {("A", 0): "N"}, "night_qualified:A:d0"),
        (tiny(night_exempt=frozenset({("A", 0)})), {("A", 0): "N"}, "night_exempt:A:d0"),
        (tiny([nurse(standby=True)]), {("A", 0): "M"}, "standby_off_roster:A:d0"),
        (tiny([nurse(role="ans")]), {("A", 0): "M"}, "ans_general_duty:A:d0"),
        (tiny(), {("A", 0): "G"}, "general_duty_forced_only:A:d0"),
        (tiny(days=2), {("A", 0): "N", ("A", 1): "M"}, "rest_after_night:A:d0"),
        (tiny(days=2), {("A", 0): "N", ("A", 1): "E"}, "rest_after_night:A:d0"),
        (
            tiny(history={"A": hist(last_shifts=(None, None, "N"))}),
            {("A", 0): "M"},
            "rest_after_night:A:d-1",
        ),
        (
            tiny(days=3, max_consecutive_days=2),
            {("A", d): "M" for d in range(3)},
            "max_consecutive_days:A:d2",
        ),
        (
            tiny(max_consecutive_days=2, history={"A": hist(consecutive_days=2)}),
            {("A", 0): "M"},
            "max_consecutive_days:A:d0",
        ),
        (
            tiny(days=3, night_block_max=2),
            {("A", d): "N" for d in range(3)},
            "max_consecutive_nights:A:d2",
        ),
        (
            tiny(history={"A": hist(consecutive_nights=3, last_shifts=("N", "N", "N"))}),
            {("A", 0): "N"},
            "max_consecutive_nights:A:d0",
        ),
        (tiny([nurse(max_shifts=1)], days=2), {("A", 0): "M", ("A", 1): "M"}, "max_shifts:A"),
        (tiny(days=2, max_nights=1), {("A", 0): "N", ("A", 1): "N"}, "max_nights:A"),
        (tiny([nurse(max_hours=8.0)], days=2), {("A", 0): "M", ("A", 1): "M"}, "max_hours:A"),
        (tiny(floor=1), {("A", 0): None}, "min_cover:d0:M"),
        (
            tiny([nurse("A"), nurse("B")], max_total={"M": 1}),
            {("A", 0): "M", ("B", 0): "M"},
            "max_cover:d0:M",
        ),
        (tiny(previous={("A", 0): None}, frozen_before_day=1), {("A", 0): "M"}, "frozen:A:d0"),
        (
            tiny(previous={("A", 0): None}, frozen_cells=frozenset({("A", 0)})),
            {("A", 0): "M"},
            "frozen:A:d0",
        ),
        (tiny(forced=(Forcing("fixed_duty", "A", 0, "G"),)), {("A", 0): "M"}, "fixed_duty:A:d0"),
        (tiny(forced=(Forcing("what_if", "A", 0, None),)), {("A", 0): "M"}, "forced:what_if:A:d0"),
        (tiny(forced=(Forcing("swap", "A", 0, "E"),)), {("A", 0): "M"}, "forced:swap:A:d0:E"),
    ]
    for inp, roster, name in cases:
        assert name in violations(inp, roster), (name, violations(inp, roster))


def test_senior_cover() -> None:
    inp = tiny([nurse("A"), nurse("B", grade="senior")])
    inp = replace(inp, demand={**inp.demand, (0, "M"): DemandSpec(1, 1, 1, 1)})
    assert violations(inp, {("A", 0): "M"}) == ["senior_cover:d0:M"]
    assert violations(inp, {("B", 0): "M"}) == []


def test_waived_names_are_not_reported() -> None:
    inp = tiny(leave=frozenset({("A", 0)}), waived=frozenset({"leave:A:d0"}))
    assert violations(inp, {("A", 0): "M"}) == []


def test_g_does_not_count_toward_cover() -> None:
    inp = tiny(floor=1, forced=(Forcing("fixed_duty", "A", 0, "G"),))
    assert violations(inp, {("A", 0): "G"}) == ["min_cover:d0:M"]


def test_display_codes_label_no_after_night() -> None:
    inp = tiny(days=4, leave=frozenset({("A", 3)}))
    row = display_codes(inp, {("A", 0): "N", ("A", 1): None, ("A", 2): None}, inp.nurses[0])
    assert row == ["N", "N/O", "D/O", "L"]
