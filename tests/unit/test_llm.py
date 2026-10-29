"""LLM gateway, cache, providers and mock (design doc 7.1, 9, 11.3 test_cache)."""

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.config import Settings, get_settings
from app.domain.edits import ConstraintEdit, EditOp
from app.llm import cache, providers
from app.llm.gateway import LLMGateway
from app.llm.mock import MockProvider, mock_intake
from app.llm.providers import OpenAICompatProvider
from app.llm.types import (
    CacheMiss,
    LLMRequest,
    MalformedToolCall,
    ProviderCaps,
    ProviderError,
    ProviderResult,
    Role,
    SpendCapReached,
    ToolCall,
)
from app.store.db import connect, open_db

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_shift",
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


class FakeProvider:
    def __init__(self, *results: Any, caps: ProviderCaps | None = None) -> None:
        self.name, self.model = "baseten", "meta-models/muse-spark-open-1026"
        self.caps = caps or ProviderCaps()
        self.results = list(results) or [
            ProviderResult(text='{"ok": true}', prompt_tokens=100, completion_tokens=200)
        ]
        self.calls: list[bool] = []

    async def complete(self, req: LLMRequest, *, json_protocol: bool) -> ProviderResult:
        self.calls.append(json_protocol)
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return ProviderResult(**vars(r))  # a fresh copy each call


def settings(db: Path, mode: str = "live", cap: float = 5.0) -> Settings:
    return get_settings().model_copy(
        update={
            "llm_mode": mode,
            "db_path": str(db),
            "llm_spend_cap_usd": cap,
            "llm_provider_intake": None,
            "llm_provider_explain": None,
        }
    )


def gateway(
    db: Path,
    fake: FakeProvider,
    mode: str = "live",
    cap: float = 5.0,
    prompts_dir: Path | None = None,
    mock: MockProvider | None = None,
) -> LLMGateway:
    open_db(db).close()
    return LLMGateway(
        settings(db, mode, cap),
        providers={"baseten": fake},
        mock=mock,
        prompts_dir=prompts_dir or cache.PROMPTS,
    )


def req(text: str = "hello", role: Role = Role.INTAKE, **kw: Any) -> LLMRequest:
    return LLMRequest(role, [{"role": "user", "content": text}], **kw)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def calls(db: Path) -> list[sqlite3.Row]:
    conn = connect(db)
    rows = conn.execute("SELECT * FROM llm_call ORDER BY ts").fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------- cache (test_cache in 11.3)


def test_same_request_one_live_call_then_hits(tmp_path: Path) -> None:
    fake = FakeProvider()
    gw = gateway(tmp_path / "a.db", fake)
    first = run(gw.complete(req(response_schema={"type": "object"})))
    second = run(gw.complete(req(response_schema={"type": "object"})))
    third = run(gw.complete(req(response_schema={"type": "object"})))
    assert len(fake.calls) == 1
    assert not first.cache_hit and first.cost_usd > 0 and first.parsed == {"ok": True}
    assert second.cache_hit and third.cache_hit and second.cost_usd == 0.0
    assert second.parsed == {"ok": True} and second.provider == "baseten"
    assert second.latency_ms < 20
    rows = calls(tmp_path / "a.db")
    assert [r["cache_hit"] for r in rows] == [0, 1, 1] and all(r["ok"] for r in rows)


def test_prompt_edit_invalidates_only_that_role(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "intake.md").write_text("v1")
    (prompts / "explain.md").write_text("e1")
    fake = FakeProvider()
    gw = gateway(tmp_path / "a.db", fake, prompts_dir=prompts)
    run(gw.complete(req()))
    run(gw.complete(req(role=Role.EXPLAIN)))
    assert len(fake.calls) == 2
    (prompts / "intake.md").write_text("v2")
    assert not run(gw.complete(req())).cache_hit
    assert run(gw.complete(req(role=Role.EXPLAIN))).cache_hit
    assert len(fake.calls) == 3


