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
import os
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
from evals.agent_eval import summarize_agent
from evals.score import score_case, score_explain, summarize, summarize_explain
from evals.vision_eval import load_vision, run_vision, summarize_vision

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
REPORTS = EVALS / "reports"
CACHE_FILE = ROOT / "data" / "llm_cache.jsonl"
FAILED = {"first_pass_ok": False, "repaired": False, "schema_failed": False, "latency_ms": 0,
          "cost_usd": 0.0, "cache_hits": 0}  # fmt: skip


def load(suite: str, limit: int | None, ids: list[str] | None) -> list[dict[str, Any]]:
    if suite == "vision":
        return load_vision(limit, ids)
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
                    now=datetime.fromisoformat(case["now"]), check_roster=False, explain=False,
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


async def run_explain(
    cases: list[dict[str, Any]], *, provider: str, mode: str, max_usd: float, workers: int, db: Path
) -> list[dict[str, Any]]:
    """Each case gets its own world database (evals/setups.py); the gateway's cache and call log
    live in `db`, which imports and (live) exports data/llm_cache.jsonl."""
    from app.explain.agent import ask
    from evals.setups import prepare

    s = get_settings().model_copy(
        update={
            "llm_mode": mode,
            "db_path": str(db),
            "llm_provider_default": provider if provider != "mock" else "baseten",
            "llm_provider_explain": None,
            "llm_spend_cap_usd": max_usd,
        }
    )
    conn = open_db(db)
    cache.import_jsonl(conn, CACHE_FILE)
    conn.close()
    gw = LLMGateway(s)
    sem = asyncio.Semaphore(workers)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            world = open_db(db.with_name(f"{case['id']}.db"))
            row: dict[str, Any] = {"id": case["id"], "tags": case["tags"], "expect": case["expect"],
                                   "question": case["question"]}  # fmt: skip
            try:
                now = datetime.fromisoformat(case["now"])
                run_id = prepare(world, case["setup"], now)  # synchronous: nothing interleaves
                ward = repo.get_ward(world)
                assert ward is not None
                r = await ask(world, ward, case["question"], asker_id=case["asker"], gateway=gw,
                              run_id=run_id, cell=case.get("cell"),
                              today=now.date().isoformat())  # fmt: skip
                calls = [{"tool": c["tool"], "args": c["args"]} for c in r.tool_calls]
                got = {"answer": r.answer, "claims": r.claims, "tool_calls": calls}
                row |= {"got": got, "grounded": r.grounded, "fallback_used": r.fallback_used,
                        "retried": r.retried, "problems": r.problems, "latency_ms": r.latency_ms,
                        "cost_usd": r.cost_usd, "model_turns": r.model_turns}  # fmt: skip
            except Exception as exc:  # noqa: BLE001 - a failed case is a finding, not a crash
                row |= {"got": {"error": repr(exc)}, "error": repr(exc), "latency_ms": 0}
            finally:
                world.close()
            row["score"] = score_explain(case["expect"], row["got"])
            row["pass"] = row["score"]["pass"]
            mark = "PASS" if row["pass"] else "FAIL"
            g = (
                "grounded"
                if row.get("grounded")
                else ("FALLBACK" if row.get("fallback_used") else "")
            )
            print(f"{mark} {case['id']} {row['latency_ms']:>6} ms {g:9} "
                  f"{'; '.join(row['score']['reasons'])[:120]}")  # fmt: skip
            return row

    rows = await asyncio.gather(*(one(c) for c in cases))
    if mode == "live":
        conn = connect(db)
        n = cache.export_jsonl(conn, CACHE_FILE)
        conn.close()
        print(f"cache: {n} entries in {CACHE_FILE.relative_to(ROOT)}")
    return list(rows)


def _settings(provider: str, mode: str, db: Path, max_usd: float) -> Any:
    return get_settings().model_copy(
        update={
            "llm_mode": mode,
            "db_path": str(db),
            "llm_provider_default": provider if provider != "mock" else "baseten",
            "llm_provider_explain": None,
            "llm_provider_message": None,
            "llm_spend_cap_usd": max_usd,
        }
    )


def _export(mode: str, db: Path) -> None:
    if mode == "live":
        conn = connect(db)
        n = cache.export_jsonl(conn, CACHE_FILE)
        conn.close()
        print(f"cache: {n} entries in {CACHE_FILE.relative_to(ROOT)}")


