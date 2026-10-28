"""Load data/llm_cache.jsonl into the database's LLM cache (entries already present are kept).
The app also does this at startup.

    uv run python -m scripts.cache_import [--db data/shiftsense.db] [--src data/llm_cache.jsonl]
"""

import argparse
from pathlib import Path

from app.config import get_settings
from app.llm.cache import import_jsonl
from app.store.db import open_db

DEFAULT_SRC = Path(__file__).resolve().parents[1] / "data" / "llm_cache.jsonl"

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Import cached LLM responses from JSON lines.")
    ap.add_argument("--db", default=get_settings().db_path)
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    a = ap.parse_args()
    n = import_jsonl(open_db(a.db), a.src)
    print(f"imported {n} new cached responses from {a.src}")
