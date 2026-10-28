"""MockProvider (design doc 9.2): deterministic responses so every screen and test works without
network or credits. It is the only non-Meta provider, never the default in the deployed app, and
the UI shows a MOCK MODE badge while it is active.

- Intake: a small rule-based parser (sick words, aaj / kal / parson, weekday names, shift words)
  that emits a valid ConstraintEdit; anything else gets a clarification.
- Narrate / message: template text. Explain: `app/explain/mock.py` calls the real tools and
  answers from `describe()` templates, so the grounding check passes.
- Fixtures: tests/fixtures/mock/*.json pin exact responses by cache key, for tests that need odd
  outputs (malformed JSON, a wrong claim) to exercise repair and grounding paths.

Context convention: the intake, narrate and message prompts put a JSON object between
`<context>` and `</context>` in a message (today, sender_id, nurses, facts...). The mock reads it;
real models just see it as part of the prompt.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.llm.types import LLMRequest, ProviderCaps, ProviderResult, Role

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "mock"
_CTX = re.compile(r"<context>(.*?)</context>", re.DOTALL)
_MSG = re.compile(r"<message>(.*?)</message>", re.DOTALL)

SICK = [
    "sick",
    "fever",
    "temperature",
    "unwell",
    "not well",
    "emergency",
    "bukhar",
    "bimar",
    "beemar",
    "tabiyat",
    "बुखार",
    "बीमार",
    "तबीयत",
]
CANT = [
    "can't",
    "cannot",
    "cant ",
    "won't be able",
    "nahi kar paungi",
    "nahi kar paunga",
    "nahi aa paungi",
    "nahi aa paunga",
    "nahi aa sakti",
    "nahi aa sakta",
    "नहीं कर पाऊँगी",
    "नहीं कर पाऊंगी",
    "नहीं आ पाऊँगी",
]
LEAVE = ["leave", "chhutti", "chutti", "छुट्टी"]
OFF = ["day off", "off chahiye", "off chaiye", "off please", "want off", "ko off", "छुट्टी चाहिए"]
AVOID = [
    "no night",
    "no nights",
    "night nahi",
    "nights nahi",
    "night duty nahi",
    "raat nahi",
    "avoid",
]
WANT = ["i want", "i'd like", "would like", "chahiye", "prefer"]
OUT_OF_SCOPE = [
    "medicine",
    "dawai",
    "dawa",
    "tablet",
    "ignore your rules",
    "ignore previous",
    "ignore all",
]
QUESTION = ["why", "kyun", "kyon", "क्यों"]
HINGLISH = [
    "nahi",
    "kal",
    "aaj",
    "chahiye",
    "mujhe",
    "subah",
    "raat",
    "shaam",
    "duty",
    "kar",
    "hai",
    "paungi",
    "paunga",
    "parson",
    "di ",
]

WEEKDAYS = {
    0: ["monday", "mon", "somvar", "somwar", "somvaar", "सोमवार"],
    1: ["tuesday", "tue", "tues", "mangalvar", "mangalwar", "mangalvaar", "मंगलवार"],
    2: ["wednesday", "wed", "budhvar", "budhwar", "budhvaar", "बुधवार"],
    3: ["thursday", "thu", "thurs", "guruvar", "guruwar", "guruvaar", "गुरुवार"],
    4: ["friday", "fri", "shukravar", "shukrawar", "shukravaar", "शुक्रवार"],
    5: ["saturday", "sat", "shanivar", "shaniwar", "shanivaar", "शनिवार"],
    6: ["sunday", "sun", "ravivar", "raviwar", "ravivaar", "रविवार"],
}
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
SHIFT_WORDS = {
    "M": ["morning", "subah", "सुबह"],
    "E": ["evening", "shaam", "sham ", "शाम"],
    "N": ["night", "raat", "रात"],
}


def read_context(req: LLMRequest) -> dict[str, Any]:
    for m in req.messages:
        content = m.get("content")
        if isinstance(content, str) and (hit := _CTX.search(content)):
            try:
                return dict(json.loads(hit.group(1)))
            except json.JSONDecodeError:
                return {}
    return {}


def last_user_text(req: LLMRequest) -> str:
    for m in reversed(req.messages):
        if m.get("role") == "user":
            content = m.get("content")
            text = (
                content
                if isinstance(content, str)
                else " ".join(p.get("text", "") for p in content or [] if isinstance(p, dict))
            )
            return _CTX.sub("", text).strip()
    return ""


def message_text(req: LLMRequest) -> str:
    """The sender's message: the first `<message>` block in a user turn (a repair turn has none),
    else the last user turn without its context block."""
    for m in req.messages:
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, str) and (hit := _MSG.search(content)):
            return hit.group(1).strip()
    return last_user_text(req)


def _has(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)


def _language(text: str) -> str:
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    low = f" {text.lower()} "
    return "hinglish" if sum(w in low for w in HINGLISH) >= 2 else "en"


def _dates(low: str, today: date) -> list[date]:
    found: list[date] = []
    if _has(low, ["aaj", "today", "tonight", "आज"]):
        found.append(today)
    if _has(low, ["kal", "tomorrow", "कल"]):
        found.append(today + timedelta(days=1))
    if _has(low, ["parson", "day after tomorrow", "परसों"]):
        found.append(today + timedelta(days=2))
    for wd, names in WEEKDAYS.items():
        if any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", low) for n in names):
            ahead = (wd - today.weekday()) % 7 or 7
            found.append(today + timedelta(days=ahead))
    num = r"(\d{1,2})(?:st|nd|rd|th)?"
    rng = rf"\b{num}\s*(?:to|-|se|till|until)\s*{num}\s+([a-z]{{3}})"
    for a, b, mon in re.findall(rng, low):
        if mon in MONTHS and int(a) <= int(b):
            year = today.year + (1 if MONTHS[mon] < today.month else 0)
            with contextlib.suppress(ValueError):
                found += [date(year, MONTHS[mon], d) for d in range(int(a), int(b) + 1)]
    for day, mon in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})", low):
        if mon in MONTHS:
            year = today.year + (1 if MONTHS[mon] < today.month else 0)
            with contextlib.suppress(ValueError):  # "31 nov" is not a date
                found.append(date(year, MONTHS[mon], int(day)))
    return sorted(set(found))


def _shifts(low: str) -> list[str] | None:
    got = [s for s, words in SHIFT_WORDS.items() if _has(low, words)]
    return got or None


def _named(low: str, ctx: dict[str, Any]) -> list[str]:
    """Names from the context nurse list ("N07 | Meena Sharma | Meena, मीना") found in the text,
    longest first, as written in the list (the pipeline resolves them)."""
    sender = str(ctx.get("sender", {}).get("id", ""))
    found: list[tuple[int, str]] = []
    for row in ctx.get("nurses", []):
        parts = [p.strip() for p in str(row).split("|")]
        if len(parts) < 2 or parts[0] == sender:
            continue
        names = [parts[1], *[a.strip() for a in (parts[2] if len(parts) > 2 else "").split(",")]]
        for nm in sorted((n for n in names if n), key=len, reverse=True):
            if re.search(rf"(?<!\w){re.escape(nm.lower())}(?!\w)", low):
                found.append((low.index(nm.lower()), nm))
                break
    return [nm for _pos, nm in sorted(found)]


def _edit(op: str, evidence: str, **kw: Any) -> dict[str, Any]:
    edit: dict[str, Any] = {
        "op": op,
        "nurse": "self",
        "dates": [],
        "shifts": None,
        "counterpart": None,
        "counterpart_dates": [],
        "counterpart_shifts": None,
        "request_id": None,
        "target_total": None,
        "reason_category": None,
        "evidence_span": evidence,
    }
    edit.update(kw)
    return edit


def _swap(text: str, low: str, today: date, ctx: dict[str, Any]) -> dict[str, Any] | None:
    """ "her Sat night for my Sun morning": the sender gives "my ..." and takes "her ..."."""
    names = _named(low, ctx)
    me, them = r"\b(?:my|mera|meri|mere)\b", r"\b(?:her|his|their|uski|unki|uska)\b"
    mine = re.search(rf"{me}(.*?)(?={them}|$)", low)
    theirs = re.search(rf"{them}(.*?)(?={me}|$)", low)
    if not (names and mine and theirs):
        return None
    d1, s1 = _dates(f" {mine.group(1)} ", today), _shifts(f" {mine.group(1)} ")
    d2, s2 = _dates(f" {theirs.group(1)} ", today), _shifts(f" {theirs.group(1)} ")
    if len(d1) != 1 or len(d2) != 1 or not s1 or not s2:
        return None
    return _edit(
        "propose_swap",
        text.strip(),
        dates=[d1[0].isoformat()],
        shifts=s1,
        counterpart=names[0],
        counterpart_dates=[d2[0].isoformat()],
        counterpart_shifts=s2,
    )


def _demand(text: str, low: str, today: date, ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Charge nurse: "we need one more on Saturday night", "Thursday night needs 5"."""
    dates, shifts = _dates(low, today), _shifts(low)
    if len(dates) != 1 or not shifts or len(shifts) != 1:
        return None
    cur = next(
        (
            int(r.get("target", 0))
            for r in ctx.get("demand", [])
            if r.get("date") == dates[0].isoformat() and r.get("shift") == shifts[0]
        ),
        None,
    )
    if (m := re.search(r"needs?\s+(\d+)", low)) is not None:
        target = int(m.group(1))
    elif cur is not None and _has(low, ["one more", "1 more", "one extra", "ek aur"]):
        target = cur + 1
    elif cur is not None and _has(low, ["one less", "1 less", "ek kam"]):
        target = cur - 1
    else:
        return None
    return _edit(
        "change_demand",
        text.strip(),
        nurse="self",
        dates=[dates[0].isoformat()],
        shifts=shifts,
        target_total=target,
    )


