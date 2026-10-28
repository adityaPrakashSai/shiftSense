"""Infeasibility diagnosis and conflict options (design doc 5.5, 7.6, 11.3)."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.config import get_settings
from app.domain.models import RunStatus
from app.explain.narrate import Narrative, narrate, validate
from app.llm.gateway import LLMGateway
from app.log.decision_log import DecisionLog
from app.planner import conflicts
from app.planner.conflicts import WAIVABLE, parse
from app.planner.runs import start_resolve
from app.planner.sickcall import rank
from app.store import repo
from tests.helpers import NIGHT, TUE, mini_baseline, night_crunch


@pytest.fixture
def crunch(conn: sqlite3.Connection) -> str:
    mini_baseline(conn)
    out = rank(conn, night_crunch(conn))
    assert out.status is RunStatus.INFEASIBLE and out.conflict_id
    return out.conflict_id


def test_diagnosis_names_the_short_night_and_the_absences(
    conn: sqlite3.Connection, crunch: str
) -> None:
    c = repo.get_conflict(conn, crunch)
    assert c is not None
    core = c["core"]["names"]
    assert "min_cover:d8:N" in core
    assert {"unavailable:N01:d8:N", "unavailable:S01:d8:N", "unavailable:S02:d8"} <= set(core)
    assert 2 <= len(core) <= 6
    assert all(parse(x)["group"] in WAIVABLE for x in core)
    # the others are explained by rules nobody can waive
    ctx = c["core"]["context"]
    assert ctx and all(r.split(":")[0] not in WAIVABLE for x in ctx for r in x["rules"])
    assert any("frozen:" in r for x in ctx for r in x["rules"])
    events = [e for e in repo.list_events(conn, "ward-mini") if e["kind"] == "run.infeasible"]
    assert events[-1]["payload"]["core"] == core


def test_options_come_from_templates(conn: sqlite3.Connection, crunch: str) -> None:
    c = repo.get_conflict(conn, crunch)
    assert c is not None
    opts = c["options"]
    assert 1 <= len(opts) <= 4 and [o["option_id"] for o in opts] == [f"O{i + 1}" for i in
                                                                      range(len(opts))]  # fmt: skip
    assert opts[0]["label"] == "Ask Anjali Verma to come in on Tue 3 Nov (Night)"
    assert all(o["waive"] in c["core"]["names"] for o in opts if o["kind"] == "waive")
    assert (
        opts[-1]["kind"] == "float" and opts[-1]["label"] == "Add a float nurse for Tue 3 Nov Night"
    )


def test_choosing_a_waiver_gives_a_feasible_proposal(conn: sqlite3.Connection, crunch: str) -> None:
    log = DecisionLog(conn, "ward-mini")
    opt, days = conflicts.apply_option(conn, crunch, "O1", by="charge", log=log)
    assert opt["waive"] == "unavailable:N01:d8:N" and days == [TUE]
    assert repo.active_waivers(conn, "ward-mini") == {"unavailable:N01:d8:N"}
    plan = start_resolve(conn, trigger=f"conflict:{crunch}", affected=days, now=NIGHT)
    assert "unavailable:N01:d8:N" in plan.inp.waived
    out = rank(conn, plan)
    conflicts.link_run(conn, crunch, plan.run_id)
    assert out.status is RunStatus.PROPOSED
    assert repo.get_roster(conn, plan.run_id)["N01"][8] == "N"
    c = repo.get_conflict(conn, crunch)
    assert c is not None and c["chosen_option"] == "O1" and c["resolved_run"] == plan.run_id
    resolved = [e for e in repo.list_events(conn, "ward-mini") if e["kind"] == "conflict.resolved"]
    assert resolved[-1]["payload"]["waived"] == "unavailable:N01:d8:N"
    with pytest.raises(ValueError):
        conflicts.apply_option(conn, crunch, "O2", by="charge", log=log)


def test_float_nurse_covers_the_night(conn: sqlite3.Connection, crunch: str) -> None:
    log = DecisionLog(conn, "ward-mini")
    opt, days = conflicts.apply_option(conn, crunch, "O4", by="charge", log=log)
    assert opt["float_nurse"] == "F01"
    ward = repo.get_ward(conn)
    assert ward is not None and ward.nurses[-1].id == "F01"
    plan = start_resolve(conn, trigger=f"conflict:{crunch}", affected=days, now=NIGHT)
    out = rank(conn, plan)
    assert out.status is RunStatus.PROPOSED
    row = repo.get_roster(conn, plan.run_id)["F01"]
    assert row[8] == "N" and sum(s is not None for s in row) == 1


def test_unknown_option_and_conflict(conn: sqlite3.Connection, crunch: str) -> None:
    log = DecisionLog(conn, "ward-mini")
    with pytest.raises(KeyError):
        conflicts.apply_option(conn, crunch, "O9", by="charge", log=log)
    with pytest.raises(LookupError):
        conflicts.apply_option(conn, "C-9999", "O1", by="charge", log=log)


# ---------------------------------------------------------------- narrator (7.6)


def test_mock_narrative_passes(conn: sqlite3.Connection, crunch: str, db_path: Path) -> None:
    gw = LLMGateway(get_settings().model_copy(update={"llm_mode": "mock", "db_path": str(db_path)}))
    out = asyncio.run(narrate(conn, crunch, gateway=gw))
    assert out["source"] == "model" and not out["problems"]
    c = repo.get_conflict(conn, crunch)
    assert c is not None and c["narrative"]["summary"] == out["summary"]
    assert [o["option_id"] for o in out["options"]] == ["O1", "O2", "O3", "O4"]
    assert repo.list_events(conn, "ward-mini")[-1]["kind"] == "conflict.narrated"


def test_narrative_validator(conn: sqlite3.Connection, crunch: str) -> None:
    c = repo.get_conflict(conn, crunch)
    assert c is not None
    opts = [{"option_id": o["option_id"], "label": o["label"]} for o in c["options"]]
    good = {"summary": "Nobody else can work Tuesday night.", "options": opts}
    assert validate(Narrative.model_validate(good), c) == []
    swapped = {**good, "options": list(reversed(good["options"]))}
    assert validate(Narrative.model_validate(swapped), c)
    added = {**good, "options": [*good["options"], {"option_id": "O5", "label": "x"}]}
    assert validate(Narrative.model_validate(added), c)
    private = {**good, "summary": "Anjali has a fever, so nobody can work Tuesday night."}
    assert any("health" in p for p in validate(Narrative.model_validate(private), c))


def test_bad_narrative_falls_back_to_template(
    conn: sqlite3.Connection, crunch: str, db_path: Path
) -> None:
    from app.llm.types import LLMRequest, ProviderCaps, ProviderResult

    class Fake:
        name, model, caps = "baseten", "fake", ProviderCaps()

        async def complete(self, req: LLMRequest, *, json_protocol: bool) -> ProviderResult:
            body = {"summary": "Rekha is sick.", "options": [{"option_id": "O1", "label": "x"}]}
            return ProviderResult(text=json.dumps(body))

    s = get_settings().model_copy(
        update={"llm_mode": "live", "db_path": str(db_path), "llm_provider_message": None}
    )
    out = asyncio.run(narrate(conn, crunch, gateway=LLMGateway(s, providers={"baseten": Fake()})))
    assert out["source"] == "template" and out["problems"]
    assert out["options"][0]["label"] == "Ask Anjali Verma to come in on Tue 3 Nov (Night)"
    assert "sick" not in out["summary"]


def test_parse_names() -> None:
    assert parse("unavailable:N01:d8:N") == {"group": "unavailable", "nurse": "N01", "day": 8,
                                             "shift": "N"}  # fmt: skip
    assert parse("min_cover:d12:E") == {
        "group": "min_cover",
        "nurse": None,
        "day": 12,
        "shift": "E",
    }
    assert parse("max_shifts:N05")["nurse"] == "N05"
