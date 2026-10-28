"""GET /api/ward (design doc 10.1)."""

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import get_conn
from app.api.errors import ApiError
from app.store import repo

router = APIRouter(prefix="/api")


@router.get("/ward")
def get_ward(conn: Annotated[sqlite3.Connection, Depends(get_conn)]) -> dict[str, Any]:
    ward = repo.get_ward(conn)
    if ward is None:
        raise ApiError(404, "WARD_NOT_FOUND", "No ward in the database; run `make seed`.")
    return ward.model_dump(mode="json")
