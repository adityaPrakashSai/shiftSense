"""Render intake replies from `templates/{en,hi}.toml` (design doc 7.3 step 6).

`lang` is the reply language: "en", or "hi" for Hindi and Hinglish senders (Roman script, which is
how the ward writes on WhatsApp). Every template key exists in both files (tested).
"""

import tomllib
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from app.domain.models import ShiftCode
from app.intake.dates import fmt_day

DIR = Path(__file__).with_name("templates")
LANGS = ("en", "hi")


@cache
def table(lang: str) -> dict[str, dict[str, str]]:
    data: dict[str, Any] = tomllib.loads((DIR / f"{lang}.toml").read_text(encoding="utf-8"))
    return data


def reply_lang(language: str | None) -> str:
    return "en" if language in (None, "en") else "hi"


def text(lang: str, section: str, key: str, **params: Any) -> str:
    return table(lang)[section][key].format(**params)


def join(lang: str, items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {table(lang)['words']['and']} " + items[-1]


def days(lang: str, ds: list[date]) -> str:
    return join(lang, [fmt_day(d, lang) for d in sorted(ds)])


def shifts(lang: str, ss: list[ShiftCode] | None) -> str:
    if not ss:
        return table(lang)["words"]["whole_day"]
    return join(lang, [table(lang)["shift"][s.value] for s in sorted(ss)])


def duty(lang: str, ds: list[date], ss: list[ShiftCode] | None) -> str:
    key = "whole_day" if not ss else "shifts"
    return text(lang, "duty", key, days=days(lang, ds), shifts=shifts(lang, ss))


def cell(lang: str, d: date | None, ss: list[ShiftCode] | None) -> str:
    """One swap cell: "Sat 7 Nov Night"."""
    when = fmt_day(d, lang) if d else "?"
    return f"{when} {shifts(lang, ss)}"
