"""Global configuration for the survey agent."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.yaml"


class ScheduleConfig(BaseModel):
    """Scheduler / heartbeat configuration."""

    wake_time: str = "08:00"
    sleep_time: str = "23:00"
    min_session_minutes: int = 15
    max_session_minutes: int = 45
    min_break_minutes: int = 5
    max_break_minutes: int = 15
    lunch_start: str = "12:00"
    lunch_duration_minutes: int = 30
    heartbeat_interval_seconds: int = 30


class AccountConfig(BaseModel):
    """Account configuration."""

    google_email: str = ""
    paypal_email: str = ""


class BrainConfig(BaseModel):
    """Brain foundation configuration (spec §4.3)."""

    interval_hours: int = 6
    events_per_digest: int = 200
    max_per_day: int = 4
    promotion_threshold: int = 3


class AppConfig(BaseModel):
    """Root application configuration."""

    max_concurrent_sessions: int = 1
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)

    # LLM provider settings (default for all backends)
    llm_provider: str = "gemini"  # gemini, groq, mistral, openai, openai-compatible, cerebras
    llm_model: str = ""  # uses provider default if empty
    llm_api_key: str = ""  # or set provider-specific env var
    llm_base_url: str = ""  # for openai-compatible providers

    # Browser backend: "browser-use" (LLM+Playwright) or "browser-harness" (real Chrome)
    browser_backend: str = "browser-use"

    # Override LLM for browser-harness (falls back to global llm_* if empty)
    harness_llm_provider: str = ""
    harness_llm_model: str = ""
    harness_llm_api_key: str = ""
    harness_llm_base_url: str = ""

    # Brain foundation (autonomous digest triggers)
    brain: BrainConfig = Field(default_factory=BrainConfig)

    def harness_llm_settings(self) -> dict:
        """Return harness LLM settings, falling back to global.

        Note: llm_base_url only falls back if the provider is openai-compatible.
        For named providers (groq, cerebras, mistral), the base URL is set by
        the provider config and should NOT inherit from the global setting.
        """
        provider = self.harness_llm_provider or self.llm_provider
        base_url = self.harness_llm_base_url
        if not base_url and provider == "openai-compatible":
            base_url = self.llm_base_url
        return {
            "llm_provider": provider,
            "llm_model": self.harness_llm_model or self.llm_model or None,
            "llm_api_key": self.harness_llm_api_key or self.llm_api_key or None,
            "llm_base_url": base_url or None,
        }


def load_config() -> AppConfig:
    """Load config from disk, creating defaults if missing."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        return AppConfig(**data)
    config = AppConfig()
    save_config(config)
    return config


def save_config(config: AppConfig) -> None:
    """Persist config to disk."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False)
