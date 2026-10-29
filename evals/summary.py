"""One eval table from the latest live report of each suite and provider (design doc M13).

    uv run python -m evals.summary [--readme]

Reads `evals/reports/*-{suite}-{provider}-live.json` and the sick-call statistics, writes
`evals/reports/summary.md`, and with `--readme` replaces the block between the `eval-table`
markers in README.md. A provider with no report yet shows as pending.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "evals" / "reports"
README = ROOT / "README.md"
SUITES = {  # suite -> (what it checks, the role whose calls it times)
    "intake": ("50 messages → ConstraintEdit", "intake"),
    "explain": ("15 why-questions, claim-checked", "explain"),
    "writer": ("20 replacement asks", "message"),
    "agent": ("10 roster-agent workflows", "agent"),
    "vision": ("5 roster sheets, per cell", "vision"),
}
PROVIDERS = ("baseten", "meta")
NAMES = {"baseten": "Muse Spark Open (Baseten)", "meta": "Muse Spark (Meta Model API)"}
START, END = "<!-- eval-table -->", "<!-- /eval-table -->"


def latest(suite: str, provider: str) -> dict[str, Any] | None:
    files = sorted(REPORTS.glob(f"*-{suite}-{provider}-live.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def headline(suite: str, s: dict[str, Any]) -> str:
    if suite == "intake":
        return (f"{s['first_pass_accuracy']:.0f}% first pass, {s['accuracy_after_repair']:.0f}% "
                f"after repair")  # fmt: skip
    if suite == "explain":
        return f"{s['pass_rate']:.0f}% pass, {s['grounding_rate']:.0f}% grounded first time"
    if suite == "writer":
        return f"{s['final_pass']} pass, {s['model_draft_accepted']} model drafts kept"
    if suite == "agent":
        return (f"{s['agent_success']} done by the agent (fallback {s['fallback_success']}), "
                f"{s['agent_forbidden_attempts']} forbidden attempts")  # fmt: skip
    if suite == "vision":
        return f"{s['cell_accuracy']} of cells, {s['photos_passed']} sheets"
    return ""


def per_call(s: dict[str, Any], role: str) -> tuple[str, str]:
    p50, p95 = s.get(f"{role}_latency_p50_ms"), s.get(f"{role}_latency_p95_ms")
    cost = s.get(f"{role}_cost_usd_per_call")
    lat = f"{p50 / 1000:.1f} / {p95 / 1000:.1f} s" if p50 is not None and p95 is not None else "n/a"
    return lat, (f"${cost:.4f}" if cost is not None else "n/a")


def table() -> str:
    lines = [
        "| Suite | Provider | Result | Latency per call (p50 / p95) | Cost per call | Report |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for suite, (what, role) in SUITES.items():
        for prov in PROVIDERS:
            r = latest(suite, prov)
            if r is None:
                lines.append(f"| {suite}: {what} | {NAMES[prov]} | pending (no key yet) | | | |")
                continue
            s = r["summary"]
            lat, cost = per_call(s, role)
            ref = f"`{r['date']}-{suite}-{prov}-live`"
            lines.append(f"| {suite}: {what} | {NAMES[prov]} | {headline(suite, s)} | {lat} | "
                         f"{cost} | {ref} |")  # fmt: skip
    stats = sorted(REPORTS.glob("*-sickcall-stats.json"))
    if stats:
        s = json.loads(stats[-1].read_text(encoding="utf-8"))["summary"]
        lines += [
            "",
            f"Sick calls (solver only, no model; {s['sick_calls']} random sick calls on the "
            f"{s['ward']} ward, `{stats[-1].stem}`): {s['solved']} re-solved, with every tier "
            f"proven optimal in {s['proven_rate']} of them, {s['solve_secs_p50']} s median and "
            f"{s['solve_secs_max']} s at most. Changed cells besides the sick nurse's own: median "
            f"{s['changed_cells_with_stability_median']:g} (max "
            f"{s['changed_cells_with_stability_max']}) with the stability tier, median "
            f"{s['changed_cells_without_stability_median']:g} (max "
            f"{s['changed_cells_without_stability_max']}) without it.",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--readme", action="store_true", help="update the README eval table")
    a = ap.parse_args(argv)
    body = table()
    (REPORTS / "summary.md").write_text("# Eval summary\n\n" + body, encoding="utf-8")
    print(body)
    if a.readme:
        text = README.read_text(encoding="utf-8")
        block = f"{START}\n{body}{END}"
        new, n = re.subn(
            re.escape(START) + r".*?" + re.escape(END), lambda _: block, text, flags=re.S
        )
        if n != 1:
            raise SystemExit("README has no eval-table markers")
        README.write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
