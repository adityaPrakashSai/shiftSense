"""Token prices from prices.toml (USD per million tokens)."""

import tomllib
from functools import lru_cache
from pathlib import Path

PRICES = Path(__file__).with_name("prices.toml")


@lru_cache
def _table() -> dict[str, dict[str, object]]:
    return tomllib.loads(PRICES.read_text(encoding="utf-8"))


def price(provider: str, model: str) -> tuple[float, float]:
    t = _table()
    entry = t.get(provider, {}).get(model) if isinstance(t.get(provider), dict) else None
    if not isinstance(entry, dict):
        entry = t["default"]
    return float(entry["input"]), float(entry["output"])  # type: ignore[arg-type]


def cost_usd(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p_in, p_out = price(provider, model)
    return (prompt_tokens * p_in + completion_tokens * p_out) / 1_000_000
