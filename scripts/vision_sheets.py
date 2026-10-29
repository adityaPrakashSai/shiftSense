"""Synthetic paper rosters for the photo-import eval (design doc 7.8, 9.3).

    uv run python -m scripts.vision_sheets            # writes evals/vision/labels.json + sheets/
    node scripts/render_sheets.mjs                    # renders sheets/*.html to evals/vision/*.jpg

Five sheets with the seed wards' invented names: three 14-day mini-ward grids (the committed
baseline, the same with a leave, and a shifted variant) and the real_a month plans (31 days, 22
nurses, two rows named "Priya Nair"). `labels.json` holds what each sheet says: one display code
per cell (M, E, N, G, N/O, D/O, L:<type>) and the printed daily totals. The HTML pages double as
the reference Aditya copies by hand onto paper; a photo of a copy is saved as
`evals/vision/photo-<sheet>.jpg` and scored against the same labels. No real roster is used.
"""

import html
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.planner.roster_view import roster_cells
from app.store import repo
from app.store.db import open_db
from app.store.seed import seed_db

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evals" / "vision"
COUNTED = ("M", "E", "N")

# per-sheet look: system handwriting fonts on macOS, then generic fallbacks
STYLES = [
    {"font": "'Bradley Hand', 'Segoe Print', cursive", "ink": "#1d3b8f", "tilt": -0.8},
    {"font": "'Noteworthy', 'Comic Sans MS', cursive", "ink": "#222", "tilt": 0.6},
    {"font": "'Marker Felt', 'Segoe Print', cursive", "ink": "#123d7a", "tilt": -1.2},
    {"font": "'Chalkboard SE', 'Comic Sans MS', cursive", "ink": "#1a1a1a", "tilt": 0.4},
    {"font": "'Bradley Hand', 'Segoe Print', cursive", "ink": "#0f2f6b", "tilt": 1.0},
]


def grid(seed: str, run_key: str | None = None) -> dict[str, Any]:
    """Display codes and totals of a seed's published run (or one of its stored runs)."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "sheet.db")
        seed_db(conn, seed)
        ward = repo.get_ward(conn)
        assert ward is not None
        if run_key is None:
            run = repo.get_published_run(conn, ward.id)
        else:
            run = repo.get_run(conn, f"run-{ward.id}-{run_key}")
        assert run is not None
        cells, _ = roster_cells(conn, ward, run)
        conn.close()
    days = [ward.horizon_start + timedelta(days=i) for i in range(ward.horizon_days)]
    by = {(c["nurse_id"], c["day"]): c for c in cells}
    rows = []
    for n in ward.nurses:
        codes = []
        for d in days:
            c = by[(n.id, d.isoformat())]
            lt = c["leave_type"]
            codes.append(f"L:{lt}" if lt else c["display"])
        rows.append({"nurse_id": n.id, "name": n.name, "cells": codes})
    return {"ward_id": ward.id, "seed": seed, "start": days[0].isoformat(), "rows": rows}


def totals(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    days = len(rows[0]["cells"])
    out = {s: [0] * days for s in COUNTED}
    for r in rows:
        for i, c in enumerate(r["cells"]):
            if c in out:
                out[c][i] += 1
    return out


def with_leave(g: dict[str, Any], nurse_id: str, first: int, last: int, kind: str) -> None:
    row = next(r for r in g["rows"] if r["nurse_id"] == nurse_id)
    for i in range(first, last + 1):
        row["cells"][i] = f"L:{kind}"


def shifted(g: dict[str, Any], k: int) -> None:
    """Rotate the rows by k places (a different, still plausible-looking sheet)."""
    cells = [r["cells"] for r in g["rows"]]
    for r, c in zip(g["rows"], cells[k:] + cells[:k], strict=True):
        r["cells"] = list(c)


def sheets() -> list[dict[str, Any]]:
    a = grid("mini")
    b = grid("mini")
    with_leave(b, "N06", 9, 11, "CL")
    c = grid("mini")
    shifted(c, 4)
    d = grid("real_a", "planned")
    e = grid("real_a", "updated")
    out = []
    for i, (sid, g, title) in enumerate(
        [
            ("mini-a", a, "Ward 3B duty roster"),
            ("mini-b", b, "Ward 3B duty roster"),
            ("mini-c", c, "Ward 3B duty roster (draft)"),
            ("month-a", d, "Ward A duty roster, October"),
            ("month-b", e, "Ward A duty roster, October (updated)"),
        ]
    ):
        g["totals"] = totals(g["rows"])
        out.append({"id": sid, "title": title, "style": STYLES[i], **g})
    return out


def cell_text(code: str) -> str:
    return code[2:] if code.startswith("L:") else ("" if code == "D/O" else code)


def page(s: dict[str, Any]) -> str:
    start = date.fromisoformat(s["start"])
    days = [start + timedelta(days=i) for i in range(len(s["rows"][0]["cells"]))]
    st = s["style"]
    head = "".join(
        f"<th class='{'we' if d.weekday() >= 5 else ''}'>{d.day}<br><small>{'MTWTFSS'[d.weekday()]}"
        "</small></th>"
        for d in days
    )
    body = []
    for r in s["rows"]:
        tds = "".join(f"<td>{html.escape(cell_text(c))}</td>" for c in r["cells"])
        body.append(f"<tr><th class='name'>{html.escape(r['name'])}</th>{tds}</tr>")
    for sh in COUNTED:
        tds = "".join(f"<td>{v}</td>" for v in s["totals"][sh])
        body.append(f"<tr class='tot'><th class='name'>Total {sh}</th>{tds}</tr>")
    month = start.strftime("%b %Y")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{s["id"]}</title>
<style>
body {{ margin: 0; background: #e9e4d8; }}
.paper {{ display: inline-block; margin: 28px; padding: 22px 26px; background: #fbf9f2;
  box-shadow: 0 2px 8px rgba(0,0,0,.25); transform: rotate({st["tilt"]}deg);
  font-family: {st["font"]}; color: {st["ink"]}; }}
h1 {{ font-size: 22px; margin: 0 0 4px; font-weight: normal; }}
p {{ margin: 0 0 10px; font-size: 14px; }}
table {{ border-collapse: collapse; font-size: 17px; }}
td, th {{ border: 1px solid #8a8a8a; width: 34px; height: 30px; text-align: center;
  font-weight: normal; padding: 0 2px; }}
th small {{ font-size: 11px; }}
th.we {{ background: #ece6d6; }}
th.name {{ width: auto; min-width: 130px; text-align: left; padding-left: 6px; }}
tr.tot td, tr.tot th {{ border-top: 2px solid #555; }}
</style></head><body><div class="paper">
<h1>{html.escape(s["title"])}</h1><p>{month} &middot; blank = day off &middot; N/O = night off</p>
<table><tr><th class="name">Name</th>{head}</tr>{"".join(body)}</table></div></body></html>
"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sheets").mkdir(exist_ok=True)
    labels = []
    for s in sheets():
        (OUT / "sheets" / f"{s['id']}.html").write_text(page(s), encoding="utf-8")
        labels.append({k: s[k] for k in ("id", "seed", "ward_id", "start", "rows", "totals")})
    text = json.dumps(labels, ensure_ascii=False, indent=1)
    (OUT / "labels.json").write_text(text + "\n", encoding="utf-8")
    print(f"wrote {len(labels)} sheets to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
