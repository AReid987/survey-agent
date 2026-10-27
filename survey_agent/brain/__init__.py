"""Brain foundation for survey-agent: multi-tier memory + autonomous digest.

Public API:

    from survey_agent import brain

    brain.record(site, event_type, context, outcome=None, session_id=None)
    brain.query_recent(site, event_type=None, limit=10)
    brain.query_rules(site=None, context_hash=None)

    # Ask-once escalation protocol (docs/adr/0002)
    brain.lookup(question, options=None)      -> BrainResponse | None
    brain.absorb(question, answer, site, ...) -> BrainResponse
    brain.known_answers(limit=200)            -> list[BrainResponse]

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from survey_agent.brain.normalize import hash_context, hash_question, normalize_site
from survey_agent.brain.promotion import should_promote
from survey_agent.brain.query import (
    _BrainState,  # noqa: F401  (re-exported for tests)
    recent,
    rules,
)
from survey_agent.brain.schemas import BrainResponse, EventRecord

logger = logging.getLogger(__name__)

__all__ = [
    "record",
    "query_recent",
    "query_rules",
    "EventRecord",
    "BrainResponse",
    "absorb",
    "lookup",
    "known_answers",
]


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


# ── Ask-once escalation protocol (docs/adr/0002) ────────────────────────


def lookup(question: str, options: list[str] | None = None) -> BrainResponse | None:
    """Return a stored owner answer for this question, or None if never asked.

    Call this BEFORE escalating to the owner. A hit means the owner has already
    answered this exact question and must not be texted again; the hit is
    counted so recurrence is visible in ``brain.known_answers()``.

    Read failures return None rather than raising: the caller then escalates,
    which is the safe direction (an extra text beats a wrong answer).
    """
    try:
        qhash = hash_question(question, options)
        store = _BrainState.store()
        found = store.find_response(qhash)
        if found is not None:
            store.mark_response_used(qhash, _now_iso())
        return found
    except Exception:
        logger.exception("brain.lookup failed — caller should escalate")
        return None


def absorb(
    question: str,
    answer: str,
    site: str,
    options: list[str] | None = None,
    source: str = "owner_text",
) -> BrainResponse:
    """Store an owner-supplied answer so this question is never asked again.

    This is the write half of the ask-once rule. Unlike ``record``, it does NOT
    swallow exceptions: a save that fails silently would re-text the owner for a
    question they already answered, which is the behaviour docs/adr/0002 exists
    to eliminate. Callers should surface the failure to the owner instead.

    Re-absorbing a known question replaces the answer (treated as a correction)
    while preserving its recurrence count.
    """
    if not question.strip():
        raise ValueError("question must not be empty")
    if not answer.strip():
        raise ValueError("answer must not be empty")

    response = BrainResponse(
        response_id=str(uuid.uuid4()),
        question_hash=hash_question(question, options),
        question_text=question.strip(),
        options=list(options or []),
        answer=answer.strip(),
        site=normalize_site(site),
        source=source,  # type: ignore[arg-type]  # validated by pydantic Literal
        created_at=_now_iso(),
    )
    store = _BrainState.store()
    is_new = store.save_response(response)
    logger.info(
        "brain.absorb: %s answer for %r (hash %s)",
        "stored new" if is_new else "corrected existing",
        response.question_text[:60],
        response.question_hash[:12],
    )
    return response


def known_answers(limit: int = 200) -> list[BrainResponse]:
    """Every question the owner has answered, newest first."""
    try:
        return _BrainState.store().all_responses(limit)
    except Exception:
        logger.exception("brain.known_answers failed")
        return []


# Aliases for spec §4.4 API names
query_recent = recent
query_rules = rules
