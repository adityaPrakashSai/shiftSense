"""Voice notes and roster photo import (design doc 7.3, 7.8, M12): the Hindi voice note gives the
same edit as the typed demo message (replayed from the committed cache), the photo reader's merge,
name mapping and checksum, the import as the published plan, and the endpoints in mock mode."""

import asyncio
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import get_settings
from app.domain.models import RequestKind, RequestStatus, RunKind, Ward
from app.domain.seeds import load_seed
from app.intake import photo, voice
from app.intake.pipeline import handle_message
from app.llm import cache
from app.llm.gateway import LLMGateway
from app.main import app
from app.planner.importer import ImportRejected, import_grid
from app.store import repo
from app.store.db import open_db
from app.store.seed import seed_db

ROOT = Path(__file__).resolve().parents[2]
CLIP = ROOT / "tests" / "fixtures" / "voice" / "sick_call_hi.wav"
TYPED = "Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai"  # design doc 2.2 step 2
DEMO_NOW = datetime.fromisoformat("2026-11-03T06:05:00+05:30")
LABELS = json.loads((ROOT / "evals" / "vision" / "labels.json").read_text(encoding="utf-8"))


def stored(conn: Any, res: Any) -> list[tuple[Any, ...]]:
    """The requests a message stored: kind, nurse, dates, shifts."""
    out = []
    for rid in res.request_ids:
        r = repo.get_request(conn, rid)
        out.append((r.kind.value, r.nurse_id, tuple(r.dates), tuple(r.shifts or ())))
    return out


async def voice_vs_typed(mode: str, db: Path) -> tuple[str, Any, Any]:
    """The demo sick call typed and as a voice note, on a fresh mini ward (recorded live once:
    `voice_vs_typed("live", ...)`, then exported to data/llm_cache.jsonl)."""
    conn = open_db(db)
    seed_db(conn, "mini")
    cache.import_jsonl(conn, ROOT / "data" / "llm_cache.jsonl")
    ward = repo.get_ward(conn)
    assert ward is not None
    s = get_settings().model_copy(
        update={"llm_mode": mode, "db_path": str(db), "llm_provider_default": "baseten",
                "llm_provider_intake": None, "llm_provider_vision": None}
    )  # fmt: skip
    gw = LLMGateway(s)
    typed = await handle_message(conn, ward, "N03", TYPED, gateway=gw, now=DEMO_NOW)
    heard = await voice.transcribe(CLIP.read_bytes(), "wav", gw)
    spoken = await handle_message(
        conn, ward, "N03", heard, gateway=gw, now=DEMO_NOW, channel="voice"
    )
    if mode == "live":
        cache.export_jsonl(conn, ROOT / "data" / "llm_cache.jsonl")
    out = heard, (typed.reply_kind, stored(conn, typed)), (spoken.reply_kind, stored(conn, spoken))
    conn.close()
    return out


def test_hindi_voice_note_gives_the_typed_edit(tmp_path: Path) -> None:
    heard, typed, spoken = asyncio.run(voice_vs_typed("replay", tmp_path / "v.db"))
    assert "बुखार" in heard  # Devanagari transcript of the synthetic clip
    assert typed[0] == spoken[0] == "confirm"
    assert typed[1] == spoken[1]
    assert spoken[1][0][:2] == ("unavailable", "N03")


def test_voice_formats_and_limits() -> None:
    assert voice.audio_format("audio/wav", None) == "wav"
    assert voice.audio_format("application/octet-stream", "note.mp3") == "mp3"
    with pytest.raises(voice.VoiceError) as e:
        voice.audio_format("audio/webm", "note.webm")
    assert e.value.code == "AUDIO_FORMAT"
    gw = LLMGateway(get_settings().model_copy(update={"llm_mode": "mock"}))
    with pytest.raises(voice.VoiceError) as e:
        asyncio.run(voice.transcribe(b"x" * (voice.MAX_BYTES + 1), "wav", gw))
    assert e.value.code == "AUDIO_TOO_LONG"


# ---------------------------------------------------------------- photo reading (no model)


def mini_ward() -> Ward:
    return load_seed("mini").ward


def tile(days: list[int], rows: list[tuple[str, str]], totals: dict[str, list[int]]) -> Any:
    return photo.TileRead(
        days, [{"name": n, "cells": c.split()} for n, c in rows], dict(totals)
    )  # fmt: skip


def test_codes_normalise_to_the_preview_vocabulary() -> None:
    assert [photo.code(x) for x in ["m", "", "-", "NO", "n/o", "CL", "L:EL", "xx", "L"]] == [
        "M", "D/O", "D/O", "N/O", "N/O", "L:CL", "L:EL", "?", "L:L",
    ]  # fmt: skip


