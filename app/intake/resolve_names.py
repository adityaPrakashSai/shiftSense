"""Nurse name resolution (design doc 7.3 step 5), shared with the explainer's `find_nurse` (7.4).

Order: "self" words -> exact id -> exact name or alias (case-insensitive, honorifics such as "di"
or "sister" dropped) -> fuzzy match with ratio >= 0.85. A name that fits more than one nurse is
ambiguous and becomes a clarification; nothing is guessed.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.domain.models import Nurse

FUZZY_MIN = 0.85
SELF_WORDS = {"self", "me", "i", "myself", "main", "mujhe", "mai", "मैं", "मुझे"}
HONORIFICS = {"di", "didi", "ji", "bhaiya", "bhai", "sister", "sr", "madam", "maam", "दीदी", "जी"}


@dataclass(frozen=True)
class NameMatch:
    nurse_id: str | None  # set when exactly one nurse matches
    candidates: list[str] = field(default_factory=list)  # ids when ambiguous
    how: str = "none"  # self | id | name | fuzzy | ambiguous | none


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s).casefold()
    s = re.sub(r"[.,'’]", "", s)
    words = [w for w in s.split() if w not in HONORIFICS]
    return " ".join(words)


def _names(n: Nurse) -> set[str]:
    return {x for x in (_norm(v) for v in [n.name, *n.aliases]) if x}


def resolve(name: str | None, nurses: list[Nurse], sender_id: str | None = None) -> NameMatch:
    raw = (name or "").strip()
    if not raw or raw.casefold() in SELF_WORDS:
        return NameMatch(sender_id, how="self") if sender_id else NameMatch(None)
    by_id = {n.id.casefold(): n.id for n in nurses}
    if raw.casefold() in by_id:
        return NameMatch(by_id[raw.casefold()], how="id")
    key = _norm(raw)
    if not key:
        return NameMatch(None)
    exact = sorted(n.id for n in nurses if key in _names(n))
    if len(exact) == 1:
        return NameMatch(exact[0], how="name")
    if exact:
        return NameMatch(None, exact, how="ambiguous")
    scores: dict[str, float] = {}
    for n in nurses:
        scores[n.id] = max(SequenceMatcher(None, key, x).ratio() for x in _names(n))
    good = sorted(i for i, r in scores.items() if r >= FUZZY_MIN)
    if len(good) == 1:
        return NameMatch(good[0], how="fuzzy")
    if good:
        return NameMatch(None, good, how="ambiguous")
    return NameMatch(None)
