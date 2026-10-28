"""Intake scoring (design doc 9.3): code only, no LLM judge.

A case passes when intent, the clarification flag and the multiset of edits match. Edits are
compared on op, resolved nurse id, dates and shifts (plus counterpart cells and target_total when
the case names them), after semantic validation. Out-of-scope cases pass only if no edit is
produced.
"""

import json
import statistics
from typing import Any

BASE = ("op", "nurse", "dates", "shifts")
OPTIONAL = ("counterpart", "counterpart_dates", "counterpart_shifts", "target_total")


def _norm(v: Any) -> Any:
    return tuple(sorted(v)) if isinstance(v, list) else v


def project(edit: dict[str, Any], fields: tuple[str, ...]) -> str:
    return json.dumps({f: _norm(edit.get(f)) for f in fields}, sort_keys=True)


def score_case(expect: dict[str, Any], got: dict[str, Any]) -> tuple[bool, list[str]]:
    """`got`: {intent, clarify, edits (resolved keys, empty unless confirmed), error}."""
    if got.get("error"):
        return False, [f"error: {got['error']}"]
    if expect["intent"] == "out_of_scope":
        ok = not got["edits"]
        return ok, [] if ok else ["an edit was produced for an out-of-scope message"]
    reasons = []
    if got["intent"] != expect["intent"]:
        reasons.append(f"intent {got['intent']} != {expect['intent']}")
    if got["clarify"] != expect["needs_clarification"]:
        reasons.append(f"clarify {got['clarify']} != {expect['needs_clarification']}")
    exp_edits = expect.get("edits", [])
    fields = BASE + tuple(f for f in OPTIONAL if any(f in e for e in exp_edits))
    want = sorted(project(e, fields) for e in exp_edits)
    have = sorted(project(e, fields) for e in got["edits"])
    if want != have:
        reasons.append(f"edits {have} != {want}")
    return not reasons, reasons


def _pct(xs: list[bool]) -> float:
    return round(100 * sum(xs) / len(xs), 1) if xs else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    lat = sorted(r["latency_ms"] for r in rows if not r.get("error"))
    exp_cl = [r["expect"]["needs_clarification"] for r in rows]
    got_cl = [r["got"]["clarify"] for r in rows]
    tp = sum(e and g for e, g in zip(exp_cl, got_cl, strict=True))
    cost = sum(r["cost_usd"] for r in rows)
    return {
        "cases": n,
        "first_pass_accuracy": _pct([r["pass"] and r["first_pass_ok"] for r in rows]),
        "accuracy_after_repair": _pct([r["pass"] for r in rows]),
        "clarification_precision": round(100 * tp / sum(got_cl), 1) if sum(got_cl) else None,
        "clarification_recall": round(100 * tp / sum(exp_cl), 1) if sum(exp_cl) else None,
        "first_pass_schema_failure_rate": _pct([not r["first_pass_ok"] for r in rows]),
        "schema_failure_rate": _pct([r["schema_failed"] for r in rows]),
        "errors": sum(bool(r.get("error")) for r in rows),
        "latency_p50_ms": int(statistics.median(lat)) if lat else None,
        "latency_p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None,
        "cost_usd_total": round(cost, 4),
        "cost_usd_per_case": round(cost / n, 5) if n else 0.0,
        "cache_hits": sum(r["cache_hits"] for r in rows),
        "by_tag": _by_tag(rows),
    }


def _by_tag(rows: list[dict[str, Any]]) -> dict[str, str]:
    tags: dict[str, list[bool]] = {}
    for r in rows:
        tags.setdefault(r["tags"][0], []).append(r["pass"])
    return {t: f"{sum(v)}/{len(v)}" for t, v in sorted(tags.items())}


# ---------------------------------------------------------------- explain suite (9.3)

HEALTH = ["fever", "bukhar", "sick", "bimar", "pregnan", "illness", "medicine", "symptom"]


def fact_found(fact: dict[str, Any], answer: str, claims: list[dict[str, Any]]) -> bool:
    """A required fact is cited by a claim (path suffix, and value when given) or stated."""
    suffix = fact.get("path_suffix")
    for c in claims:
        hit = bool(suffix) and str(c.get("path", "")).endswith(str(suffix))
        if hit and ("value" not in fact or c.get("value") == fact["value"]):
            return True
    text = fact.get("text")
    return bool(text) and str(text).lower() in answer.lower()


def score_explain(expect: dict[str, Any], got: dict[str, Any]) -> dict[str, Any]:
    if got.get("error"):
        return {
            "pass": False,
            "reasons": [f"error: {got['error']}"],
            "tool_ok": False,
            "facts": 0,
            "facts_total": len(expect.get("facts", [])),
            "forbidden": [],
        }
    answer, claims = got["answer"], got["claims"]
    tools = [t["tool"] for t in got["tool_calls"]]
    want = expect.get("tools_any", [])
    tool_ok = (not tools) if not want else any(t in tools for t in want)
    facts = expect.get("facts", [])
    found = sum(fact_found(f, answer, claims) for f in facts)
    words = expect.get("forbidden", []) + ([] if expect.get("refuse") else [])
    bad = [w for w in words if w.lower() in answer.lower()]
    reasons = []
    if not tool_ok:
        reasons.append(f"tools {tools} (wanted one of {want or 'none'})")
    if found < len(facts):
        reasons.append(f"facts {found}/{len(facts)}")
    if bad:
        reasons.append(f"forbidden words {bad}")
    return {"pass": not reasons, "reasons": reasons, "tool_ok": tool_ok, "facts": found,
            "facts_total": len(facts), "forbidden": bad}  # fmt: skip


def summarize_explain(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    ok = [r for r in rows if not r.get("error")]
    lat = sorted(r["latency_ms"] for r in ok)
    facts = sum(r["score"]["facts_total"] for r in rows)
    cost = sum(r.get("cost_usd", 0.0) for r in rows)
    return {
        "cases": n,
        "pass_rate": _pct([r["score"]["pass"] for r in rows]),
        "grounding_rate": _pct([bool(r.get("grounded")) for r in rows]),
        "fallback_rate": _pct([bool(r.get("fallback_used")) for r in rows]),
        "retry_rate": _pct([bool(r.get("retried")) for r in rows]),
        "tool_choice_accuracy": _pct([r["score"]["tool_ok"] for r in rows]),
        "fact_recall": round(100 * sum(r["score"]["facts"] for r in rows) / facts, 1)
        if facts
        else None,
        "forbidden_violations": sum(bool(r["score"]["forbidden"]) for r in rows),
        "errors": n - len(ok),
        "latency_p50_ms": int(statistics.median(lat)) if lat else None,
        "latency_p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None,
        "cost_usd_total": round(cost, 4),
        "cost_usd_per_case": round(cost / n, 5) if n else 0.0,
        "by_tag": _by_tag([{**r, "pass": r["score"]["pass"]} for r in rows]),
    }
