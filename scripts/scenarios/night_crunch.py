"""Night crunch (design doc M9): make the nurses who could still work tonight's night unavailable,
so the re-solve cannot meet the night's minimum cover and the planner opens a conflict.

    uv run python -m scripts.scenarios.night_crunch [--now 2026-11-03T22:00:00+05:30]

Works on the app database (DB_PATH) with its published roster. "Now" defaults to 22:00 on the
DEMO_NOW day (else today): the Morning and Evening shifts are under way, so only nurses already on
tonight's night or off today can cover it. On the mini ward (SEED=mini) that means marking three
night-qualified nurses unavailable (one is the standby nurse already off sick). Then reload the
charge view: the Alerts tab shows the conflict with its options.
"""

import argparse
import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.domain.models import Request, RequestKind, RequestStatus
from app.explain.narrate import narrate
from app.intake.dates import ward_now
from app.llm.gateway import get_gateway
from app.planner.build_input import day_index
from app.planner.runs import resolve_input, start_resolve
from app.planner.sickcall import rank
from app.solver.rules import rule_check
from app.store import repo
from app.store.db import open_db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--now", help="ISO datetime; default 22:00 on the demo day")
    a = ap.parse_args(argv)
    s = get_settings()
    conn = open_db(s.db_path)
    ward = repo.get_ward(conn)
    if ward is None or repo.get_published_run(conn, ward.id) is None:
        print("no ward with a published roster; run `make seed` first")
        return 2
    zone = ZoneInfo(ward.tz)
    now = (
        datetime.fromisoformat(a.now)
        if a.now
        else datetime.combine(ward_now(ward.tz, s.demo_now).date(), time(22, 0), tzinfo=zone)
    )
    tonight = now.date()
    d = day_index(ward, tonight)
    _ward, inp, _pub, _active, _now = resolve_input(conn, ward.id, affected=[tonight], now=now)
    prev = inp.previous or {}
    away = {(n, dd) for n, dd, sh in inp.unavailable if sh in (None, "N")}
    pool = [
        n.id for n in inp.nurses
        if prev.get((n.id, d)) in (None, "N") and (n.id, d) not in away
        and not rule_check(prev, inp, n.id, d, "N", nurse_only=True)
    ]  # fmt: skip
    pool.sort(key=lambda n: (prev.get((n, d)) != "N", n))  # those on tonight's night first
    need = len(pool) - inp.demand[d, "N"].min_total + 1
    if need <= 0:
        print("tonight's night is already short; nothing to do")
        return 1
    chosen = pool[:need]
    already = sorted(n for n, dd in away if dd == d and inp.demand.get((d, "N")) is not None)
    for n in chosen:
        r = Request(id=repo.next_request_id(conn), nurse_id=n, kind=RequestKind.UNAVAILABLE,
                    dates=[tonight], shifts=["N"], hard=True, status=RequestStatus.ACTIVE,
                    created_at=now)  # fmt: skip
        repo.insert_request(conn, ward.id, r)
    print(f"{now:%a %d %b %H:%M}: night of {tonight} needs {inp.demand[d, 'N'].min_total}; "
          f"could be covered by {pool}; already unavailable: {already}; "
          f"now unavailable: {chosen}")  # fmt: skip
    plan = start_resolve(conn, ward.id, trigger="night_crunch", affected=[tonight], now=now)
    out = rank(conn, plan)
    print(f"run {out.run_id}: {out.status.value}")
    if out.conflict_id:
        c = repo.get_conflict(conn, out.conflict_id)
        assert c is not None
        print(f"conflict {out.conflict_id}: core {c['core']['names']}")
        for o in c["options"]:
            print(f"  {o['option_id']}: {o['label']}")
        n = asyncio.run(narrate(conn, out.conflict_id, gateway=get_gateway()))
        print(f"narrative ({n['source']}): {n['summary']}")
        print("reload the charge view; the Alerts tab shows it")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
