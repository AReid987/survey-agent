"""Shared fixtures for brain tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def brain_dir(tmp_data_dir: Path) -> Path:
    """Create data/brain inside tmp_data_dir and return it."""
    (tmp_data_dir / "brain").mkdir()
    return tmp_data_dir / "brain"


@pytest.fixture(autouse=True)
def _reset_brain_state(tmp_data_dir: Path):  # noqa: ARG001
    """Clear the module-level _BrainState singleton between tests AND
    ensure DATA_DIR is sandboxed via `tmp_data_dir`.

    The `tmp_data_dir` argument is unused but required so this autouse
    fixture pulls in the upstream `tmp_data_dir` fixture, which patches
    `DATA_DIR` before any brain code touches it.
    """
    from survey_agent.brain import query as query_mod

    query_mod._BrainState._store = None
    query_mod._BrainState._data_dir = None
    yield
    query_mod._BrainState._store = None
    query_mod._BrainState._data_dir = None


@pytest.fixture()
def fake_llm(monkeypatch):
    """Patch survey_agent.brain.digest.call_llm to return a canned response.

    The fixture returns a `_set` callable that lets each test pick its
    canned payload. Default payload returns no rules and no hints.
    """
    from survey_agent.brain import digest as digest_mod

    state = {"payload": {"new_rules": [], "persona_hints": [], "rationale": "no-op"}}

    def _set(payload: dict) -> None:
        state["payload"] = payload

    def _call(prompt: str) -> dict:  # noqa: ARG001
        return dict(state["payload"])

    monkeypatch.setattr(digest_mod, "call_llm", _call)
    return _set


@pytest.fixture()
def brain_dir_with_profile(brain_dir, tmp_data_dir):
    """Pre-populate brain_dir + profile.json + empty rules.yaml."""
    profile = {
        "demographics": {"age": 39, "gender": "Male"},
        "shopping": {"preferred_stores": ["Amazon"], "decision_factors": ["Price"]},
    }
    (tmp_data_dir / "profile.json").write_text(json.dumps(profile))
    (brain_dir / "rules.yaml").write_text("[]\n")
    return brain_dir