async def run_writer(
    cases: list[dict[str, Any]], *, provider: str, mode: str, max_usd: float, workers: int, db: Path
) -> list[dict[str, Any]]:
    """Message writer (7.7): each case drafts one ask on the default seed ward."""
    from app.agent.writer import check, write
    from app.explain.narrate import health_words

    conn = open_db(db)
    seed_db(conn, "default", solve=False)
    cache.import_jsonl(conn, CACHE_FILE)
    ward = repo.get_ward(conn)
    conn.close()
    assert ward is not None
    gw = LLMGateway(_settings(provider, mode, db, max_usd))
    sem = asyncio.Semaphore(workers)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            w = await write(ward, nurse_id=case["nurse"], day=date.fromisoformat(case["day"]),
                            shift=case["shift"], kind=case["kind"], facts=case["facts"],
                            gateway=gw)  # fmt: skip
            final = check(w.text, w.context)
            leaked = health_words(w.draft)
            row = {"id": case["id"], "tags": case["tags"], "text": w.text, "source": w.source,
                   "language": w.language, "problems": w.problems, "draft_health_words": leaked,
                   "final_problems": final, "pass": not final,
                   "reasons": final + [f"draft: {p}" for p in w.problems]}  # fmt: skip
            print(f"{'PASS' if row['pass'] else 'FAIL'} {case['id']} {w.source:8} {w.text[:90]}")
            return row

    rows = list(await asyncio.gather(*(one(c) for c in cases)))
    _export(mode, db)
    return rows


def summarize_writer(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1
    return {
        "cases": len(rows),
        "final_pass": f"{sum(r['pass'] for r in rows)}/{len(rows)}",
        "model_draft_accepted": f"{100 * sum(r['source'] == 'model' for r in rows) / n:.0f}%",
        "drafts_with_health_words": sum(1 for r in rows if r["draft_health_words"]),
        "final_with_health_words": sum(
            1 for r in rows if any("health" in p for p in r["final_problems"])
        ),
        "cache_misses": sum(1 for r in rows if any("CacheMiss" in p for p in r["problems"])),
        "errors": sum(1 for r in rows if not r["pass"]),
        "by_tag": {},
    }


async def run_agent(
    cases: list[dict[str, Any]], *, provider: str, mode: str, max_usd: float, workers: int, db: Path
) -> list[dict[str, Any]]:
    """Roster agent (7.10): every scenario by the agent, then by the deterministic fallback."""
    from evals.agent_eval import run_agent_suite

    saved = {k: os.environ.get(k) for k in ("DEMO_NOW", "DB_PATH")}
    conn = open_db(db)
    cache.import_jsonl(conn, CACHE_FILE)
    conn.close()
    gw = LLMGateway(_settings(provider, mode, db, max_usd))
    try:
        rows = await run_agent_suite(cases, gateway=gw, tmp=db.parent,
                                     modes=("agent", "fallback"))  # fmt: skip
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()
    _export(mode, db)
    return rows


async def run_vision_suite(
    cases: list[dict[str, Any]], *, provider: str, mode: str, max_usd: float, workers: int, db: Path
) -> list[dict[str, Any]]:
    """Roster photo reading (7.8): each sheet image read into a grid and scored per cell."""
    conn = open_db(db)
    cache.import_jsonl(conn, CACHE_FILE)
    conn.close()
    s = _settings(provider, mode, db, max_usd).model_copy(update={"llm_provider_vision": None})
    rows = await run_vision(cases, gateway=LLMGateway(s), db=db, workers=workers)
    _export(mode, db)
    return rows


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
        *[
            f"- `{r['id']}`: {'; '.join(r.get('reasons') or r['score']['reasons'])}"
            for r in rows
            if not r["pass"]
        ],
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
    ap.add_argument(
        "--suite", default="intake", choices=["intake", "explain", "writer", "agent", "vision"]
    )
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
        runner = {"explain": run_explain, "writer": run_writer, "agent": run_agent,
                  "vision": run_vision_suite}.get(a.suite, run_intake)  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            rows = asyncio.run(
                runner(cases, provider=prov, mode=mode, max_usd=a.max_usd,
                       workers=a.workers, db=Path(tmp) / "eval.db")
            )  # fmt: skip
        summary = {
            "explain": summarize_explain,
            "writer": summarize_writer,
            "agent": summarize_agent,
            "vision": summarize_vision,
        }.get(a.suite, summarize)(rows)
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
