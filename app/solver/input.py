"""Solver-facing input (design doc 4.3). Standard library only.

`SolveInput` is frozen and built once per run by `app/planner/build_input.py`. It serialises to
plain JSON (`to_json` / `from_json`) so every run stored in `run.input_json` can be replayed.
Days are indices from the horizon start (0 = first day).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

SHIFTS: tuple[str, ...] = ("M", "E", "N")  # ward coverage shifts; "G" is general duty
TIERS: tuple[str, ...] = ("coverage", "stability", "contract", "requests", "fairness")

Cell = tuple[str, int]  # (nurse, day)
CellShift = tuple[str, int, str | None]  # (nurse, day, shift | None = whole day)


@dataclass(frozen=True)
class NurseSpec:
    id: str
    grade: str  # senior | staff | trainee
    role: str  # staff | senior | in_charge | ans
    standby: bool
    can_night: bool
    min_shifts: int  # scaled to the horizon, rounded down
    max_shifts: int  # scaled to the horizon, rounded up
    max_hours: float | None = None  # scaled to the horizon; hard when set


@dataclass(frozen=True)
class DemandSpec:
    min_total: int  # hard floor
    target_total: int  # soft target (coverage tier)
    max_total: int  # hard ceiling (defaults to target_total)
    min_senior: int = 0


@dataclass(frozen=True)
class Pref:
    request_id: str
    nurse: str
    day: int
    shift: str | None  # None for a day-off preference
    weight: int


@dataclass(frozen=True)
class Forcing:
    kind: str  # fixed_duty | swap | what_if
    nurse: str
    day: int
    shift: str | None  # None forces the day off


@dataclass(frozen=True)
class NurseHistory:
    last_shifts: tuple[str | None, str | None, str | None] = (None, None, None)  # days -3..-1
    consecutive_days: int = 0
    consecutive_nights: int = 0
    nights: int = 0
    mornings: int = 0
    evenings: int = 0
    weekends: int = 0
    denied: int = 0


@dataclass(frozen=True)
class SolveInput:
    nurses: tuple[NurseSpec, ...]
    num_days: int
    weekend_days: frozenset[int]
    shift_hours: Mapping[str, float]
    demand: Mapping[tuple[int, str], DemandSpec]
    # HARD availability, one field per hard group (5.2) so constraint names stay exact
    unavailable: frozenset[CellShift]  # sick / emergency (group `unavailable`)
    leave: frozenset[Cell]  # approved leave (group `leave`)
    night_exempt: frozenset[Cell]  # Nurse.night_exempt_until (group `night_exempt`)
    day_off_prefs: tuple[Pref, ...]
    shift_prefs: tuple[Pref, ...]  # wanted
    shift_avoids: tuple[Pref, ...]
    forced: tuple[Forcing, ...]  # fixed duties, approved swaps, what-if forcings
    waived: frozenset[str]  # hard-constraint names waived by a person
    max_consecutive_days: int
    night_block_min: int  # soft
    night_block_max: int  # hard (max_consecutive_nights)
    max_nights: int  # per nurse for this horizon
    standby_allowed: bool = False  # False in a full run (standby_off_roster)
    previous: Mapping[Cell, str | None] | None = None  # published roster for re-solve
    frozen_before_day: int = 0
    frozen_cells: frozenset[Cell] = frozenset()
    history: Mapping[str, NurseHistory] = field(default_factory=dict)
    priorities: tuple[str, ...] = TIERS

    # ---------------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        def sorted_list(items: frozenset[Any]) -> list[Any]:
            return sorted((list(i) if isinstance(i, tuple) else i for i in items), key=repr)

        return {
            "nurses": [asdict(n) for n in self.nurses],
            "num_days": self.num_days,
            "weekend_days": sorted(self.weekend_days),
            "shift_hours": dict(self.shift_hours),
            "demand": [
                {"day": d, "shift": s, **asdict(spec)}
                for (d, s), spec in sorted(self.demand.items())
            ],
            "unavailable": sorted_list(self.unavailable),
            "leave": sorted_list(self.leave),
            "night_exempt": sorted_list(self.night_exempt),
            "day_off_prefs": [asdict(p) for p in self.day_off_prefs],
            "shift_prefs": [asdict(p) for p in self.shift_prefs],
            "shift_avoids": [asdict(p) for p in self.shift_avoids],
            "forced": [asdict(f) for f in self.forced],
            "waived": sorted(self.waived),
            "max_consecutive_days": self.max_consecutive_days,
            "night_block_min": self.night_block_min,
            "night_block_max": self.night_block_max,
            "max_nights": self.max_nights,
            "standby_allowed": self.standby_allowed,
            "previous": (
                None
                if self.previous is None
                else [[n, d, s] for (n, d), s in sorted(self.previous.items())]
            ),
            "frozen_before_day": self.frozen_before_day,
            "frozen_cells": sorted_list(self.frozen_cells),
            "history": {
                n: {**asdict(h), "last_shifts": list(h.last_shifts)}
                for n, h in sorted(self.history.items())
            },
            "priorities": list(self.priorities),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def input_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SolveInput:
        def last3(v: list[str | None]) -> tuple[str | None, str | None, str | None]:
            a, b, c = v
            return (a, b, c)

        return cls(
            nurses=tuple(NurseSpec(**n) for n in d["nurses"]),
            num_days=d["num_days"],
            weekend_days=frozenset(d["weekend_days"]),
            shift_hours=dict(d["shift_hours"]),
            demand={
                (x["day"], x["shift"]): DemandSpec(
                    x["min_total"], x["target_total"], x["max_total"], x["min_senior"]
                )
                for x in d["demand"]
            },
            unavailable=frozenset((n, day, s) for n, day, s in d["unavailable"]),
            leave=frozenset((n, day) for n, day in d["leave"]),
            night_exempt=frozenset((n, day) for n, day in d["night_exempt"]),
            day_off_prefs=tuple(Pref(**p) for p in d["day_off_prefs"]),
            shift_prefs=tuple(Pref(**p) for p in d["shift_prefs"]),
            shift_avoids=tuple(Pref(**p) for p in d["shift_avoids"]),
            forced=tuple(Forcing(**f) for f in d["forced"]),
            waived=frozenset(d["waived"]),
            max_consecutive_days=d["max_consecutive_days"],
            night_block_min=d["night_block_min"],
            night_block_max=d["night_block_max"],
            max_nights=d["max_nights"],
            standby_allowed=d["standby_allowed"],
            previous=(
                None if d["previous"] is None else {(n, day): s for n, day, s in d["previous"]}
            ),
            frozen_before_day=d["frozen_before_day"],
            frozen_cells=frozenset((n, day) for n, day in d["frozen_cells"]),
            history={
                n: NurseHistory(**{**h, "last_shifts": last3(h["last_shifts"])})
                for n, h in d["history"].items()
            },
            priorities=tuple(d["priorities"]),
        )

    @classmethod
    def from_json(cls, s: str) -> SolveInput:
        return cls.from_dict(json.loads(s))
