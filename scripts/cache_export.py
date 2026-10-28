"""Dump the LLM response cache to data/llm_cache.jsonl (committed), so CI and anyone cloning the
repo can run LLM_MODE=replay without keys (design doc 9.1).

    uv run python -m scripts.cache_export [--db data/shiftsense.db] [--out data/llm_cache.jsonl]
"""

import argparse
from pathlib import Path

from app.config import get_settings
from app.llm.cache import export_jsonl
from app.store.db import open_db

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "llm_cache.jsonl"

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Export the LLM cache to JSON lines.")
    ap.add_argument("--db", default=get_settings().db_path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    n = export_jsonl(open_db(a.db), a.out)
    print(f"exported {n} cached responses to {a.out}")
