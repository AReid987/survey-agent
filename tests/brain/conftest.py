"""Shared fixtures for brain tests."""

from __future__ import annotations

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
