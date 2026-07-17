"""Brain foundation for survey-agent: multi-tier memory + autonomous digest.

Public API:

    from survey_agent import brain

    brain.record(site, event_type, context, outcome=None, session_id=None)
    brain.query_recent(site, event_type=None, limit=10)
    brain.query_rules(site=None, context_hash=None)

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from survey_agent.brain.normalize import hash_context, normalize_site
from survey_agent.brain.promotion import should_promote
from survey_agent.brain.query import (
    _BrainState,  # noqa: F401  (re-exported for tests)
    recent,
    rules,
)
from survey_agent.brain.schemas import EventRecord

logger = logging.getLogger(__name__)

__all__ = ["record", "query_recent", "query_rules", "EventRecord"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:12]}"


def record(
    site: str | None,
    event_type: str | None,
    context: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    """Emit a brain event. Never raises (spec §4.4)."""
    try:
        if site is None or event_type is None:
            raise ValueError("site and event_type are required")
        event = EventRecord(
            event_id=str(uuid.uuid4()),
            site=normalize_site(site),
            event_type=event_type,
            context_hash=hash_context(context or {}),
            context=context or {},
            outcome=outcome or {},
            session_id=session_id or _new_session_id(),
            created_at=_now_iso(),
        )
        store = _BrainState.store()
        store.append(event)
        if should_promote(event, store):
            store.mark_promotion(event)
    except Exception:
        logger.exception("brain.record: swallowed exception per spec §4.4")


# Aliases for spec §4.4 API names
query_recent = recent
query_rules = rules
