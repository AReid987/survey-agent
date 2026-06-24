"""Shared fixtures for the survey-agent test suite."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect DATA_DIR to a temp directory so tests don't touch real data."""
    monkeypatch.setattr("survey_agent.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("survey_agent.config.CONFIG_PATH", tmp_path / "config.yaml")
    return tmp_path


@pytest.fixture()
def mock_keyring(monkeypatch: pytest.MonkeyPatch):
    """Mock keyring so credential tests don't touch the real keychain."""
    store: dict[str, dict[str, str]] = {}

    def _set_password(service: str, username: str, password: str) -> None:
        store.setdefault(service, {})[username] = password

    def _get_password(service: str, username: str) -> str | None:
        return store.get(service, {}).get(username)

    def _delete_password(service: str, username: str) -> None:
        if service in store:
            store[service].pop(username, None)

    mock = MagicMock()
    mock.set_password = MagicMock(side_effect=_set_password)
    mock.get_password = MagicMock(side_effect=_get_password)
    mock.delete_password = MagicMock(side_effect=_delete_password)
    mock.errors = MagicMock()
    mock.errors.PasswordDeleteError = type("PasswordDeleteError", (Exception,), {})

    monkeypatch.setattr("survey_agent.credentials.keyring", mock)
    return mock


@pytest.fixture()
def sample_config_yaml(tmp_path: Path) -> Path:
    """Write a minimal config YAML and return its path."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
max_concurrent_sessions: 2
schedule:
  wake_time: "09:00"
  sleep_time: "22:00"
account:
  google_email: test@example.com
llm_provider: gemini
"""
    )
    return config_path
