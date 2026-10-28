"""Eval runner (design doc 9.3, 9.4).

    uv run python -m evals.run --suite intake --provider baseten [--mode live|replay|mock]
                               [--limit N] [--max-usd 2] [--compare baseten,meta]

Each run uses a fresh temporary database seeded with the default ward (the eval's dates assume
it), imports the committed response cache `data/llm_cache.jsonl`, and pins `now` per case. Live
runs add their new responses to that file, so CI can re-run the suite in replay mode without
spending credits. Swap cells are not checked against the published roster here: the seed roster
comes from a time-limited multi-worker solve and is not identical between runs.

Writes `evals/reports/{date}-{suite}-{provider}.json` and a markdown table beside it.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.llm import cache
from app.llm.gateway import LLMGateway
from app.store import repo
from app.store.db import connect, open_db
from app.store.seed import seed_db
from evals.score import score_case, summarize

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
REPORTS = EVALS / "reports"
CACHE_FILE = ROOT / "data" / "llm_cache.jsonl"
FAILED = {"first_pass_ok": False, "repaired": False, "schema_failed": False, "latency_ms": 0,
          "cost_usd": 0.0, "cache_hits": 0}  # fmt: skip


def load(suite: str, limit: int | None, ids: list[str] | None) -> list[dict[str, Any]]:
    path = EVALS / f"{suite}.jsonl"
    cases = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    return cases[:limit] if limit else cases


async def run_intake(
    cases: list[dict[str, Any]], *, provider: str, mode: str, max_usd: float, workers: int, db: Path
) -> list[dict[str, Any]]:
    from app.intake.pipeline import handle_message

    s = get_settings().model_copy(
        update={
            "llm_mode": mode,
            "db_path": str(db),
            "llm_provider_default": provider if provider != "mock" else "baseten",
            "llm_provider_intake": None,
            "llm_spend_cap_usd": max_usd,
        }
    )
    conn = open_db(db)
    seed_db(conn, "default", solve=False)
    cache.import_jsonl(conn, CACHE_FILE)
    ward = repo.get_ward(conn)
    conn.close()
    assert ward is not None
    gw = LLMGateway(s)
    sem = asyncio.Semaphore(workers)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            c = connect(db)
            row: dict[str, Any] = {"id": case["id"], "tags": case["tags"], "expect": case["expect"]}
            try:
                res = await handle_message(
                    c, ward, case["sender"], case["message"], gateway=gw,
                    now=datetime.fromisoformat(case["now"]), check_roster=False,
                )  # fmt: skip
                confirmed = res.reply_kind == "confirm"
                row["got"] = {
                    "intent": res.parse.intent if res.parse else None,
                    "clarify": res.reply_kind == "clarify",
                    "edits": [r.key() for r in res.resolved] if confirmed else [],
                    "reply_kind": res.reply_kind,
                    "reply": res.reply,
                    "problems": [p.code for p in res.problems],
                    "parse": None if res.parse is None else res.parse.model_dump(mode="json"),
                }
                row |= {
                    "first_pass_ok": res.first_pass_ok,
                    "repaired": res.repaired,
                    "schema_failed": res.schema_failed,
                    "latency_ms": res.latency_ms,
                    "cost_usd": res.cost_usd,
                    "cache_hits": res.cache_hits,
                }
            except Exception as exc:  # noqa: BLE001 - a failed case is a finding, not a crash
                row["got"] = {"intent": None, "clarify": False, "edits": [], "error": repr(exc)}
                row |= dict(FAILED, error=repr(exc))
            finally:
                c.close()
            ok, reasons = score_case(case["expect"], {**row["got"], "error": row.get("error")})
            row |= {"pass": ok, "reasons": reasons}
            mark = "PASS" if ok else "FAIL"
            print(f"{mark} {case['id']} {row['latency_ms']:>6} ms  {'; '.join(reasons)[:150]}")
            return row

    rows = await asyncio.gather(*(one(c) for c in cases))
    if mode == "live":
        conn = connect(db)
        n = cache.export_jsonl(conn, CACHE_FILE)
        conn.close()
        print(f"cache: {n} entries in {CACHE_FILE.relative_to(ROOT)}")
    return list(rows)


def markdown(suite: str, provider: str, mode: str, summary: dict[str, Any], rows: list[Any]) -> str:
    keys = [k for k in summary if k != "by_tag"]
    lines = [
        f"# {suite} eval · {provider} · {mode} · {date.today().isoformat()}",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        *[f"| {k} | {summary[k]} |" for k in keys],
        "",
        "| Category | Passed |",
        "| --- | --- |",
        *[f"| {t} | {v} |" for t, v in summary["by_tag"].items()],
        "",
        "## Failures",
        "",
        *[f"- `{r['id']}`: {'; '.join(r['reasons'])}" for r in rows if not r["pass"]],
    ]
    return "\n".join(lines) + "\n"


def compare_table(results: dict[str, dict[str, Any]]) -> str:
    providers = list(results)
    keys = [k for k in next(iter(results.values())) if k != "by_tag"]
    head = "| Metric | " + " | ".join(providers) + " |"
    sep = "| --- |" + " --- |" * len(providers)
    body = [f"| {k} | " + " | ".join(str(results[p][k]) for p in providers) + " |" for k in keys]
    return "\n".join([head, sep, *body]) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a fixed eval suite.")
    ap.add_argument("--suite", default="intake", choices=["intake"])
    ap.add_argument("--provider", default="baseten", choices=["baseten", "meta", "modal", "mock"])
    ap.add_argument("--mode", default="live", choices=["live", "replay", "mock"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids", help="comma-separated case ids")
    ap.add_argument("--max-usd", type=float, default=2.0, help="spend cap for this run (9.4)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--compare", help="comma-separated providers, e.g. baseten,meta")
    ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args(argv)
    cases = load(a.suite, a.limit, a.ids.split(",") if a.ids else None)
    providers = a.compare.split(",") if a.compare else [a.provider]
    summaries: dict[str, dict[str, Any]] = {}
    errors = 0
    for prov in providers:
        mode = "mock" if prov == "mock" else a.mode
        with tempfile.TemporaryDirectory() as tmp:
            rows = asyncio.run(
                run_intake(cases, provider=prov, mode=mode, max_usd=a.max_usd,
                           workers=a.workers, db=Path(tmp) / "eval.db")
            )  # fmt: skip
        summary = summarize(rows)
        summaries[prov] = summary
        errors += summary["errors"]
        print(json.dumps({k: v for k, v in summary.items()}, indent=1, ensure_ascii=False))
        if not a.no_report:
            REPORTS.mkdir(parents=True, exist_ok=True)
            stem = REPORTS / f"{date.today().isoformat()}-{a.suite}-{prov}-{mode}"
            report = {
                "suite": a.suite,
                "provider": prov,
                "mode": mode,
                "date": date.today().isoformat(),
                "summary": summary,
                "cases": rows,
            }
            stem.with_suffix(".json").write_text(
                json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            stem.with_suffix(".md").write_text(
                markdown(a.suite, prov, mode, summary, rows), encoding="utf-8"
            )
            print(f"report: {stem.relative_to(ROOT)}.json / .md")
    if len(providers) > 1:
        table = compare_table(summaries)
        print(table)
        if not a.no_report:
            (REPORTS / f"{date.today().isoformat()}-{a.suite}-compare.md").write_text(table)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
