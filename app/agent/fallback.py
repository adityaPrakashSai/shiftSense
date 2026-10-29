"""The workflows as fixed code (design doc 7.10 deterministic fallback, 6.4 recipe).

`next_call(workflow, calls, state)` reads the task's history (every executed call as
{"call_id", "tool", "args", "result"}; a wait tool's result is the person's answer) and returns the
next call in the default order, or None when the workflow is finished. The loop uses it when the
agent has failed twice in a row, the mock model uses it in mock mode, and `finished()` tells the
loop whether a model that stopped calling tools actually completed its workflow.

`approval_summary` writes the summary for `request_approval` from the latest relevant tool result
with `describe()` sentences, so every claim passes the claim check.
"""

from __future__ import annotations

from typing import Any

from app.explain import describe

Call = dict[str, Any]
Step = tuple[str, dict[str, Any]]
MAX_DECLINES = 3


def _ok(c: Call) -> bool:
    return isinstance(c.get("result"), dict) and "error" not in c["result"]


def _last(calls: list[Call], tool: str, after: int = -1) -> tuple[int, dict[str, Any]] | None:
    for i in range(len(calls) - 1, after, -1):
        if calls[i]["tool"] == tool and _ok(calls[i]):
            return i, calls[i]["result"]
    return None


def _errors(calls: list[Call], tool: str) -> int:
    """Failed calls the fixed code made itself (the model's mistakes do not count against it)."""
    return sum(1 for c in calls if c["tool"] == tool and not _ok(c) and c.get("mode") == "fallback")


def _approval(calls: list[Call], after: int, kind: str) -> tuple[int, dict[str, Any]] | None:
    """The answered request_approval of `kind` (run | conflict | request) after index `after`."""
    for i in range(len(calls) - 1, after, -1):
        c = calls[i]
        if c["tool"] != "request_approval" or not _ok(c) or "decision" not in c["result"]:
            continue
        if _approval_kind(c["args"]) == kind:
            return i, c["result"]
    return None


def _approval_kind(args: dict[str, Any]) -> str:
    if args.get("run_id"):
        return "run"
    return "conflict" if args.get("conflict_id") else "request"


def declined(calls: list[Call]) -> list[str]:
    return [
        str(c["result"].get("nurse_id"))
        for c in calls
        if c["tool"] == "wait_for_reply" and _ok(c) and c["result"].get("reply") == "declined"
    ]


def _approve(calls: list[Call], **target: Any) -> Step:
    text, claims = approval_summary(calls)
    return "request_approval", {**target, "summary": text, "claims": claims}


def _replacement(calls: list[Call]) -> Step | None:
    """Sick call and demand change: rank, approval, message, reply; a decline ranks again."""
    if _last(calls, "get_open_cells") is None:
        return "get_open_cells", {}
    rk = _last(calls, "rank_replacements")
    if rk is None:
        return "rank_replacements", {"exclude": declined(calls)}
    i, r = rk
    if r.get("status") != "proposed":
        return None  # infeasible: the conflict task takes over
    ap = _approval(calls, i, "run")
    if ap is None:
        return _approve(calls, run_id=r["run_id"])
    j, a = ap
    if a.get("decision") != "approved" or not a.get("nurse"):
        return None  # rejected, superseded, or a plain re-plan with nobody to ask
    msg = _last(calls, "send_message", j)
    if msg is None:
        return "send_message", {"nurse_id": a["nurse"], "kind": "replacement",
                                "run_id": a["run_id"]}  # fmt: skip
    k, m = msg
    rep = _last(calls, "wait_for_reply", k)
    if rep is None:
        return "wait_for_reply", {"nurse_id": m["nurse_id"], "message_id": m["message_id"]}
    if rep[1].get("reply") == "accepted":
        return None
    no = declined(calls)
    if len(no) >= MAX_DECLINES:
        return "escalate", {"reason": f"{len(no)} nurses declined the shift: {', '.join(no)}"}
    return "rank_replacements", {"exclude": no}


def _conflict(calls: list[Call], state: dict[str, Any]) -> Step | None:
    cid = state.get("conflict_id")
    if _last(calls, "diagnose") is None:
        return "diagnose", {"conflict_id": cid}
    if _last(calls, "list_conflict_options") is None:
        return "list_conflict_options", {"conflict_id": cid}
    ap = _approval(calls, -1, "conflict")
    if ap is None:
        return _approve(calls, conflict_id=cid)
    i, a = ap
    if a.get("decision") != "approved":
        return None
    if _last(calls, "apply_option", i) is None:
        return "apply_option", {"conflict_id": cid, "option_id": a.get("option_id")}
    return _resolve_then_approve(calls, i)


