"""Roster photo import (design doc 7.8, US-13): read a photo into a preview, import the grid."""

from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from app.api import jobs
from app.api.deps import Conn, require_ward
from app.api.errors import ApiError
from app.api.schemas import ImportBody, ImportOut, PhotoReadOut
from app.api.ws import hub
from app.intake.photo import PhotoError, read_photo
from app.llm.gateway import get_gateway
from app.planner.importer import ImportRejected, import_grid

router = APIRouter(prefix="/api/import", tags=["import"])

MAX_PHOTO_BYTES = 12_000_000


@router.post("/photo", response_model=PhotoReadOut)
async def photo(conn: Conn, image: Annotated[UploadFile, File()]) -> PhotoReadOut:
    """Muse Spark reads the photo into the current period's grid; nothing is stored. Cells it
    could not read are "?", and days whose cells miss the printed totals are listed."""
    ward = require_ward(conn)
    raw = await image.read()
    if len(raw) > MAX_PHOTO_BYTES:
        raise ApiError(413, "PHOTO_TOO_LARGE", "Photos up to 12 MB, please.")
    try:
        read = await read_photo(raw, ward, get_gateway())
    except PhotoError as exc:
        raise ApiError(422, "PHOTO_UNREADABLE", str(exc)) from exc
    return PhotoReadOut.model_validate(read, from_attributes=True)


@router.post("", response_model=ImportOut)
async def import_roster(conn: Conn, body: ImportBody) -> ImportOut:
    """The corrected grid becomes the published, planned run (`kind = import`)."""
    ward = require_ward(conn)
    rows = [(r.nurse_id, r.cells) for r in body.rows]
    try:
        async with jobs.ward_lock(ward.id):
            res = import_grid(conn, ward, rows, by=body.by, log=jobs.live_log(conn, ward.id))
    except ImportRejected as exc:
        raise ApiError(422, exc.code, str(exc)) from exc
    for rid in res.leave_requests:
        await hub.broadcast(ward.id, {"type": "request_updated", "request_id": rid})
    await hub.broadcast(ward.id, {"type": "roster_published", "run_id": res.run_id})
    return ImportOut.model_validate(res, from_attributes=True)
