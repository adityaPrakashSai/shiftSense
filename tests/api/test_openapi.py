"""The committed OpenAPI schema (input to the web client's generated types) matches the app.
Run `make api` after changing a route or schema."""

import json
from pathlib import Path

from app.main import app

SCHEMA = Path(__file__).resolve().parents[2] / "web" / "openapi.json"


def test_web_openapi_schema_is_current() -> None:
    assert json.loads(SCHEMA.read_text(encoding="utf-8")) == app.openapi(), "run `make api`"
