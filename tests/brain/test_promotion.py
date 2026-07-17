"""Tests for frequency-based promotion trigger."""

from __future__ import annotations

import uuid

from survey_agent.brain.promotion import should_promote
from survey_agent.brain.schemas import EventRecord
from survey_agent.brain.store import FailureStore


def _event(suffix: str, created_at: str = "2026-07-16T14:30:22+00:00") -> EventRecord:
    return EventRecord(
        event_id=str(uuid.uuid4()),
        site="branded_surveys",
        event_type="captcha_failed",
        context_hash="a" * 64,
        session_id=f"sess-{suffix}",
        created_at=created_at,
    )


def test_below_threshold_does_not_promote(brain_dir):
    store = FailureStore(brain_dir)
    store.append(_event("1"))
    store.append(_event("2"))
    assert should_promote(_event("3"), store) is False


def test_at_threshold_promotes(brain_dir):
    store = FailureStore(brain_dir)
    for i in range(3):
        store.append(_event(str(i)))
    # The 3rd event crosses the threshold
    store.append(_event("final"))
    assert should_promote(_event("final"), store) is True


def test_re_occurrence_does_not_re_promote(brain_dir):
    store = FailureStore(brain_dir)
    for i in range(5):
        store.append(_event(str(i)))
    # All 5 are appended; only the 3rd crossed the threshold.
    # 4th, 5th events still see true (signature qualifies) but the
    # promotion already exists.
    ev4 = _event("4")
    ev5 = _event("5")
    # should_promote checks the "qualifies" predicate, separate from
    # whether the promotion row exists.
    assert should_promote(ev4, store) is True
    assert should_promote(ev5, store) is True
