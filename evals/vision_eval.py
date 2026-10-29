"""Photo-import eval (design doc 7.8, 9.3): cell accuracy of roster photos read by Muse Spark.

The cases are the synthetic sheets in `evals/vision/labels.json` (`scripts/vision_sheets.py`).
Each sheet is scored from its rendered image `<id>.jpg` and, when Aditya has photographed a
handwritten copy, from `photo-<id>.jpg` too. Scoring is code only: a cell is right when the read
code equals the label (blank and D/O are the same code); rows are compared by nurse id after the
app's own name mapping, so a wrong name mapping costs the whole row.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from app.domain.models import Ward
from app.intake.photo import PhotoError, read_photo
from app.llm.gateway import LLMGateway
from app.llm.types import LLMError
from app.store import repo
from app.store.db import open_db
from app.store.seed import seed_db

VISION = Path(__file__).resolve().parent / "vision"
PASS_ACCURACY = 0.95


def load_vision(limit: int | None, ids: list[str] | None) -> list[dict[str, Any]]:
    labels = json.loads((VISION / "labels.json").read_text(encoding="utf-8"))
    cases = []
    for lab in labels:
        for stem, source in ((lab["id"], "rendered"), (f"photo-{lab['id']}", "handwritten")):
            if (VISION / f"{stem}.jpg").exists():
                cases.append({"id": stem, "source": source, "tags": [source], "label": lab})
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    return cases[:limit] if limit else cases


def score(label: dict[str, Any], read: Any) -> dict[str, Any]:
    want = {r["nurse_id"]: r["cells"] for r in label["rows"]}
    got = {r.nurse_id: r.cells for r in read.rows if r.nurse_id}
    right = total = unreadable = 0
    wrong: list[str] = []
    for nid, cells in want.items():
        mine = got.get(nid, ["?"] * len(cells))
        for i, (a, b) in enumerate(zip(cells, mine, strict=False)):
            total += 1
            unreadable += b == "?"
            if a == b:
                right += 1
            elif len(wrong) < 12:
                wrong.append(f"{nid} d{i + 1}: {a} read {b}")
    t_right = t_total = 0
    for s, vals in label["totals"].items():
        for a, b in zip(vals, read.totals.get(s, []), strict=False):
            t_total += 1
            t_right += a == b
    return {
        "cells": total,
        "cells_right": right,
        "accuracy": round(right / total, 4) if total else 0.0,
        "unreadable": unreadable,
        "rows_found": len(read.rows),
        "rows_expected": len(label["rows"]),
        "totals_accuracy": round(t_right / t_total, 4) if t_total else 0.0,
        "checksum_flags": len(read.mismatches),
        "flagged_names": sum(r.flagged for r in read.rows),
        "wrong": wrong,
    }


async def run_vision(
    cases: list[dict[str, Any]], *, gateway: LLMGateway, db: Path, workers: int
) -> list[dict[str, Any]]:
    wards: dict[str, Ward] = {}
    for seed in sorted({c["label"]["seed"] for c in cases}):
        conn = open_db(db.with_name(f"{seed}.db"))
        seed_db(conn, seed, solve=False)
        ward = repo.get_ward(conn)
        conn.close()
        assert ward is not None
        wards[seed] = ward
    sem = asyncio.Semaphore(workers)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            raw = (VISION / f"{case['id']}.jpg").read_bytes()
            try:
                read = await read_photo(raw, wards[case["label"]["seed"]], gateway)
            except (PhotoError, LLMError) as exc:
                row = {"id": case["id"], "tags": case["tags"], "pass": False,
                       "reasons": [f"{type(exc).__name__}: {exc}"], "accuracy": 0.0}  # fmt: skip
                print(f"FAIL {case['id']} {row['reasons'][0][:100]}")
                return row
            sc = score(case["label"], read)
            ok = sc["accuracy"] >= PASS_ACCURACY and sc["rows_found"] == sc["rows_expected"]
            row = {"id": case["id"], "tags": case["tags"], "tiles": read.tiles,
                   "cache_hits": read.cache_hits, **sc, "pass": ok,
                   "reasons": [f"accuracy {sc['accuracy']:.1%}", *sc["wrong"]]}  # fmt: skip
            print(f"{'PASS' if ok else 'FAIL'} {case['id']:16} {sc['cells_right']}/{sc['cells']}"
                  f" cells, totals {sc['totals_accuracy']:.0%}, {read.tiles} tile(s)")  # fmt: skip
            return row

    return list(await asyncio.gather(*(one(c) for c in cases)))


def summarize_vision(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cells = sum(r.get("cells", 0) for r in rows)
    right = sum(r.get("cells_right", 0) for r in rows)
    by_tag: dict[str, str] = {}
    for tag in sorted({t for r in rows for t in r["tags"]}):
        rs = [r for r in rows if tag in r["tags"]]
        c = sum(r.get("cells", 0) for r in rs)
        by_tag[tag] = f"{sum(r.get('cells_right', 0) for r in rs)}/{c} cells"
    return {
        "photos": len(rows),
        "cell_accuracy": f"{100 * right / cells:.1f}%" if cells else "n/a",
        "cells": f"{right}/{cells}",
        "unreadable_cells": sum(r.get("unreadable", 0) for r in rows),
        "totals_accuracy": " ".join(f"{r['id']}:{r.get('totals_accuracy', 0):.0%}" for r in rows),
        "photos_passed": f"{sum(r['pass'] for r in rows)}/{len(rows)} (>= {PASS_ACCURACY:.0%})",
        "errors": sum(1 for r in rows if not r["pass"]),
        "by_tag": by_tag,
    }
