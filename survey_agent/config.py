"""Global configuration for the survey agent."""

from __future__ import annotations

import os
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


class AppConfig(BaseModel):
    """Root application configuration."""

    max_concurrent_sessions: int = 1
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)

    # LLM provider settings
    llm_provider: str = "gemini"  # gemini, groq, mistral, openai, openai-compatible, cerebras
    llm_model: str = ""  # uses provider default if empty
    llm_api_key: str = ""  # or set provider-specific env var
    llm_base_url: str = ""  # for openai-compatible providers


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