def test_merge_maps_days_and_marks_disagreements() -> None:
    ward = mini_ward()  # 26 Oct - 8 Nov
    horizon = [
        ward.horizon_start.fromordinal(ward.horizon_start.toordinal() + i) for i in range(14)
    ]
    left = tile(
        list(range(26, 32)) + [1, 2],
        [("Anjali Verma", "M M M M M M E E"), ("Priya Nair", "N N N/O D/O E E E E")],
        {},
    )
    right = tile([1, 2, 3, 4, 5, 6, 7, 8], [("Anjali Verma", "E N M M M M M M"),
                                            ("Priya Nair", "E E E E E E E E")], {})  # fmt: skip
    rows, _ = photo.merge([left, right], horizon)
    assert rows[0]["cells"][:6] == ["M"] * 6
    assert rows[0]["cells"][6:8] == ["E", "?"]  # 1 Nov agrees, 2 Nov was read as E and N
    assert rows[1]["cells"][2:4] == ["N/O", "D/O"]
    assert rows[1]["cells"][13] == "E"


def test_right_tile_with_an_extra_row_stays_aligned() -> None:
    def rows(*spec: str) -> list[dict[str, Any]]:
        return [{"name": x.split(":")[0], "cells": x.split(":")[1].split()} for x in spec]

    base = rows("A:M E N N/O", "B:E E M M", "C:N N N N/O")
    other = rows("A:N N/O M", "X:G G G", "B:M M E", "C:N N/O D/O")  # X: a row read twice
    assert photo._row_map(base, [0, 1, 2, 3], other, [2, 3, 4]) == [0, None, 1, 2]


def test_duplicate_names_take_ward_order_and_are_flagged() -> None:
    ward = load_seed("real_a").ward  # N02 and N12 are both Priya Nair
    rows = photo.map_names(
        [{"name": "Priya Nair", "cells": []}, {"name": "Priya Nair", "cells": []},
         {"name": "Kamala Iyer", "cells": []}, {"name": "Nobody Here", "cells": []}], ward
    )  # fmt: skip
    assert [(r.nurse_id, r.flagged) for r in rows] == [
        ("N02", True), ("N12", True), ("S1", False), (None, True),
    ]  # fmt: skip
    assert rows[0].candidates == ["N02", "N12"]


def test_checksum_flags_days_that_miss_the_printed_totals() -> None:
    rows = [photo.PhotoRow("A", "N01", ["N01"], ["M", "M"]),
            photo.PhotoRow("B", "N02", ["N02"], ["M", "E"])]  # fmt: skip
    out = photo.check_totals(rows, {"M": [2, 2], "E": [0, None], "N": [0, 0]})
    assert out == [{"day": 2, "shift": "M", "cells": 1, "printed": 2}]


def test_unreadable_image_is_refused() -> None:
    with pytest.raises(photo.PhotoError):
        photo.normalise(b"not an image")


def test_eval_photos_replay_from_the_cache(tmp_path: Path) -> None:
    """The five synthetic sheets recorded live replay with no misses and score exactly what the
    committed report says (the report holds the accuracy; this guards the pipeline after it)."""
    from evals.run import load, run_vision_suite
    from evals.vision_eval import summarize_vision

    report = json.loads(
        (ROOT / "evals" / "reports" / "2026-10-29-vision-baseten-live.json").read_text()
    )
    cases = load("vision", None, None)
    rows = asyncio.run(run_vision_suite(cases, provider="baseten", mode="replay", max_usd=0.0,
                                        workers=2, db=tmp_path / "v.db"))  # fmt: skip
    assert not [r for r in rows if "CacheMiss" in " ".join(r["reasons"])]
    s = summarize_vision(rows)
    assert s["cells"] == report["summary"]["cells"] and s["photos"] == 5
    assert all(r["rows_found"] == r["rows_expected"] for r in rows)


# ---------------------------------------------------------------- import


def label_rows(sheet: str) -> list[tuple[str, list[str]]]:
    lab = next(x for x in LABELS if x["id"] == sheet)
    return [(r["nurse_id"], list(r["cells"])) for r in lab["rows"]]


def test_import_publishes_a_planned_run_with_leave(conn: Any) -> None:
    seed_db(conn, "mini")
    ward = repo.get_ward(conn)
    assert ward is not None
    rows = label_rows("mini-b")  # N06 on CL for days 10-12
    before = repo.get_published_run(conn, ward.id)
    res = import_grid(conn, ward, rows)
    run = repo.get_published_run(conn, ward.id)
    assert run is not None and run["run_id"] == res.run_id and res.superseded == before["run_id"]
    assert run["kind"] == RunKind.IMPORT.value and run["parent_run"] is None
    q = "SELECT nurse_id, day, shift FROM assignment WHERE run_id = ?"
    cells = {(r["nurse_id"], r["day"]): r["shift"] for r in conn.execute(q, (res.run_id,))}
    want = dict(rows)
    for (nid, day), shift in cells.items():
        i = (datetime.fromisoformat(day).date() - ward.horizon_start).days
        code = want[nid][i]
        assert shift == (code if code in ("M", "E", "N", "G") else None)
    leave = [repo.get_request(conn, rid) for rid in res.leave_requests]
    cl = [r for r in leave if r and r.nurse_id == "N06"]
    assert len(cl) == 1 and cl[0].kind is RequestKind.LEAVE and cl[0].status is RequestStatus.ACTIVE
    assert [d.day for d in cl[0].dates] == [4, 5, 6]  # 4-6 Nov
    kinds = [e["kind"] for e in repo.list_events(conn, ward.id)]
    assert "roster.imported" in kinds and "run.published" in kinds


