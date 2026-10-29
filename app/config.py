"""Runtime configuration (design doc section 3.5), read from the environment and `.env`."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

LLMMode = Literal["live", "replay", "mock"]
ProviderName = Literal["baseten", "meta", "modal"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_mode: LLMMode = "live"
    llm_provider_default: ProviderName = "baseten"
    llm_provider_intake: ProviderName | None = None
    llm_provider_explain: ProviderName | None = None
    llm_provider_message: ProviderName | None = None
    llm_provider_vision: ProviderName | None = None

    baseten_api_key: str | None = None
    baseten_base_url: str = "https://inference.baseten.co/v1"
    baseten_model: str = "meta-models/muse-spark-open-1026"

    meta_api_key: str | None = None
    meta_base_url: str | None = None
    meta_model: str | None = None

    modal_api_key: str | None = None
    modal_base_url: str | None = None
    modal_model: str | None = None

    llm_spend_cap_usd: float = 5.00
    db_path: str = "data/shiftsense.db"
    seed: Literal["default", "mini", "real_a"] = "default"  # seed loaded into an empty DB
    ward_tz: str = "Asia/Kolkata"
    demo_now: str | None = None
    solver_workers: int = 8
    solver_stage_seconds: float = 5.0
    solver_total_seconds: float = 9.0  # cap on one full solve, so a draft stays under 10 s (G1)
    log_dir: str = "logs"  # per-stage CP-SAT search logs: logs/{run_id}/{tier}.solver.log
    explain_denied: bool = True  # after each solve, find why each denied request was denied (M8)
    agent_enabled: bool = True  # the roster agent runs sick calls, conflicts, ... (7.10)

    def model_for(self, provider: ProviderName) -> str | None:
        return {
            "baseten": self.baseten_model,
            "meta": self.meta_model,
            "modal": self.modal_model,
        }[provider]


@lru_cache
def get_settings() -> Settings:
    return Settings()