def _resolve_then_approve(calls: list[Call], after: int) -> Step | None:
    rr = _last(calls, "run_resolve", after)
    if rr is None:
        return "run_resolve", {}
    i, r = rr
    if r.get("status") != "proposed":
        return None
    if _approval(calls, i, "run") is None:
        return _approve(calls, run_id=r["run_id"])
    return None


def _leave(calls: list[Call], state: dict[str, Any]) -> Step | None:
    rid = state.get("request_id")
    if _last(calls, "what_if") is None:
        return "what_if", {"request_id": rid}
    ap = _approval(calls, -1, "request")
    if ap is None:
        return _approve(calls, request_id=rid)
    if ap[1].get("decision") != "approved":
        return None
    return _resolve_then_approve(calls, ap[0])


def _swap(calls: list[Call], state: dict[str, Any]) -> Step | None:
    rid = state.get("request_id")
    if _last(calls, "rule_check_swap") is None:
        return "rule_check_swap", {"request_id": rid}
    if _last(calls, "what_if") is None:
        return "what_if", {"request_id": rid}
    msg = _last(calls, "send_message")
    if msg is None:
        return "send_message", {"nurse_id": state.get("counterpart"), "kind": "swap",
                                "request_id": rid}  # fmt: skip
    rep = _last(calls, "wait_for_reply", msg[0])
    if rep is None:
        return "wait_for_reply", {"nurse_id": msg[1]["nurse_id"],
                                  "message_id": msg[1]["message_id"]}  # fmt: skip
    if rep[1].get("reply") != "accepted":
        return None
    ap = _approval(calls, rep[0], "request")
    if ap is None:
        return _approve(calls, request_id=rid)
    if ap[1].get("decision") != "approved":
        return None
    return _resolve_then_approve(calls, ap[0])


def _new_period(calls: list[Call]) -> Step | None:
    full = _last(calls, "run_full")
    if full is None:
        return "run_full", {}
    i, r = full
    if r.get("status") != "proposed":
        return None
    if _last(calls, "get_run_summary", i) is None:
        return "get_run_summary", {"run_id": r["run_id"]}
    if _last(calls, "explain_denied_requests", i) is None:
        return "explain_denied_requests", {"run_id": r["run_id"]}
    if _approval(calls, i, "run") is None:
        return _approve(calls, run_id=r["run_id"])
    return None


def next_call(workflow: str, calls: list[Call], state: dict[str, Any]) -> Step | None:
    if any(c["tool"] == "escalate" for c in calls):
        return None
    for tool in {c["tool"] for c in calls}:
        if _errors(calls, tool) >= 2 and _last(calls, tool) is None:
            return "escalate", {"reason": f"{tool} failed twice"}
    if workflow in ("sick_call", "demand_change"):
        return _replacement(calls)
    if workflow == "conflict":
        return _conflict(calls, state)
    if workflow == "leave":
        return _leave(calls, state)
    if workflow == "swap":
        return _swap(calls, state)
    if workflow == "new_period":
        return _new_period(calls)
    raise ValueError(f"unknown workflow {workflow!r}")


def finished(workflow: str, calls: list[Call], state: dict[str, Any]) -> bool:
    return next_call(workflow, calls, state) is None


PLAN = {
    "get_open_cells": "Find the shifts this request opens.",
    "rank_replacements": "Re-solve and rank replacements.",
    "request_approval": "Ask the charge nurse to approve.",
    "send_message": "Ask the chosen nurse to cover.",
    "wait_for_reply": "Wait for the nurse's answer.",
    "escalate": "Hand this to the charge nurse.",
    "diagnose": "Find which rules clash.",
    "list_conflict_options": "List what the charge nurse can do.",
    "apply_option": "Confirm the chosen option.",
    "run_resolve": "Re-solve with the change.",
    "run_full": "Build the roster for the period.",
    "what_if": "Check what granting it would change.",
    "rule_check_swap": "Check the swap against the hard rules.",
    "explain_denied_requests": "Find why requests were denied.",
    "get_run_summary": "Summarise the roster.",
}


