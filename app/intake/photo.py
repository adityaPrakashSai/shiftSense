"""Roster photo reading (design doc 7.8): a photo of the paper roster -> an editable preview grid.

The photo goes to `Role.VISION` whole, with room for 32k output tokens: Muse Spark Open reasons
for 10-13k tokens over a 22 x 31 month before it answers, and with that budget it copied both
month sheets of the eval without an error. The design's two-tile read (7.8) is the fallback,
used when the whole-sheet answer is cut off, unreadable or misses date columns: the left 58%,
then the right 55% with the left tile's row names listed in the prompt (the right part has no
name column; pasting the names beside it failed on tilted sheets, whose rows no longer line up).
Each read reports the day numbers it shows, so columns map to dates by day number; the right
tile's rows join by position or, if the counts differ, by an order-keeping alignment on the days
both tiles show. A cell read differently in the two tiles becomes "?". Code maps names to nurse
ids (two rows with the same name keep the order of the ward list and are flagged for the charge
nurse to check) and checks the cells against the printed daily totals (`planner/totals.py`).
Nothing is stored here: the charge nurse corrects the grid and imports it
(`app/planner/importer.py`).
"""

import base64
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from PIL import Image, ImageOps

from app.domain.models import LeaveType, Ward
from app.intake.resolve_names import resolve
from app.llm.cache import PROMPTS
from app.llm.gateway import LLMGateway
from app.llm.jsonproto import loads_json
from app.llm.types import LLMRequest, Role
from app.planner.totals import COUNTED, Mismatch, checksum

MAX_SIDE = 2400  # px; phone photos are downscaled before upload to the model
LEFT_END = 0.58  # tile 1: the left 58% of the sheet
RIGHT_START = 0.45  # tile 2: everything right of 45% (no names)
OFF = ("D/O", "N/O")
CODE = re.compile(r"^(M|E|N|G|N/O|D/O|\?|L:[A-Z]+)$")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "days": {"type": "array", "items": {"type": "integer"}},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "cells": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "cells"],
            },
        },
        "totals": {
            "type": "object",
            "properties": {
                s: {"type": "array", "items": {"type": ["integer", "null"]}} for s in COUNTED
            },
        },
    },
    "required": ["days", "rows"],
}


class PhotoError(Exception):
    """The photo could not be read into a grid (bad image, or no usable model answer)."""


@dataclass
class TileRead:
    days: list[int]
    rows: list[dict[str, Any]]
    totals: dict[str, list[int | None]]
    call_id: str = ""
    cache_hit: bool = False


@dataclass
class PhotoRow:
    name: str
    nurse_id: str | None
    candidates: list[str]  # ids to pick from when the name is unknown or shared
    cells: list[str]  # one code per horizon day
    flagged: bool = False  # duplicate or unmatched name: the charge nurse checks the id


@dataclass
class PhotoRead:
    days: list[date]
    rows: list[PhotoRow]
    totals: dict[str, list[int | None]]  # printed totals per horizon day (None = not read)
    mismatches: list[Mismatch]
    unreadable: int
    tiles: int
    sheet_days: list[int] = field(default_factory=list)  # day numbers on the sheet, as read
    call_ids: list[str] = field(default_factory=list)
    cache_hits: int = 0


# ---------------------------------------------------------------- image


def normalise(raw: bytes) -> Image.Image:
    try:
        img: Image.Image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        img = img.convert("RGB")
    except Exception as exc:
        raise PhotoError("That file is not an image we can read (JPEG, PNG or WebP).") from exc
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def tiles(img: Image.Image) -> list[bytes]:
    """The fallback's two overlapping tiles: the left 58% (with the names) and the right 55%."""
    w, h = img.size
    return [
        jpeg(img.crop((0, 0, round(w * LEFT_END), h))),
        jpeg(img.crop((round(w * RIGHT_START), 0, w, h))),
    ]


# ---------------------------------------------------------------- model call


def build_request(image: bytes, tile: int, of: int, names: list[str] | None = None) -> LLMRequest:
    """`names`: the rows read from the left tile, which the right tile (no name column) follows.
    The instructions for each part are in `prompts/vision.md`; the user turn only labels it."""
    if of == 1:
        where = "Part: whole sheet."
    elif tile == 0:
        where = "Part: left."
    else:
        listed = "\n".join(f"{i}. {n}" for i, n in enumerate(names or [], 1))
        where = f"Part: right.\nNurse rows, top to bottom:\n{listed}"
    url = "data:image/jpeg;base64," + base64.b64encode(image).decode()
    return LLMRequest(
        role=Role.VISION,
        messages=[
            {"role": "system", "content": (PROMPTS / "vision.md").read_text(encoding="utf-8")},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": where},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
        ],
        response_schema=SCHEMA,
        max_tokens=32000,
        timeout_s=150.0,
    )


