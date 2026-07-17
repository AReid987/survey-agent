"""Shared fixtures for brain tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def brain_dir(tmp_data_dir: Path) -> Path:
    """Create data/brain inside tmp_data_dir and return it."""
    (tmp_data_dir / "brain").mkdir()
    return tmp_data_dir / "brain"