def test_import_refuses_unread_cells_and_unknown_nurses(conn: Any) -> None:
    seed_db(conn, "mini", solve=False)
    ward = repo.get_ward(conn)
    assert ward is not None
    rows = label_rows("mini-a")
    rows[0][1][3] = "?"
    with pytest.raises(ImportRejected) as e:
        import_grid(conn, ward, rows)
    assert e.value.code == "UNREADABLE_CELLS"
    with pytest.raises(ImportRejected) as e:
        import_grid(conn, ward, [("X99", ["M"] * 14)])
    assert e.value.code == "NURSE_NOT_FOUND"
    with pytest.raises(ImportRejected) as e:
        import_grid(conn, ward, [("N01", ["M"] * 14), ("N01", ["E"] * 14)])
    assert e.value.code == "DUPLICATE_NURSE"


def test_import_reports_broken_rules_as_warnings(conn: Any) -> None:
    seed_db(conn, "mini", solve=False)
    ward = repo.get_ward(conn)
    assert ward is not None
    rows = label_rows("mini-a")
    rows[0] = (rows[0][0], ["N", "M"] + rows[0][1][2:])  # a morning right after a night
    res = import_grid(conn, ward, rows)
    assert any(w.startswith("rest_after_night:N01") for w in res.warnings)


# ---------------------------------------------------------------- endpoints (mock mode)


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SEED", "mini")
    monkeypatch.setenv("DEMO_NOW", "2026-11-03T06:05:00+05:30")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with TestClient(app) as c:
        yield c


def test_voice_note_endpoint_in_mock_mode(client: TestClient) -> None:
    r = client.post("/api/messages", data={"sender_id": "N03"},
                    files={"audio": ("note.wav", CLIP.read_bytes(), "audio/wav")})  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] == TYPED and body["reply_kind"] == "confirm"
    assert body["parse"]["edits"][0]["op"] == "mark_unavailable"
    msgs = client.get("/api/messages", params={"nurse_id": "N03"}).json()
    assert msgs[-1]["channel"] == "voice" and msgs[-1]["body"] == TYPED
    silent = client.post("/api/messages", data={"sender_id": "N03"},
                         files={"audio": ("x.wav", b"RIFF0000WAVE", "audio/wav")})  # fmt: skip
    assert silent.status_code == 422 and silent.json()["error"]["code"] == "VOICE_UNCLEAR"
    webm = client.post("/api/messages", data={"sender_id": "N03"},
                       files={"audio": ("x.webm", b"...", "audio/webm")})  # fmt: skip
    assert webm.json()["error"]["code"] == "AUDIO_FORMAT"


def test_photo_endpoint_refuses_a_bad_file_and_import_publishes(client: TestClient) -> None:
    r = client.post("/api/import/photo", files={"image": ("x.jpg", b"nope", "image/jpeg")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "PHOTO_UNREADABLE"
    rows = [{"nurse_id": n, "cells": c} for n, c in label_rows("mini-a")]
    r = client.post("/api/import", json={"rows": rows})
    assert r.status_code == 200, r.text
    assert client.get("/api/roster").json()["run"]["run_id"] == r.json()["run_id"]
    bad = client.post("/api/import", json={"rows": [{"nurse_id": "N01", "cells": ["?"] * 14}]})
    assert bad.status_code == 422


def test_photo_endpoint_reads_a_sheet_in_mock_mode(client: TestClient) -> None:
    """The mock cannot see; a pinned answer stands in so the endpoint's shape is tested."""
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, "JPEG")
    lab = next(x for x in LABELS if x["id"] == "mini-a")
    answer = {"days": [26, 27, 28, 29, 30, 31, 1, 2, 3, 4, 5, 6, 7, 8],
              "rows": [{"name": r["name"], "cells": r["cells"]} for r in lab["rows"]],
              "totals": lab["totals"]}  # fmt: skip

    async def fake(gateway: Any, image: bytes, t: int, of: int, names: Any = None) -> Any:
        return photo.parse_tile(answer)

    mp = pytest.MonkeyPatch()
    mp.setattr(photo, "read_tile", fake)
    try:
        r = client.post(
            "/api/import/photo", files={"image": ("s.jpg", buf.getvalue(), "image/jpeg")}
        )
    finally:
        mp.undo()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tiles"] == 1 and body["unreadable"] == 0 and not body["mismatches"]
    assert [row["nurse_id"] for row in body["rows"]] == [n for n, _ in label_rows("mini-a")]
    assert body["days"][0] == "2026-10-26"
