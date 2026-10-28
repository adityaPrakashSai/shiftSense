"""Seed files on disk -> domain models (design doc 4.5). No database access.

Seeds: `default` (data/seed/ward.json, 20 + 4 standby), `mini` (9-nurse mini ward) and `real_a`
(pseudonymized October ward whose "runs" block holds its printed plan and hand-edited update).
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.models import Request, Ward, WardHistory

SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "seed"
SEED_FILES = {
    "default": "ward.json",
    "mini": "mini_ward.json",
    "real_a": "real_ward_a.json",
}


@dataclass
class Seed:
    name: str
    ward: Ward
    requests: list[Request]
    history: WardHistory
    runs: dict[str, Any]  # raw "runs" block (real_a only)


def _off(code: str | None) -> str | None:
    return None if code in (None, "-") else code


def parse_seed(raw: dict[str, Any], name: str) -> Seed:
    w = dict(raw["ward"])
    if "max_consecutive_nights" in w:  # mini_ward/v1 spelling of night_block_max
        w.setdefault("night_block_max", w.pop("max_consecutive_nights"))
    ward = Ward.model_validate(w)

    default_created = datetime.fromisoformat(f"{ward.horizon_start.isoformat()}T00:00:00+00:00")
    requests = []
    for r in raw.get("requests", []):
        requests.append(Request.model_validate({"created_at": default_created, **r}))

    h = raw.get("history", {})
    history = WardHistory.model_validate(
        {
            "periods": h.get("periods", 0),
            "nurses": {
                nid: {**rec, "last_days": [_off(c) for c in rec.get("last_days", [])]}
                for nid, rec in h.get("nurses", {}).items()
            },
        }
    )
    return Seed(name, ward, requests, history, raw.get("runs", {}))


def load_seed_file(path: str | Path) -> Seed:
    p = Path(path)
    return parse_seed(json.loads(p.read_text(encoding="utf-8")), p.stem)


def load_seed(name: str = "default") -> Seed:
    if name not in SEED_FILES:
        raise ValueError(f"unknown seed {name!r}; choose one of {sorted(SEED_FILES)}")
    return parse_seed(json.loads((SEED_DIR / SEED_FILES[name]).read_text(encoding="utf-8")), name)
