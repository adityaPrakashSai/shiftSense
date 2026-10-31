"""Counter-offers ("yes, if...") through the API: the nurse conditions her accept, the
agent evaluates the condition with the solver, and the charge nurse decides. Feasible
conditions come back as approvals with deltas; already-granted ones are plain accepts;
impossible ones reopen the ask once; a second condition is a decline."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    with TestClient(app) as c:
        yield c


def wait_for(get: Callable[[], Any], ok: Callable[[Any], bool], secs: float = 60.0) -> Any:
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        val = get()
        if ok(val):
            return val
        time.sleep(0.05)
    raise AssertionError(f"timed out; last value: {val}")


def task(client: TestClient) -> dict[str, Any]:
    tasks = client.get("/api/agent/tasks").json()
    assert tasks, "no agent task"
    return dict(tasks[0])


def start_cascade(client: TestClient) -> tuple[list[str], str]:
    """A sick call, ranked and approved as a contact plan; the first nurse is asked."""
    roster = client.get("/api/roster").json()
    nurse = next(
        c["nurse_id"]
        for c in roster["grid"]
        if c["day"] == "2026-11-03" and c["shift"] == "M" and not c["nurse_id"].startswith("S")
    )
    body = client.post(
        "/api/messages",
        data={"sender_id": nurse, "text": "aaj subah ki duty nahi kar paungi, bukhar hai"},
    ).json()
    run_id = client.post(f"/api/requests/{body['request_id']}/confirm").json()["run_id"]
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    order = list(t["waiting"]["cascade"])
    assert len(order) == 3
    r = client.post(f"/api/agent/tasks/{t['id']}/approve-cascade")
    assert r.status_code == 200, r.json()
    wait_for(lambda: task(client), lambda t: t["status"] == "waiting_reply")
    return order, run_id


def ask(client: TestClient, nurse_id: str) -> dict[str, Any]:
    rows = client.get("/api/inbox", params={"nurse_id": nurse_id}).json()
    assert len(rows) == 1, rows
    return dict(rows[0])


def tier(run: dict[str, Any], name: str) -> int:
    return next(s["best"] for s in run["stages"] if s["tier"] == name)


def test_condition_check_parses_free_text(client: TestClient) -> None:
    order, _ = start_cascade(client)
    m = ask(client, order[0])
    assert m["status"] == "asked" and m["can_condition"]
    r = client.post(f"/api/inbox/{m['id']}/condition/check", json={"text": "kal off chahiye"})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out["condition"] == {"kind": "day_off", "day": "2026-11-04", "shift": None}
    assert out["chip"].endswith("?") and "4" in out["chip"]  # the template confirm chip
    assert ask(client, order[0])["status"] == "asked"  # checking answers nothing


def test_condition_check_rejects_out_of_scope(client: TestClient) -> None:
    order, _ = start_cascade(client)
    m = ask(client, order[0])
    r = client.post(
        f"/api/inbox/{m['id']}/condition/check", json={"text": "I want leave on friday"}
    )
    assert r.status_code == 422, r.json()
    assert r.json()["error"]["code"] == "CONDITION_UNCLEAR"
    assert r.json()["error"]["message"]  # a clarification, not the chip


def test_conditional_feasible_comes_back_with_deltas(client: TestClient) -> None:
    order, run_id = start_cascade(client)
    cands = {c["nurse"]: c for c in client.get(f"/api/proposals/{run_id}").json()["candidates"]}
    first = ask(client, order[0])
    r = client.post(f"/api/inbox/{first['id']}/respond", json={"accept": False})
    assert r.status_code == 200, r.json()
    wait_for(
        lambda: task(client),
        lambda t: t["status"] == "waiting_reply" and t["waiting_on"] == order[1],
    )
    second = ask(client, order[1])
    r = client.post(
        f"/api/inbox/{second['id']}/respond",
        json={"accept": True, "condition": {"kind": "day_off", "day": "2026-11-04"}},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "conditional"
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    new_id = t["waiting"]["ref"]
    assert new_id not in {c["run_id"] for c in cands.values()}  # a new variant run
    new = client.get(f"/api/runs/{new_id}").json()
    cand = client.get(f"/api/runs/{cands[order[1]]['run_id']}").json()
    assert new["run"]["status"] == "proposed"
    diff = new["run"]["changed_cells"] - cand["run"]["changed_cells"]
    summary = t["waiting"]["summary"]
    if diff > 0:
        assert f"{diff} more cell" in summary, summary
    elif diff == 0:
        assert "no more cells change" in summary, summary
    else:
        assert f"{-diff} fewer cell" in summary, summary
    if tier(new, "coverage") == tier(cand, "coverage"):
        assert "coverage unchanged" in summary, summary
    assert "off" in summary, summary
    # the deal holds in the roster: the nurse covers, and is off on the 4th
    cells = {(c["nurse_id"], c["day"]): c["to_shift"] for c in new["change_set"]}
    assert cells.get((order[1], "2026-11-03")) == "M"
    assert not cells.get((order[1], "2026-11-04"))
    assert client.post(f"/api/runs/{new_id}/publish").status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "done")
    assert "published" in t["outcome"]


def test_conditional_already_true_is_a_plain_accept(client: TestClient) -> None:
    order, run_id = start_cascade(client)
    cands = {c["nurse"]: c for c in client.get(f"/api/proposals/{run_id}").json()["candidates"]}
    m = ask(client, order[0])
    # the top candidate is already off on the 4th: nothing to evaluate
    r = client.post(
        f"/api/inbox/{m['id']}/respond",
        json={"accept": True, "condition": {"kind": "day_off", "day": "2026-11-04"}},
    )
    assert r.status_code == 200, r.json()
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    assert t["waiting"]["ref"] == cands[order[0]]["run_id"]  # her own candidate run
    assert "accepted" in t["waiting"]["summary"], t["waiting"]["summary"]
    assert client.post(f"/api/runs/{t['waiting']['ref']}/publish").status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "done")
    assert "published" in t["outcome"]


def test_conditional_infeasible_reopens_the_ask_once(client: TestClient) -> None:
    order, run_id = start_cascade(client)
    m = ask(client, order[0])
    r = client.post(
        f"/api/inbox/{m['id']}/respond",
        json={"accept": True, "condition": {"kind": "day_off", "day": "2026-11-03"}},
    )
    assert r.status_code == 200, r.json()
    first_text = m["text"]
    again = wait_for(
        lambda: ask(client, order[0]),
        lambda a: a["status"] == "asked" and a["text"] != first_text,
    )
    assert again["id"] == m["id"]  # the same message row, redrafted
    assert again["text"] != m["text"] and not again["can_condition"]
    assert client.post(f"/api/inbox/{m['id']}/respond", json={"accept": False}).status_code == 200
    wait_for(
        lambda: task(client),
        lambda t: t["status"] == "waiting_reply" and t["waiting_on"] == order[1],
    )
    nxt = ask(client, order[1])
    assert client.post(f"/api/inbox/{nxt['id']}/respond", json={"accept": True}).status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    assert client.post(f"/api/runs/{t['waiting']['ref']}/publish").status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "done")
    assert "published" in t["outcome"]


def test_reject_reopens_the_deal_as_a_plain_followup(client: TestClient) -> None:
    order, _ = start_cascade(client)
    first = ask(client, order[0])
    r = client.post(f"/api/inbox/{first['id']}/respond", json={"accept": False})
    assert r.status_code == 200, r.json()
    wait_for(
        lambda: task(client),
        lambda t: t["status"] == "waiting_reply" and t["waiting_on"] == order[1],
    )
    second = ask(client, order[1])
    body = {"accept": True, "condition": {"kind": "day_off", "day": "2026-11-04"}}
    assert client.post(f"/api/inbox/{second['id']}/respond", json=body).status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    new_id = t["waiting"]["ref"]
    r = client.post(f"/api/agent/tasks/{t['id']}/reject")
    assert r.status_code == 200, r.json()
    assert client.get(f"/api/runs/{new_id}").json()["run"]["status"] == "proposed"
    again = wait_for(
        lambda: ask(client, order[1]),
        lambda a: a["status"] == "asked" and not a["can_condition"],
    )
    assert again["id"] == second["id"] and again["text"] != second["text"]
    r = client.post(f"/api/inbox/{second['id']}/respond", json={"accept": True})
    assert r.status_code == 200, r.json()
    t = wait_for(lambda: task(client), lambda t: t["status"] == "waiting_approval")
    assert client.post(f"/api/runs/{t['waiting']['ref']}/publish").status_code == 200
    t = wait_for(lambda: task(client), lambda t: t["status"] == "done")
    assert "published" in t["outcome"]
    # rejecting a finished task is a no-op with a clear error
    r = client.post(f"/api/agent/tasks/{t['id']}/reject")
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_RUN_APPROVAL"


def test_second_condition_counts_as_a_decline(client: TestClient) -> None:
    order, _ = start_cascade(client)
    m = ask(client, order[0])
    body = {"accept": True, "condition": {"kind": "day_off", "day": "2026-11-03"}}
    assert client.post(f"/api/inbox/{m['id']}/respond", json=body).status_code == 200
    wait_for(lambda: ask(client, order[0]), lambda m: not m["can_condition"])
    body2 = {"accept": True, "condition": {"kind": "day_off", "day": "2026-11-05"}}
    assert client.post(f"/api/inbox/{m['id']}/respond", json=body2).status_code == 200
    t = wait_for(
        lambda: task(client),
        lambda t: t["status"] == "waiting_reply" and t["waiting_on"] == order[1],
    )
    assert t["cascade"]["asked"][order[0]] == "declined"
    assert ask(client, order[1])["status"] == "asked"