# ---------------------------------------------------------------- summaries


def approval_summary(calls: list[Call]) -> tuple[str, list[dict[str, Any]]]:
    """Text and claims from the newest result worth showing the charge nurse."""
    for c in reversed(calls):
        if not _ok(c):
            continue
        r, cid, tool = c["result"], c["call_id"], c["tool"]
        if tool == "rank_replacements" and r.get("status") == "proposed":
            if r.get("candidates"):
                sents = describe.proposal(r, cid)[:2]
            else:
                n = r.get("changed_cells") or 0
                sents = [describe.Sentence(
                    f"Re-planned roster: {n} changed cell{'s' if n != 1 else ''}; no open shift "
                    "needs a replacement.",
                    [describe.claim(f"{n} changed", cid, "changed_cells", n)])]  # fmt: skip
            return _join(sents)
        if tool in ("run_resolve", "run_full", "get_run_summary") and r.get("stages"):
            sents = describe.summary(r, cid)
            n = r.get("changed_cells")
            if n is not None:
                sents.insert(0, describe.Sentence(
                    f"{n} changed cell{'s' if n != 1 else ''}.",
                    [describe.claim(f"{n} changed", cid, "changed_cells", n)]))  # fmt: skip
            return _join(sents[:3])
        if tool == "list_conflict_options":
            opts = r.get("options", [])
            text = "Pick one: " + "; ".join(f"{o['option_id']} {o['label']}" for o in opts) + "."
            claims = [describe.claim(o["option_id"], cid, f"options.{i}.option_id", o["option_id"])
                      for i, o in enumerate(opts)]  # fmt: skip
            return text, claims
        if tool == "what_if":
            kind = str(r.get("kind", "")).replace("_", " ")
            text = (
                f"Granting {r.get('name')}'s {kind}: {r.get('verdict')} "
                f"({r.get('reason_code')}), {r.get('changed_cells')} changed cells."
            )
            claims = [
                describe.claim("verdict", cid, "verdict", r.get("verdict")),
                describe.claim("reason", cid, "reason_code", r.get("reason_code")),
                describe.claim("changed", cid, "changed_cells", r.get("changed_cells")),
            ]
            return text, claims
    return "Ready for your decision.", []


def _join(sents: list[describe.Sentence]) -> tuple[str, list[dict[str, Any]]]:
    return " ".join(s.text for s in sents), [x for s in sents for x in s.claims]


def brief(tool: str, result: dict[str, Any], names: dict[str, str]) -> str:
    """One short line for the Agent tab."""
    if "error" in result:
        return f"error: {str(result['error'])[:120]}"

    def nm(n: Any) -> str:
        return names.get(str(n), str(n))

    if tool == "get_open_cells":
        cells = result.get("open_cells", [])
        return ", ".join(f"{c['day']} {c['shift']}" for c in cells) or "no published cell opens"
    if tool == "rank_replacements":
        if result.get("status") != "proposed":
            return f"{result.get('status')}: {result.get('note', '')}".strip()
        cands = result.get("candidates", [])
        if not cands:
            return f"re-planned, {result.get('changed_cells')} changed cells, no open shift"
        top = cands[0]
        cells = top.get("changed_cells")
        return (
            f"{len(cands)} ranked; top {nm(top['nurse'])} ({cells} changed "
            f"cell{'s' if cells != 1 else ''}), {len(result.get('excluded', []))} excluded"
        )
    if tool == "send_message":
        return f"to {nm(result.get('nurse_id'))} ({result.get('source')}): {result.get('text')}"
    if tool in ("run_resolve", "run_full"):
        return f"{result.get('status')} {result.get('run_id')}"
    if tool == "what_if":
        return f"{result.get('verdict')} {result.get('reason_code')}"
    if tool == "list_conflict_options":
        return f"{len(result.get('options', []))} options"
    if tool == "diagnose":
        return f"core: {', '.join(result.get('core', []))}"
    if tool == "explain_denied_requests":
        return f"{len(result.get('denied', []))} denied requests explained"
    if tool == "rule_check_swap":
        return "legal" if result.get("legal") else f"breaks {result.get('broken')}"
    if tool == "escalate":
        return str(result.get("reason", ""))
    return ", ".join(f"{k}" for k in list(result)[:4])
