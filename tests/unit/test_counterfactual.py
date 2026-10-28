"""Counterfactual service (design doc 6.6, 11.3)."""

import sqlite3
from datetime import date

from app.domain.models import Request, RequestKind, RequestStatus
from app.evidence.counterfactual import Question, compare, explain_denied, what_if
from app.planner import requests as lifecycle
from app.planner.runs import start_resolve
from app.planner.sickcall import rank
from app.store import repo
from tests.helpers import NOW, mini_baseline


def test_trainee_on_a_night_is_impossible(conn: sqlite3.Connection) -> None:
    run_id = mini_baseline(conn)
    # N07 (Deepa) is not night-qualified in the mini ward
    v = what_if(conn, run_id, Question(forcings=(("N07", 9, "N"),)))
    assert v.verdict == "IMPOSSIBLE" and v.reason_code == "HARD:night_qualified"
    assert "night_qualified:N07:d9" in v.core and v.alt is None


def test_cache_hit_on_the_second_call(conn: sqlite3.Connection) -> None:
    run_id = mini_baseline(conn)
    q = Question(forcings=(("N02", 12, None),), scope="local")
    first = what_if(conn, run_id, q)
    second = what_if(conn, run_id, q)
    assert not first.cached and second.cached
    assert (second.verdict, second.reason_code, second.alt) == (
        first.verdict, first.reason_code, first.alt,
    )  # fmt: skip
    assert "everyone else's roster fixed" in first.note


def test_swapping_two_identical_nurses_is_a_tie(conn: sqlite3.Connection) -> None:
    """Scenario C: S01 and S02 are interchangeable standby nurses for Thursday evening."""
    mini_baseline(conn)
    ward = repo.get_ward(conn)
    assert ward is not None
    thu = date(2026, 11, 5)
    req = Request(id="R-0200", nurse_id="charge", kind=RequestKind.DEMAND, dates=[thu],
                  shifts=["E"], target_total=3, hard=False,
                  status=RequestStatus.PENDING_CONFIRM, created_at=NOW)  # fmt: skip
    repo.insert_request(conn, ward.id, req)
    lifecycle.confirm(conn, ward, "R-0200")
    plan = start_resolve(conn, trigger="request:R-0200", affected=[thu], now=NOW)
    rank(conn, plan)
    grid = repo.get_roster(conn, plan.run_id)
    on, off = ("S01", "S02") if grid["S01"][10] == "E" else ("S02", "S01")
    v = what_if(conn, plan.run_id, Question(forcings=((off, 10, "E"), (on, 10, None))))
    assert v.verdict == "EQUAL" and v.reason_code == "TIE"


def test_compare_order_and_unproven_suffix() -> None:
    pr = ("coverage", "stability", "requests")
    assert compare({"coverage": 1, "stability": 2}, {"coverage": 1, "stability": 3}, pr) == (
        "WORSE", "TIER:stability",
    )  # fmt: skip
    assert compare({"coverage": 1, "requests": 5}, {"coverage": 0, "requests": 9}, pr) == (
        "BETTER", "BASE_NOT_OPTIMAL",
    )  # fmt: skip
    assert compare({"coverage": 1}, {"coverage": 1}, pr) == ("EQUAL", "TIE")


def test_denied_requests_get_reason_codes(conn: sqlite3.Connection) -> None:
    run_id = mini_baseline(conn)
    denied = [o for o in repo.get_request_outcomes(conn, run_id) if o["status"] == "DENIED"]
    assert denied, "the reference baseline denies some requests"
    assert explain_denied(conn, run_id) == len(denied)
    after = {o["request_id"]: o for o in repo.get_request_outcomes(conn, run_id)}
    for o in denied:
        code = after[o["request_id"]]["reason_code"]
        assert code.split(":")[0] in ("TIER", "HARD", "TIE", "BASE_NOT_OPTIMAL"), code
        assert "granting it:" in after[o["request_id"]]["detail"]
    assert explain_denied(conn, run_id) == 0  # already explained: nothing to redo