def code(raw: Any) -> str:
    """A model cell -> a preview code; anything outside the vocabulary becomes "?"."""
    s = str(raw or "").strip().upper().replace(" ", "")
    if s in ("", "-", "OFF", "DO", "D/O", "O"):
        return "D/O"
    if s in ("NO", "N/O", "N.O", "N/0"):
        return "N/O"
    if s in [lt.value for lt in LeaveType] or s == "L":
        s = f"L:{s}"
    return s if CODE.match(s) else "?"


def parse_tile(obj: Any) -> TileRead:
    if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
        raise PhotoError("The model did not return a roster grid.")
    days = [int(d) for d in obj.get("days") or [] if isinstance(d, int | float)]
    rows = []
    for r in obj["rows"]:
        if not isinstance(r, dict):
            continue
        cells = [code(c) for c in (r.get("cells") or [])][: len(days)]
        rows.append({"name": str(r.get("name") or "").strip(), "cells": cells})
    got = obj.get("totals")
    raw_tot: dict[str, Any] = got if isinstance(got, dict) else {}
    totals: dict[str, list[int | None]] = {}
    for s in COUNTED:
        vals = raw_tot.get(s) or []
        totals[s] = [int(v) if isinstance(v, int | float) else None for v in vals][: len(days)]
    return TileRead(days, rows, totals)


async def read_tile(
    gateway: LLMGateway, image: bytes, tile: int, of: int, names: list[str] | None = None
) -> TileRead:
    resp = await gateway.complete(build_request(image, tile, of, names))
    try:
        obj = resp.parsed if resp.parsed is not None else loads_json(resp.text or "")
    except (ValueError, json.JSONDecodeError) as exc:
        raise PhotoError(
            "The model's answer was not a readable grid; try a clearer photo."
        ) from exc
    out = parse_tile(obj)
    out.call_id, out.cache_hit = resp.call_id, resp.cache_hit
    return out


# ---------------------------------------------------------------- merge


def column_index(days: list[int], horizon: list[date]) -> list[int | None]:
    """Each tile column's horizon index, matching day numbers left to right."""
    out: list[int | None] = []
    pos = 0
    for d in days:
        hit = next((i for i in range(pos, len(horizon)) if horizon[i].day == d), None)
        if hit is None:
            hit = next((i for i in range(len(horizon)) if horizon[i].day == d), None)
        out.append(hit)
        if hit is not None:
            pos = hit + 1
    return out


def _row_map(
    base: list[dict[str, Any]], base_cols: list[int | None],
    other: list[dict[str, Any]], other_cols: list[int | None],
) -> list[int | None]:  # fmt: skip
    """For each row of the right tile, its row in the left tile: by position when the counts
    agree, else an order-keeping alignment (like a diff) that maximises agreement on the days both
    tiles show, so a row one tile missed or added leaves the rest aligned. Unmatched -> None."""
    if len(base) == len(other):
        return list(range(len(other)))

    def cells(rows: list[dict[str, Any]], cols: list[int | None], r: int) -> dict[int, str]:
        return {d: c for d, c in zip(cols, rows[r]["cells"], strict=False) if d is not None}

    shared = {d for d in base_cols if d is not None} & {d for d in other_cols if d is not None}
    a = [cells(base, base_cols, i) for i in range(len(base))]
    b = [cells(other, other_cols, j) for j in range(len(other))]

    def score(i: int, j: int) -> float:  # > 0 when more than half the shared days agree
        same = sum(a[i].get(d) == b[j].get(d) for d in shared)
        return same - len(shared) / 2 + 0.25

    n, m = len(a), len(b)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best[i][j] = max(best[i + 1][j], best[i][j + 1], score(i, j) + best[i + 1][j + 1])
    out: list[int | None] = [None] * m
    i = j = 0
    while i < n and j < m:
        if best[i][j] == score(i, j) + best[i + 1][j + 1] and score(i, j) > 0:
            out[j] = i
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return out


