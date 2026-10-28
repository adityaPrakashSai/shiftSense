"""Deterministic explanations from tool results (design doc 7.4 fallback, 9.2 mock).

Each function turns one tool result into short sentences, and every sentence carries the claims
that support it (paths into that result, with their exact values), so the text always passes the
grounding check. Used when a model answer is withheld, and by the mock explainer.
"""

from dataclasses import dataclass, field
from typing import Any

RULES = {
    "rest_after_night": "rest after night",
    "quick_return": "too little rest between shifts",
    "max_consecutive_days": "too many days in a row",
    "max_consecutive_nights": "night block too long",
    "max_nights": "night cap for the period",
    "max_shifts": "contract maximum",
    "max_hours": "hours limit",
    "unavailable": "unavailable",
    "leave": "on leave",
    "night_qualified": "not night-qualified",
    "night_exempt": "exempt from nights",
    "standby_off_roster": "standby not called in a full plan",
    "frozen": "shift already fixed",
    "fixed_duty": "fixed duty",
    "min_cover": "minimum cover",
    "max_cover": "maximum cover",
    "senior_cover": "senior cover",
    "no_feasible_roster": "no legal roster with this nurse",
}
SHIFT = {"M": "Morning", "E": "Evening", "N": "Night", "off": "off"}


def rule_text(name: str) -> str:
    head = name.split(":", 1)[0]
    return RULES.get(head, head.replace("_", " "))


def rules_text(names: list[str]) -> str:
    out: list[str] = []
    for n in names:
        t = rule_text(n)
        if t not in out:
            out.append(t)
    return ", ".join(out)


@dataclass
class Sentence:
    text: str
    claims: list[dict[str, Any]] = field(default_factory=list)


def claim(text: str, call_id: str, path: str, value: Any) -> dict[str, Any]:
    return {"text": text, "call_id": call_id, "path": path, "value": value}


def proposal(r: dict[str, Any], cid: str, about: str | None = None) -> list[Sentence]:
    out = []
    cands = r.get("candidates", [])
    if cands:
        c = cands[0]
        cells = c["changed_cells"]
        txt = f"{c['name']} is ranked first: {cells} changed cell{'s' if cells != 1 else ''}"
        cl = [claim(f"{cells} changed", cid, "candidates.0.changed_cells", cells)]
        if c.get("tie"):
            txt += f", a tie decided by {c.get('tie_break')}"
            cl.append(claim("tie", cid, "candidates.0.tie", True))
        out.append(Sentence(txt + ".", cl))
    for i, c in enumerate(cands[1:], 1):
        if about and about not in (c["nurse"], c["name"]):
            continue
        worse = ", ".join(f"+{v} {t}" for t, v in (c.get("delta_vs_best") or {}).items() if v)
        txt = f"{c['name']} is ranked {c['rank']}"
        cl = [claim(f"rank {c['rank']}", cid, f"candidates.{i}.rank", c["rank"])]
        if worse:
            txt += f" ({worse} compared with the first choice)"
            t, v = next((t, v) for t, v in c["delta_vs_best"].items() if v)
            cl.append(claim(f"+{v} {t}", cid, f"candidates.{i}.delta_vs_best.{t}", v))
        out.append(Sentence(txt + ".", cl))
    for i, e in enumerate(r.get("excluded", [])):
        if about and about not in (e["nurse"], e.get("name")):
            continue
        out.append(
            Sentence(
                f"{e.get('name') or e['nurse']} cannot take it: {rules_text(e['rules'])}.",
                [claim(rule_text(e["rules"][0]), cid, f"excluded.{i}.rules.0", e["rules"][0])],
            )
        )
    return out


def compare(r: dict[str, Any], cid: str) -> list[Sentence]:
    a, b = r.get("name_a"), r.get("name_b")
    tail = ", keeping everyone else's roster fixed" if r.get("scope") == "local" else ""
    v = r.get("verdict")
    if r.get("b_current_shift") == r.get("shift"):
        return [
            Sentence(
                f"{b} is already working that {SHIFT.get(str(r.get('shift')), r.get('shift'))} "
                "shift, so was not a candidate for it.",
                [claim("already on", cid, "b_current_shift", r.get("b_current_shift"))],
            )
        ]
    if v == "IMPOSSIBLE":
        rules = r.get("b_rule_check") or r.get("core") or []
        return [
            Sentence(
                f"{b} cannot take that shift instead of {a}: {rules_text(rules)}{tail}.",
                [claim("impossible", cid, "verdict", "IMPOSSIBLE")],
            )
        ]
    if v == "WORSE":
        t, d = next(iter((r.get("delta") or {}).items()), ("", 0))
        return [
            Sentence(
                f"Putting {b} there instead of {a} makes the roster worse on {t} (+{d}){tail}.",
                [claim("worse", cid, "verdict", "WORSE"), claim(f"+{d}", cid, f"delta.{t}", d)],
            )
        ]
    if v == "EQUAL":
        return [
            Sentence(
                f"{a} and {b} are equally good there; it was a tie{tail}.",
                [claim("tie", cid, "verdict", "EQUAL")],
            )
        ]
    return [
        Sentence(
            f"Putting {b} there would be at least as good{tail}; the stored roster was a best-found"
            " solution, not a proven one.",
            [claim("better", cid, "verdict", str(v))],
        )
    ]