def mock_intake(text: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Rule-based ConstraintEdit for one message."""
    low = f" {text.lower()} "
    lang = _language(text)
    today = date.fromisoformat(str(ctx.get("today", date.today().isoformat())))
    charge = ctx.get("sender", {}).get("role") == "charge"
    base: dict[str, Any] = {
        "edits": [],
        "question": None,
        "needs_clarification": False,
        "clarification": None,
        "language": lang,
    }

    if _has(low, OUT_OF_SCOPE):
        return {**base, "intent": "out_of_scope", "confidence": 0.9}

    if "swap" in low and (swap := _swap(text, low, today, ctx)) is not None:
        return {**base, "intent": "edit", "edits": [swap], "confidence": 0.8}
    if "cancel" in low and (rid := re.search(r"\br-\d{4}\b", low)):
        edit = _edit("cancel_request", text.strip(), request_id=rid.group(0).upper())
        return {**base, "intent": "edit", "edits": [edit], "confidence": 0.85}
    if charge and (dm := _demand(text, low, today, ctx)) is not None:
        return {**base, "intent": "edit", "edits": [dm], "confidence": 0.85}

    sick = _has(low, SICK)
    op = None
    if sick or _has(low, CANT):
        op = "mark_unavailable"
    elif _has(low, OFF):
        op = "request_day_off"
    elif _has(low, LEAVE):
        op = "request_leave"
    elif _has(low, AVOID):
        op = "avoid_shift"
    elif _has(low, WANT) and _shifts(low):
        op = "request_shift"

    if op is None:
        if _has(low, QUESTION) or text.strip().endswith("?"):
            return {**base, "intent": "question", "question": text.strip(), "confidence": 0.8}
        if re.fullmatch(r"\s*(hi|hello|hey|namaste|नमस्ते)[\s!.]*", low):
            return {**base, "intent": "greeting", "confidence": 0.9}
        ask = {
            "en": "Could you tell me which day and shift you mean?",
            "hinglish": "Kaunsa din aur kaunsi shift? Thoda bata dijiye.",
            "hi": "कौन सा दिन और कौन सी शिफ्ट? कृपया बताइए।",
        }[lang]
        return {
            **base,
            "intent": "edit",
            "needs_clarification": True,
            "clarification": ask,
            "confidence": 0.3,
        }

    dates = _dates(low, today)
    if not dates:
        ask = {
            "en": "Which day do you mean?",
            "hinglish": "Kis din ke liye?",
            "hi": "किस दिन के लिए?",
        }[lang]
        return {
            **base,
            "intent": "edit",
            "needs_clarification": True,
            "clarification": ask,
            "confidence": 0.4,
        }
    shifts = None if op in ("request_day_off", "request_leave") else _shifts(low)
    if op == "avoid_shift" and shifts is None:
        shifts = ["N"]
    names = _named(low, ctx) if charge else []
    edit = _edit(
        op,
        text.strip(),
        nurse=names[0] if names else "self",
        dates=[d.isoformat() for d in dates],
        shifts=shifts,
        reason_category="sick" if sick else ("personal" if op != "mark_unavailable" else "other"),
    )
    return {**base, "intent": "edit", "edits": [edit], "confidence": 0.85}


def mock_message(ctx: dict[str, Any]) -> dict[str, Any]:
    name = ctx.get("nurse_name", "")
    when = f"{ctx.get('date', '')} {ctx.get('shift_name', '')} ({ctx.get('times', '')})".strip()
    if ctx.get("language") == "hi":
        text = (
            f"Namaste {name}, kya aap {when} ki duty cover kar sakte hain? "
            "Kripya Accept ya Decline karein."
        )
    else:
        text = f"Hello {name}, could you cover {when}? Please tap Accept or Decline."
    return {"message": " ".join(text.split())}


class MockProvider:
    name = "mock"
    model = "mock-rule-based"
    caps = ProviderCaps(native_tools=True, json_schema=True, vision=False, audio=False)

    def __init__(self, fixtures_dir: Path = FIXTURES) -> None:
        self.pinned: dict[str, dict[str, Any]] = {}
        if fixtures_dir.is_dir():
            for p in sorted(fixtures_dir.glob("*.json")):
                fx = json.loads(p.read_text(encoding="utf-8"))
                self.pinned[fx["cache_key"]] = fx["response"]
        self.calls = 0

    def pinned_result(self, key: str) -> ProviderResult | None:
        fx = self.pinned.get(key)
        return (
            None
            if fx is None
            else ProviderResult(text=fx.get("text"), reasoning=fx.get("reasoning"))
        )

    async def complete(self, req: LLMRequest, *, json_protocol: bool) -> ProviderResult:
        self.calls += 1
        ctx = read_context(req)
        if req.role is Role.INTAKE:
            body: dict[str, Any] = mock_intake(message_text(req), ctx)
        elif req.role is Role.MESSAGE:
            body = mock_message(ctx)
        elif req.role is Role.NARRATE:
            opts = ctx.get("options", [])
            body = {
                "summary": "The solver cannot meet every hard rule here; pick one option.",
                "options": [
                    {"option_id": o.get("option_id"), "label": o.get("label", "")}
                    for o in opts
                    if isinstance(o, dict)
                ],
            }
        elif req.role in (Role.EXPLAIN, Role.AGENT):
            from app.explain.mock import respond  # the explainer imports llm; import lazily

            return respond(req)
        else:
            body = {"text": "mock"}
        return ProviderResult(text=json.dumps(body, ensure_ascii=False))
