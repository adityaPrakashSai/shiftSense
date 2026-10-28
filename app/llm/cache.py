"""Content-addressed response cache (design doc 9.1).

Key: sha256 of canonical JSON of {provider, model, role, messages, tools, response_schema,
temperature, max_tokens, prompt_version}. Image and audio parts are replaced by the sha256 of their
bytes. `prompt_version` hashes the role's prompt files, so editing a prompt invalidates only that
role's entries.
"""

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.llm.types import LLMRequest

PROMPTS = Path(__file__).with_name("prompts")


def prompt_version(role: str, prompts_dir: Path = PROMPTS) -> str:
    """Hash of every prompt file that belongs to a role (`{role}.md`, `{role}_*.jsonl`, ...)."""
    h = hashlib.sha256()
    files = sorted(p for p in prompts_dir.glob(f"{role}*") if p.is_file())
    for p in files:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16] if files else "none"


def _media_digest(part: dict[str, Any]) -> dict[str, Any]:
    """Replace inline image / audio bytes by their sha256 (9.1 media rule)."""
    kind = part.get("type")
    if kind == "image_url":
        url = str(part.get("image_url", {}).get("url", ""))
        data = url.split(",", 1)[1] if url.startswith("data:") and "," in url else url
        raw = base64.b64decode(data) if url.startswith("data:") else data.encode()
        return {"type": "image_url", "sha256": hashlib.sha256(raw).hexdigest()}
    if kind == "input_audio":
        raw = base64.b64decode(str(part.get("input_audio", {}).get("data", "")))
        return {"type": "input_audio", "sha256": hashlib.sha256(raw).hexdigest()}
    return part


def _key_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            m = {**m, "content": [_media_digest(p) if isinstance(p, dict) else p for p in content]}
        out.append(m)
    return out


def cache_key(provider: str, model: str, req: LLMRequest, prompts_dir: Path = PROMPTS) -> str:
    body = {
        "provider": provider,
        "model": model,
        "role": req.role.value,
        "messages": _key_messages(req.messages),
        "tools": req.tools,
        "response_schema": req.response_schema,
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
        "prompt_version": prompt_version(req.role.value, prompts_dir),
    }
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def get(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT response_json FROM llm_cache WHERE cache_key = ?", (key,)).fetchone()
    return None if row is None else dict(json.loads(row[0]))


def _stored_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages as stored for inspection: system prompts (long, and already covered by the key
    through `prompt_version`) are replaced by their sha256, keeping the committed cache small."""
    out = []
    for m in _key_messages(messages):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            digest = hashlib.sha256(m["content"].encode()).hexdigest()
            m = {"role": "system", "sha256": digest, "chars": len(m["content"])}
        out.append(m)
    return out


def put(
    conn: sqlite3.Connection,
    key: str,
    provider: str,
    model: str,
    req: LLMRequest,
    response: dict[str, Any],
    created_at: str,
) -> None:
    request_json = json.dumps(
        {
            "role": req.role.value,
            "messages": _stored_messages(req.messages),
            "tools": req.tools,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        },
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache(cache_key, provider, model, request_json, response_json, "
        "created_at) VALUES(?, ?, ?, ?, ?, ?)",
        (key, provider, model, request_json, json.dumps(response, ensure_ascii=False), created_at),
    )
    conn.commit()


def export_jsonl(conn: sqlite3.Connection, path: Path) -> int:
    """Dump the cache to JSON lines, sorted by key (committed as data/llm_cache.jsonl)."""
    rows = conn.execute(
        "SELECT cache_key, provider, model, request_json, response_json, created_at "
        "FROM llm_cache ORDER BY cache_key"
    ).fetchall()
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(
                json.dumps(
                    {
                        "cache_key": r[0],
                        "provider": r[1],
                        "model": r[2],
                        "request": json.loads(r[3] or "null"),
                        "response": json.loads(r[4]),
                        "created_at": r[5],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    return len(rows)


def import_jsonl(conn: sqlite3.Connection, path: Path) -> int:
    """Load cache entries that are not already present. Returns the number added."""
    if not path.exists():
        return 0
    before = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
    with path.open(encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    conn.executemany(
        "INSERT OR IGNORE INTO llm_cache(cache_key, provider, model, request_json, response_json, "
        "created_at) VALUES(?, ?, ?, ?, ?, ?)",
        [
            (
                r["cache_key"],
                r["provider"],
                r["model"],
                json.dumps(r.get("request"), ensure_ascii=False),
                json.dumps(r["response"], ensure_ascii=False),
                r.get("created_at"),
            )
            for r in rows
        ],
    )
    conn.commit()
    return int(conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] - before)
