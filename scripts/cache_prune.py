"""Drop dead entries from data/llm_cache.jsonl: responses to prompts that no longer exist.

    uv run python -m scripts.cache_prune [--dry-run]

An entry is live when every system message it was asked with (stored as a sha256) is one the
app would send today: the intake prompt with its examples, the schema prompts, the explain,
narrate, message and agent prompts, or the JSON tool-protocol prompts. Editing a prompt changes
its hash, so the old answers can never be hit again (the cache key includes the prompt version);
this keeps the committed file small.
"""

import argparse
import hashlib
import json
from pathlib import Path

from app.agent.tools import ALLOWLIST, specs
from app.agent.writer import Draft
from app.domain.edits import constraint_edit_schema
from app.explain.agent import prompt
from app.explain.narrate import Narrative
from app.explain.tools import SPECS
from app.intake.pipeline import system_prompt
from app.llm.jsonproto import schema_prompt, tools_prompt

FILE = Path(__file__).resolve().parent.parent / "data" / "llm_cache.jsonl"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def current() -> set[str]:
    return {
        sha(system_prompt()),
        sha(schema_prompt(constraint_edit_schema())),
        sha(prompt("explain")),
        sha(tools_prompt(SPECS)),
        sha(prompt("narrate")),
        sha(schema_prompt(Narrative.model_json_schema())),
        sha(prompt("message")),
        sha(schema_prompt(Draft.model_json_schema())),
        sha(prompt("agent")),
        *(sha(tools_prompt(specs(wf))) for wf in ALLOWLIST),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    live = current()
    rows = [json.loads(x) for x in FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    keep = []
    for r in rows:
        systems = [m.get("sha256") for m in (r.get("request") or {}).get("messages", [])
                   if m.get("role") == "system"]  # fmt: skip
        if all(s in live for s in systems):
            keep.append(r)
    print(f"{len(rows)} entries, {len(rows) - len(keep)} dead, {len(keep)} kept")
    if not a.dry_run:
        FILE.write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in keep),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
