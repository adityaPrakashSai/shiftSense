"""Write the API's OpenAPI schema to web/openapi.json (input to the TS client generator).

uv run python -m scripts.export_openapi      # or: make api
"""

import json
from pathlib import Path

from app.main import app

OUT = Path(__file__).resolve().parents[1] / "web" / "openapi.json"

if __name__ == "__main__":
    OUT.write_text(json.dumps(app.openapi(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
