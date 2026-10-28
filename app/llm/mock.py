"""MockProvider (design doc 9.2): deterministic responses so every screen and test works without
network or credits. It is the only non-Meta provider, never the default in the deployed app, and
the UI shows a MOCK MODE badge while it is active.

- Intake: a small rule-based parser (sick words, aaj / kal / parson, weekday names, shift words)
  that emits a valid ConstraintEdit; anything else gets a clarification.
- Narrate / message: template text. Explain: wired in M8 (calls the real tools then).
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
    0: ["monday", "somvar", "somwar", "सोमवार"],
    1: ["tuesday", "mangalvar", "mangalwar", "मंगलवार"],
    2: ["wednesday", "budhvar", "budhwar", "बुधवार"],
    3: ["thursday", "guruvar", "guruwar", "brihaspativar", "गुरुवार"],
    4: ["friday", "shukravar", "shukrawar", "शुक्रवार"],
    5: ["saturday", "shanivar", "shaniwar", "शनिवार"],
    6: ["sunday", "ravivar", "raviwar", "रविवार"],
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
        if _has(low, names):
            ahead = (wd - today.weekday()) % 7 or 7
            found.append(today + timedelta(days=ahead))
    for day, mon in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})", low):
        if mon in MONTHS:
            year = today.year + (1 if MONTHS[mon] < today.month else 0)
            with contextlib.suppress(ValueError):  # "31 nov" is not a date
                found.append(date(year, MONTHS[mon], int(day)))
    return sorted(set(found))


def _shifts(low: str) -> list[str] | None:
    got = [s for s, words in SHIFT_WORDS.items() if _has(low, words)]
    return got or None


def mock_intake(text: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Rule-based ConstraintEdit for one message."""
    low = f" {text.lower()} "
    lang = _language(text)
    today = date.fromisoformat(str(ctx.get("today", date.today().isoformat())))
    base: dict[str, Any] = {
        "edits": [],
        "question": None,
        "needs_clarification": False,
        "clarification": None,
        "language": lang,
    }

    if _has(low, OUT_OF_SCOPE):
        return {**base, "intent": "out_of_scope", "confidence": 0.9}

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
    edit = {
        "op": op,
        "nurse": "self",
        "dates": [d.isoformat() for d in dates],
        "shifts": shifts,
        "counterpart": None,
        "counterpart_dates": [],
        "counterpart_shifts": None,
        "request_id": None,
        "target_total": None,
        "reason_category": "sick"
        if sick
        else ("personal" if op != "mark_unavailable" else "other"),
        "evidence_span": text.strip(),
    }
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
            body: dict[str, Any] = mock_intake(last_user_text(req), ctx)
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
        elif req.tools:
            body = {
                "answer": "Explanations are wired to the evidence tools in M8 (mock).",
                "claims": [],
                "language": "en",
            }
        else:
            body = {"text": "mock"}
        return ProviderResult(text=json.dumps(body, ensure_ascii=False))
