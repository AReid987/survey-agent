"""Public read paths for the brain.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §4.2,
§4.4.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from survey_agent.brain.schemas import EventRecord, Rule
from survey_agent.brain.store import FailureStore

logger = logging.getLogger(__name__)


class _BrainState:
    """Holds the singleton FailureStore + paths. Lazily initialized."""

    _store: FailureStore | None = None
    _data_dir: Path | None = None

    @classmethod
    def configure(cls, data_dir: Path) -> None:
        cls._data_dir = data_dir
        cls._store = FailureStore(data_dir / "brain")

    @classmethod
    def store(cls) -> FailureStore:
        if cls._store is None:
            from survey_agent.config import DATA_DIR  # noqa: PLC0415

            cls.configure(DATA_DIR)
        assert cls._store is not None
        return cls._store

    @classmethod
    def rules_path(cls) -> Path:
        if cls._data_dir is None:
            from survey_agent.config import DATA_DIR  # noqa: PLC0415
            cls._data_dir = DATA_DIR
        return cls._data_dir / "brain" / "rules.yaml"


def recent(site: str, event_type: str | None = None, limit: int = 10) -> list[EventRecord]:
    return _BrainState.store().recent(site, event_type, limit)


def rules(site: str | None = None, context_hash: str | None = None) -> list[Rule]:
    path = _BrainState.rules_path()
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text()) or []
    except yaml.YAMLError:
        logger.exception("brain: malformed rules.yaml — returning empty")
        return []
    if not isinstance(raw, list):
        return []
    out: list[Rule] = []
    for entry in raw:
        try:
            rule = Rule.model_validate(entry)
        except Exception:
            logger.exception("brain: skipping malformed rule entry")
            continue
        if site is not None and rule.site != site:
            continue
        if (
            context_hash is not None
            and rule.applies_when.get("context_hash") != context_hash
        ):
            continue
        out.append(rule)
    return out