def test_replay_serves_hits_and_raises_on_miss(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    fake = FakeProvider()
    run(gateway(db, fake).complete(req("cached")))
    replay = gateway(db, fake, mode="replay")
    assert run(replay.complete(req("cached"))).cache_hit
    with pytest.raises(CacheMiss):
        run(replay.complete(req("never seen")))
    assert len(fake.calls) == 1
    assert calls(db)[-1]["ok"] == 0 and "CacheMiss" in calls(db)[-1]["error"]


def test_spend_cap_blocks_live_calls_but_not_cache_or_mock(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    big = ProviderResult(text="x", prompt_tokens=1_000_000, completion_tokens=0)
    fake = FakeProvider(big)
    gw = gateway(db, fake, cap=0.10)  # one call costs $0.50 at the placeholder price
    run(gw.complete(req("first")))
    assert gw.spent_usd >= 0.10
    with pytest.raises(SpendCapReached):
        run(gw.complete(req("second")))
    assert len(fake.calls) == 1
    assert run(gw.complete(req("first"))).cache_hit  # cached answers still work
    mock_gw = gateway(db, fake, mode="mock", cap=0.0)
    assert run(mock_gw.complete(req("Kal mujhe off chahiye"))).provider == "mock"


def test_media_parts_are_hashed_in_the_key() -> None:
    def image(data: bytes) -> LLMRequest:
        url = "data:image/png;base64," + base64.b64encode(data).decode()
        return LLMRequest(
            Role.VISION,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
        )

    k1 = cache.cache_key("baseten", "m", image(b"photo-1"))
    assert k1 == cache.cache_key("baseten", "m", image(b"photo-1"))
    assert k1 != cache.cache_key("baseten", "m", image(b"photo-2"))
    assert k1 != cache.cache_key("meta", "m", image(b"photo-1"))


def test_run_ids_replay_on_a_new_run(tmp_path: Path) -> None:
    """Run ids carry the wall-clock time; the key uses placeholders, so the scripted demo's answer
    for one run is served for the next, with that run's ids put back (M11)."""
    a, b, c = "run-20261103-060501-ab12", "run-20261103-061502-cd34", "run-20261029-053317-35a7"
    fake = FakeProvider(
        ProviderResult(
            text=f"{a} replaces {c}",
            tool_calls=[ToolCall("t1", "get_run", {"run_id": a})],
            prompt_tokens=10,
            completion_tokens=10,
        )
    )
    gw = gateway(tmp_path / "a.db", fake)
    first = run(gw.complete(req(f"compare {a} with {c}")))
    second = run(gw.complete(req(f"compare {b} with {c}")))
    assert len(fake.calls) == 1 and second.cache_hit and not first.cache_hit
    assert second.text == f"{b} replaces {c}"
    assert second.tool_calls[0].arguments == {"run_id": b}
    # a different shape (the ids swapped) is a different prompt
    assert cache.cache_key("baseten", "m", req(f"{a} {c}")) != cache.cache_key(
        "baseten", "m", req(f"{c} {c}")
    )
    stored = connect(tmp_path / "a.db").execute("SELECT * FROM llm_cache").fetchone()
    assert a not in stored["request_json"] + stored["response_json"]
    assert "run-ref-1" in stored["response_json"]


def test_cache_export_import_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    run(gateway(db, FakeProvider()).complete(req("keep me")))
    out = tmp_path / "llm_cache.jsonl"
    assert cache.export_jsonl(connect(db), out) == 1
    other = tmp_path / "b.db"
    conn = open_db(other)
    assert cache.import_jsonl(conn, out) == 1 and cache.import_jsonl(conn, out) == 0
    conn.close()
    replay = gateway(other, FakeProvider(), mode="replay")
    assert run(replay.complete(req("keep me"))).cache_hit


# ---------------------------------------------------------------- tools: native and JSON protocol


def test_json_protocol_when_provider_has_no_native_tools(tmp_path: Path) -> None:
    reply = ProviderResult(text='{"tool": "get_shift", "args": {"nurse": "N07"}}')
    fake = FakeProvider(reply, caps=ProviderCaps(native_tools=False))
    r = run(gateway(tmp_path / "a.db", fake).complete(req(tools=TOOLS)))
    assert [(c.name, c.arguments) for c in r.tool_calls] == [("get_shift", {"nurse": "N07"})]
    assert r.text is None and r.json_protocol


def test_two_malformed_native_calls_switch_the_role_to_json_protocol(tmp_path: Path) -> None:
    fake = FakeProvider(
        MalformedToolCall("bad"),
        MalformedToolCall("bad"),
        ProviderResult(text='{"final": {"answer": "done"}}'),
    )
    gw = gateway(tmp_path / "a.db", fake)
    r = run(gw.complete(req(tools=TOOLS)))
    assert fake.calls == [False, False, True]
    assert Role.INTAKE in gw.json_protocol_roles and r.json_protocol
    assert json.loads(r.text or "") == {"answer": "done"}


# ---------------------------------------------------------------- OpenAI-compatible provider


class FakeCompletions:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.kwargs: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> Any:
        self.kwargs.append(kw)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def completion(
    content: str | None, *, extra: dict[str, Any] | None = None, tool_calls: list[Any] | None = None
) -> Any:
    msg = SimpleNamespace(content=content, tool_calls=tool_calls, model_extra=extra or {})
    usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=40,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=25),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def http_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    return cls(message=f"status {status}", response=response, body=None)


def compat(fake: FakeCompletions, caps: ProviderCaps | None = None) -> OpenAICompatProvider:
    client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return OpenAICompatProvider(
        "baseten",
        "https://inference.baseten.co/v1",
        "test-key",
        "meta-models/muse-spark-open-1026",
        caps or ProviderCaps(),
        client,
    )


def test_reasoning_is_split_from_the_answer(tmp_path: Path) -> None:
    fake = FakeCompletions(
        completion(
            '<think>check dates</think>{"intent": "greeting"}',
            extra={"reasoning_content": "long thought"},
        )
    )
    p = compat(fake)
    gw = LLMGateway(settings(tmp_path / "a.db"), providers={"baseten": p})
    open_db(tmp_path / "a.db").close()
    r = run(gw.complete(req(response_schema={"type": "object"})))
    assert r.text == '{"intent": "greeting"}' and r.parsed == {"intent": "greeting"}
    assert "long thought" in (r.reasoning or "") and "check dates" in (r.reasoning or "")
    assert r.reasoning_tokens == 25
    row = calls(tmp_path / "a.db")[-1]
    assert row["reasoning"] and row["reasoning_tokens"] == 25
    sent = fake.kwargs[0]
    assert sent["response_format"]["type"] == "json_schema" and "tools" not in sent


def test_retries_on_429_and_5xx_but_not_other_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "BACKOFF_S", 0.0)
    ok = completion("fine")
    flaky = FakeCompletions(
        http_error(openai.RateLimitError, 429), http_error(openai.InternalServerError, 500), ok
    )
    assert run(compat(flaky).complete(req(), json_protocol=False)).text == "fine"
    assert len(flaky.kwargs) == 3
    bad = FakeCompletions(http_error(openai.BadRequestError, 400), ok)
    with pytest.raises(ProviderError):
        run(compat(bad).complete(req(), json_protocol=False))
    assert len(bad.kwargs) == 1
    down = FakeCompletions(*(http_error(openai.InternalServerError, 503) for _ in range(3)))
    with pytest.raises(ProviderError):
        run(compat(down).complete(req(), json_protocol=False))
    assert len(down.kwargs) == 3


