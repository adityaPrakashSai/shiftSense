"""Ward time for intake (design doc 7.3, 3.5): "now" in the ward timezone (frozen by `DEMO_NOW`),
the 35-day calendar table the model resolves "aaj", "kal" and weekday names against, and shift
start times for the "shift already started" check."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.models import ShiftCode, Ward

WINDOW_DAYS = 35  # calendar rows in the prompt; intake accepts dates in this window only
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_HI = [
    "Somvaar", "Mangalvaar", "Budhvaar", "Guruvaar", "Shukravaar", "Shanivaar", "Ravivaar"
]  # fmt: skip
WEEKDAYS_DEV = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]


def ward_now(tz: str, demo_now: str | None = None) -> datetime:
    """The current time in the ward timezone; `DEMO_NOW` (ISO datetime) freezes it."""
    zone = ZoneInfo(tz)
    if demo_now:
        t = datetime.fromisoformat(demo_now)
        return (t if t.tzinfo else t.replace(tzinfo=zone)).astimezone(zone)
    return datetime.now(zone)


@dataclass(frozen=True)
class CalendarDay:
    day: date
    weekday: str
    weekday_hi: str
    weekday_dev: str

    def row(self) -> str:
        return f"{self.day.isoformat()} | {self.weekday} | {self.weekday_hi} ({self.weekday_dev})"


def calendar(today: date, days: int = WINDOW_DAYS) -> list[CalendarDay]:
    out = []
    for i in range(days):
        d = today + timedelta(days=i)
        w = d.weekday()
        out.append(CalendarDay(d, WEEKDAYS[w], WEEKDAYS_HI[w], WEEKDAYS_DEV[w]))
    return out


def window(today: date) -> tuple[date, date]:
    """First and last date intake accepts (today .. today + 34)."""
    return today, today + timedelta(days=WINDOW_DAYS - 1)


def shift_start(ward: Ward, shift: ShiftCode, day: date) -> datetime:
    st = next((s for s in ward.shifts if s.code == shift), None)
    hh, mm = (st.start if st else "00:00").split(":")
    return datetime.combine(day, time(int(hh), int(mm)), tzinfo=ZoneInfo(ward.tz))


def fmt_day(d: date, lang: str) -> str:
    """ "Tue 3 Nov" (en) or "3 Nov (Mangalvaar)" (hi)."""
    if lang == "en":
        return f"{WEEKDAYS[d.weekday()][:3]} {d.day} {d:%b}"
    return f"{d.day} {d:%b} ({WEEKDAYS_HI[d.weekday()]})"