Totals = dict[str, list[int | None]]


def merge(reads: list[TileRead], horizon: list[date]) -> tuple[list[dict[str, Any]], Totals]:
    n = len(horizon)
    base = reads[0].rows
    seen: list[list[list[str]]] = [[[] for _ in range(n)] for _ in base]
    printed: dict[str, list[list[int]]] = {s: [[] for _ in range(n)] for s in COUNTED}
    base_cols = column_index(reads[0].days, horizon)
    for t in reads:
        cols = column_index(t.days, horizon)
        rmap = (
            list(range(len(t.rows))) if t is reads[0] else _row_map(base, base_cols, t.rows, cols)
        )
        for ri, bi in enumerate(rmap):
            if bi is None:
                continue
            for c, d in zip(t.rows[ri]["cells"], cols, strict=False):
                if d is not None:
                    seen[bi][d].append(c)
        for s in COUNTED:
            for v, d in zip(t.totals.get(s, []), cols, strict=False):
                if d is not None and v is not None:
                    printed[s][d].append(v)
    rows = []
    for b, cells in zip(base, seen, strict=True):
        agreed = [c[0] if c and len(set(c)) == 1 else "?" for c in cells]
        rows.append({"name": b["name"], "cells": agreed})
    totals = {s: [v[0] if v and len(set(v)) == 1 else None for v in printed[s]] for s in COUNTED}
    return rows, totals


def map_names(rows: list[dict[str, Any]], ward: Ward) -> list[PhotoRow]:
    """Names -> nurse ids. Rows sharing a name take that name's ids in ward order, flagged."""
    out: list[PhotoRow] = []
    taken: dict[str, int] = {}
    for r in rows:
        m = resolve(r["name"], ward.nurses)
        if m.nurse_id is not None:
            out.append(PhotoRow(r["name"], m.nurse_id, [m.nurse_id], r["cells"]))
            continue
        if m.candidates:
            k = taken.get(r["name"].casefold(), 0)
            taken[r["name"].casefold()] = k + 1
            pick = m.candidates[k] if k < len(m.candidates) else None
            out.append(PhotoRow(r["name"], pick, list(m.candidates), r["cells"], flagged=True))
            continue
        out.append(PhotoRow(r["name"], None, [n.id for n in ward.nurses], r["cells"], True))
    return out


def check_totals(rows: list[PhotoRow], totals: dict[str, list[int | None]]) -> list[Mismatch]:
    """Days where the read cells do not add up to the printed totals (unread totals skipped)."""
    roster = {str(i): [c if c in COUNTED else None for c in r.cells] for i, r in enumerate(rows)}
    printed = {s: [-1 if v is None else v for v in totals[s]] for s in COUNTED}
    return [m for m in checksum(roster, printed) if m["printed"] >= 0]


async def read_tiles(gateway: LLMGateway, img: Image.Image) -> list[TileRead]:
    """Fallback: the left tile, then the right tile with the left tile's row names listed."""
    parts = tiles(img)
    left = await read_tile(gateway, parts[0], 0, 2)
    if not left.rows:
        raise PhotoError("No roster rows were found in the photo.")
    right = await read_tile(gateway, parts[1], 1, 2, [r["name"] for r in left.rows])
    return [left, right]


async def read_photo(raw: bytes, ward: Ward, gateway: LLMGateway) -> PhotoRead:
    horizon = [ward.horizon_start + timedelta(days=i) for i in range(ward.horizon_days)]
    img = normalise(raw)
    reads: list[TileRead] = []
    try:
        whole = await read_tile(gateway, jpeg(img), 0, 1)
        if whole.rows and set(column_index(whole.days, horizon)) >= set(range(len(horizon))):
            reads = [whole]
    except PhotoError:
        pass
    tiled = not reads
    if tiled:  # cut off, unreadable or missing date columns: read the sheet in two parts
        reads = await read_tiles(gateway, img)
    merged, totals = merge(reads, horizon)
    rows = map_names(merged, ward)
    return PhotoRead(
        days=horizon,
        rows=rows,
        totals=totals,
        mismatches=check_totals(rows, totals),
        unreadable=sum(c == "?" for r in rows for c in r.cells),
        tiles=2 if tiled else 1,
        sheet_days=sorted({d for t in reads for d in t.days}),
        call_ids=[t.call_id for t in reads],
        cache_hits=sum(t.cache_hit for t in reads),
    )