def test_malformed_native_tool_arguments_raise() -> None:
    bad = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_shift", arguments="{nope"))
    fake = FakeCompletions(completion(None, tool_calls=[bad]))
    with pytest.raises(MalformedToolCall):
        run(compat(fake).complete(req(tools=TOOLS), json_protocol=False))


def test_no_json_schema_caps_falls_back_to_json_object() -> None:
    fake = FakeCompletions(completion('{"a": 1}'))
    p = compat(fake, ProviderCaps(json_schema=False))
    run(p.complete(req(response_schema={"type": "object"}), json_protocol=False))
    sent = fake.kwargs[0]
    assert sent["response_format"] == {"type": "json_object"}
    assert "Reply with one JSON object" in sent["messages"][0]["content"]


# ---------------------------------------------------------------- mock provider


CTX = {"today": "2026-11-03", "sender_id": "N07"}  # Tue 3 Nov 2026, the demo's DEMO_NOW day


@pytest.mark.parametrize(
    ("message", "op", "dates", "shifts", "lang"),
    [
        (
            "Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai",
            "mark_unavailable",
            ["2026-11-03"],
            ["M"],
            "hinglish",
        ),
        (
            "Can't come in for my night shift tonight, running a temperature",
            "mark_unavailable",
            ["2026-11-03"],
            ["N"],
            "en",
        ),
        ("कल रात की ड्यूटी नहीं कर पाऊँगी", "mark_unavailable", ["2026-11-04"], ["N"], "hi"),
        ("Kal mujhe off chahiye", "request_day_off", ["2026-11-04"], None, "hinglish"),
        (
            "I need leave on 20 nov for my sister's wedding",
            "request_leave",
            ["2026-11-20"],
            None,
            "en",
        ),
        ("agle friday night duty nahi please", "avoid_shift", ["2026-11-06"], ["N"], "hinglish"),
    ],
)
def test_mock_intake_parses(
    message: str, op: str, dates: list[str], shifts: list[str] | None, lang: str
) -> None:
    out = ConstraintEdit.model_validate(mock_intake(message, CTX))
    assert out.intent == "edit" and not out.needs_clarification and out.language == lang
    (edit,) = out.edits
    assert edit.op == EditOp(op) and [d.isoformat() for d in edit.dates] == dates
    assert (None if edit.shifts is None else [s.value for s in edit.shifts]) == shifts
    assert edit.evidence_span in message


def test_mock_intake_clarifies_refuses_and_routes_questions() -> None:
    vague = ConstraintEdit.model_validate(mock_intake("I can't come", CTX))
    assert vague.needs_clarification and vague.clarification and not vague.edits
    med = ConstraintEdit.model_validate(mock_intake("what medicine should I take for fever?", CTX))
    assert med.intent == "out_of_scope" and not med.edits
    q = ConstraintEdit.model_validate(mock_intake("mujhe har weekend kyun milta hai?", CTX))
    assert q.intent == "question" and q.question


def test_mock_reads_context_and_pinned_fixtures(tmp_path: Path) -> None:
    r = req(f"<context>{json.dumps(CTX)}</context>\naaj subah ki duty nahi kar paungi")
    db = tmp_path / "a.db"
    out = run(gateway(db, FakeProvider(), mode="mock").complete(r))
    assert out.parsed is None and json.loads(out.text or "")["edits"][0]["dates"] == ["2026-11-03"]
    key = cache.cache_key("mock", "mock-rule-based", r)
    fixtures = tmp_path / "mock"
    fixtures.mkdir()
    (fixtures / "malformed.json").write_text(
        json.dumps({"cache_key": key, "response": {"text": "{not json"}})
    )
    pinned = run(gateway(db, FakeProvider(), mode="mock", mock=MockProvider(fixtures)).complete(r))
    assert pinned.text == "{not json"
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0  # mock never caches
    conn.close()


# ---------------------------------------------------------------- schema migration


def test_migrate_adds_reasoning_columns_to_an_old_llm_call_table(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE llm_call(id TEXT PRIMARY KEY, ts TEXT, role TEXT, provider TEXT, "
        "model TEXT, cache_key TEXT, cache_hit INT, prompt_tokens INT, "
        "completion_tokens INT, cost_usd REAL, latency_ms INT, ok INT, error TEXT, "
        "run_id TEXT, request_id TEXT)"
    )
    conn.close()
    conn = open_db(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_call)")}
    assert {"reasoning", "reasoning_tokens"} <= cols