def summary(r: dict[str, Any], cid: str) -> list[Sentence]:
    out = []
    for i, s in enumerate(r.get("stages", [])):
        if s.get("skipped"):
            continue
        state = "proven optimal" if s["proven"] else "best found"
        out.append(
            Sentence(
                f"{s['tier'].capitalize()}: {s['best']} ({state}).",
                [claim(f"{s['tier']} {s['best']}", cid, f"stages.{i}.best", s["best"])],
            )
        )
    req = r.get("requests") or {}
    out.append(
        Sentence(
            f"{req.get('denied', 0)} requests denied, {req.get('granted', 0)} granted.",
            [claim("denied", cid, "requests.denied", req.get("denied", 0))],
        )
    )
    return out


def outcome(r: dict[str, Any], cid: str) -> list[Sentence]:
    out = []
    for i, q in enumerate(r.get("requests", [])):
        if q.get("outcome") is None:
            continue
        when = ", ".join(q.get("dates") or [])
        kind = q["kind"].replace("_", " ")
        txt = f"Request {q['request_id']} ({kind}, {when}): {q['outcome'].lower()}"
        cl = [claim(q["outcome"], cid, f"requests.{i}.outcome", q["outcome"])]
        if q["outcome"] == "DENIED" and q.get("reason_code"):
            txt += f", reason {q['reason_code']}"
            cl.append(claim("reason", cid, f"requests.{i}.reason_code", q["reason_code"]))
        out.append(Sentence(txt + ".", cl))
    return out or [Sentence("No outcome is recorded for those requests in this roster yet.")]


def fairness(r: dict[str, Any], cid: str) -> list[Sentence]:
    cur, early = r.get("current_period", {}), r.get("earlier_periods", {})
    avg = cur.get("ward_average", {})
    return [
        Sentence(
            f"This period {r['name']} has {cur.get('weekend_shifts', 0)} weekend shifts and "
            f"{cur.get('nights', 0)} nights; the ward average is {avg.get('weekend_shifts', 0)} "
            f"weekend shifts and {avg.get('nights', 0)} nights.",
            [
                claim(
                    "weekend shifts",
                    cid,
                    "current_period.weekend_shifts",
                    cur.get("weekend_shifts", 0),
                ),
                claim(
                    "average",
                    cid,
                    "current_period.ward_average.weekend_shifts",
                    avg.get("weekend_shifts", 0),
                ),
            ],
        ),  # fmt: skip
        Sentence(
            f"Over the {early.get('periods', 0)} earlier periods: {early.get('weekends', 0)} "
            f"weekends and {early.get('nights', 0)} nights.",
            [claim("earlier weekends", cid, "earlier_periods.weekends", early.get("weekends", 0))],
        ),
    ]


def assignment(r: dict[str, Any], cid: str) -> list[Sentence]:
    shift = SHIFT.get(str(r.get("shift")), str(r.get("shift")))
    out = [
        Sentence(
            f"{r['name']} is on {shift} on {r['date']}"
            + (f" (changed from {SHIFT.get(str(r['prev_shift']), r['prev_shift'] or 'off')})"
               if r.get("changed") else "") + ".",
            [claim("shift", cid, "shift", r.get("shift"))],
        )
    ]  # fmt: skip
    blocked = [(i, o) for i, o in enumerate(r.get("other_shifts", [])) if not o["allowed"]]
    for i, o in blocked[:2]:
        out.append(
            Sentence(
                f"{SHIFT.get(o['shift'], o['shift'])} instead would break: "
                f"{rules_text(o['rules'])}.",
                [claim("blocked", cid, f"other_shifts.{i}.allowed", False)],
            )
        )
    return out


def nurse_matches(r: dict[str, Any], cid: str) -> list[Sentence]:
    m = r.get("matches", [])
    if not m:
        return [Sentence(f"No nurse matches {r.get('query')!r}.")]
    return [
        Sentence(f"{m[0]['name']} is {m[0]['id']}.", [claim("id", cid, "matches.0.id", m[0]["id"])])
    ]


BY_TOOL = {
    "get_proposal": proposal,
    "compare_candidates": compare,
    "get_run_summary": summary,
    "get_request_outcome": outcome,
    "get_fairness_history": fairness,
    "get_assignment_evidence": assignment,
    "find_nurse": nurse_matches,
}


def from_calls(calls: list[dict[str, Any]], about: str | None = None, limit: int = 3) -> Sentence:
    """One answer (text + claims) from wrapped tool results {"call_id", "tool", "result"}."""
    parts: list[Sentence] = []
    for c in calls:
        res = c.get("result") or {}
        if "error" in res:
            continue
        fn = BY_TOOL.get(c["tool"])
        if fn is None:
            continue
        sents = proposal(res, c["call_id"], about) if fn is proposal else fn(res, c["call_id"])
        parts += sents
    parts = parts[:limit]
    if not parts:
        return Sentence("I could not find evidence for that in the roster.")
    return Sentence(" ".join(p.text for p in parts), [x for p in parts for x in p.claims])
