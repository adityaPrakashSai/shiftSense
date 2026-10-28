"""Grounding check (design doc 7.4): every claim's `path` must exist in the cited tool result and
equal `value`.

A claim is {"text", "call_id", "path", "value"}; `path` is dot-separated inside the result
("candidates.0.changed_cells", "excluded.1.rules.0"). Numbers compare by value (1 == 1.0),
strings exactly after trimming.
"""

from typing import Any

MISSING = object()


def lookup(obj: Any, path: str) -> Any:
    cur = obj
    for part in [p for p in str(path).split(".") if p != ""]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif (
            isinstance(cur, list)
            and part.lstrip("-").isdigit()
            and -len(cur) <= int(part) < len(cur)
        ):
            cur = cur[int(part)]
        else:
            return MISSING
    return cur


def same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or (isinstance(a, bool) and isinstance(b, bool) and a == b)
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip() == b.strip()
    return bool(a == b)


def check(claims: list[dict[str, Any]], results: dict[str, Any]) -> list[str]:
    """Problems, one line each; an empty list means every claim is grounded."""
    problems = []
    for i, c in enumerate(claims):
        cid, path = c.get("call_id"), c.get("path")
        if cid not in results:
            problems.append(f"claim {i}: call_id {cid!r} is not a tool call in this conversation")
            continue
        got = lookup(results[cid], str(path or ""))
        if got is MISSING:
            problems.append(f"claim {i}: path {path!r} does not exist in {cid}")
        elif not same(got, c.get("value")):
            problems.append(f"claim {i}: {cid}.{path} is {got!r}, not {c.get('value')!r}")
    return problems
