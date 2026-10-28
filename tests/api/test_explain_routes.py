"""Explain routes (design doc 10.1): POST /api/explain, stored explanations, cell evidence."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    with TestClient(app) as c:
        yield c


def test_explain_and_read_back(client: TestClient) -> None:
    r = client.post("/api/explain", json={"question": "Is the roster proven?"})
    assert r.status_code == 200
    body = r.json()
    assert body["grounded"] and not body["fallback_used"] and body["claims"]
    assert body["tool_calls"][0]["tool"] == "get_run_summary"
    cid = body["claims"][0]["call_id"]
    assert cid in {t["call_id"] for t in body["tool_calls"]}
    again = client.get(f"/api/explanations/{body['explanation_id']}").json()
    assert again["answer"] == body["answer"] and again["claims"] == body["claims"]


def test_cell_context_and_evidence(client: TestClient) -> None:
    roster = client.get("/api/roster").json()
    c = next(x for x in roster["grid"] if x["day"] == "2026-11-04" and x["shift"])
    ctx = {"nurse_id": c["nurse_id"], "day": c["day"], "shift": c["shift"]}
    r = client.post("/api/explain", json={"question": "Why this shift?", "context": ctx}).json()
    assert r["tool_calls"][0]["tool"] == "get_assignment_evidence" and r["grounded"]
    ev = client.get(
        "/api/evidence/cell", params={"nurse_id": c["nurse_id"], "day": c["day"]}
    ).json()
    assert ev["shift"] == c["shift"] and {o["shift"] for o in ev["other_shifts"]}


def test_question_in_chat_gets_claims(client: TestClient) -> None:
    body = client.post(
        "/api/messages", data={"sender_id": "N02", "text": "mujhe har weekend kyun milta hai?"}
    ).json()
    assert body["reply_kind"] == "answer" and body["explanation_id"] and body["claims"]
    hist = client.get("/api/messages", params={"nurse_id": "N02"}).json()
    assert hist[-1]["claims"] == body["claims"]


def test_explain_errors(client: TestClient) -> None:
    assert client.post("/api/explain", json={"question": " "}).status_code == 400
    r = client.post("/api/explain", json={"question": "why?", "run_id": "run-nope"})
    assert r.status_code == 404
    assert client.get("/api/explanations/exp-nope").status_code == 404
    bad = client.get("/api/evidence/cell", params={"nurse_id": "N99", "day": "2026-11-04"})
    assert bad.status_code == 404
