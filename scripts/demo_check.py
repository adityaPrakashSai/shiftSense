"""Run the demo script (design doc 2.2, steps 2-9) against a running server over HTTP.

    uv run python -m scripts.demo_check [--base http://127.0.0.1:8000] [--reset]

Expects the mini ward (SEED=mini) with "now" on the sick-call morning
(DEMO_NOW=2026-11-03T06:05:00+05:30). Meena sends the Hindi sick call, confirms; the roster agent
ranks and asks for approval; the charge nurse asks "Why Arun and not Priya?" and approves; Arun's
inbox gets the drafted message and he accepts; the agent ends done. Prints each step with its time
and exits non-zero on the first failure. Works in any LLM mode; in live mode it spends credits.
"""

import argparse
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx

SICK = "Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai"
HEALTH = ("fever", "bukhar", "sick", "bimar", "बुखार")


def wait_for(get: Callable[[], Any], ok: Callable[[Any], bool], secs: float) -> Any:
    t0 = time.monotonic()
    val = None
    while time.monotonic() - t0 < secs:
        val = get()
        if ok(val):
            return val
        time.sleep(0.25)
    raise AssertionError(f"timed out after {secs:.0f} s; last: {str(val)[:300]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--reset", action="store_true", help="POST /api/demo/reset first")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds per waiting step")
    a = ap.parse_args(argv)
    c = httpx.Client(base_url=a.base, timeout=60)
    t_start = time.monotonic()

    def step(label: str) -> None:
        print(f"{time.monotonic() - t_start:7.1f} s  {label}", flush=True)

    def task() -> dict[str, Any]:
        rows = c.get("/api/agent/tasks").json()
        return rows[0] if rows else {}

    try:
        if a.reset:
            c.post("/api/demo/reset").raise_for_status()
            step("reset")
        h = c.get("/api/health").json()
        step(f"health: mode {h['llm_mode']}, {h['provider']} {h['model']}")
        msg = c.post("/api/messages", data={"sender_id": "N03", "text": SICK})
        msg.raise_for_status()
        body = msg.json()
        assert body["reply_kind"] == "confirm", body
        step(f"intake: {body['reply']}")
        out = c.post(f"/api/requests/{body['request_id']}/confirm").json()
        run_id = out["run_id"]
        step(f"confirmed {body['request_id']}, re-solve {run_id}")
        t = wait_for(task, lambda t: t.get("status") in ("waiting_approval", "escalated",
                                                          "failed"), a.timeout)  # fmt: skip
        assert t["status"] == "waiting_approval", t
        step(f"agent ({t['mode']}) asks for approval: {t['waiting']['summary']}")
        p = c.get(f"/api/proposals/{t['waiting']['ref']}").json()
        names = [x["nurse"] for x in p["candidates"]]
        step(f"ranked {names}, excluded {[e['nurse'] for e in p['excluded']]}")
        why = {"question": "Why Arun and not Priya?", "asker_id": "charge", "run_id": p["run_id"]}
        ex = c.post("/api/explain", json=why).json()
        step(f"why: {ex['answer']} (grounded {ex['grounded']}, {len(ex['claims'])} claims)")
        top = p["candidates"][0]["nurse"]
        c.post(f"/api/proposals/{p['run_id']}/choose", json={"nurse_id": top}).raise_for_status()
        step(f"approved {top}")
        inbox = wait_for(lambda: c.get("/api/inbox", params={"nurse_id": top}).json(),
                         lambda rows: bool(rows), a.timeout)  # fmt: skip
        text = inbox[0]["text"]
        assert not any(w in text.lower() for w in HEALTH), text
        step(f"message to {top} ({inbox[0]['source']}): {text}")
        c.post(f"/api/inbox/{inbox[0]['id']}/respond", json={"accept": True}).raise_for_status()
        t = wait_for(task, lambda t: t.get("status") in ("done", "escalated", "failed"), a.timeout)
        assert t["status"] == "done", t
        step(f"agent done ({t['mode']}): {t['outcome']}; {t['counters']}")
        kinds = {e["kind"] for e in c.get("/api/events", params={"last": 500}).json()}
        need = {"intake.parsed", "request.confirmed", "stage.done", "run.published",
                "message.drafted", "replacement.accepted", "agent.done"}  # fmt: skip
        assert need <= kinds, need - kinds
        step("decision log has intake, confirmation, stages, approval and acceptance")
    except (AssertionError, httpx.HTTPError, KeyError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print("demo script passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
