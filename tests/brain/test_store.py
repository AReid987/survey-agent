"""Tests for FailureStore — JSONL append + SQLite WAL mirror."""

from __future__ import annotations

import json

from survey_agent.brain.schemas import EventRecord
from survey_agent.brain.store import FailureStore


def _event(
    site: str = "branded_surveys",
    event_type: str = "captcha_failed",
    context_hash: str = "a" * 64,
    session_id: str = "sess-1",
    event_id: str = "7f3a0000-0000-4000-8000-000000000001",
) -> EventRecord:
    return EventRecord(
        event_id=event_id,
        site=site,
        event_type=event_type,
        context_hash=context_hash,
        context={"challenge_type": "iframe_grid"},
        outcome={"solver": "vision_llm"},
        session_id=session_id,
        created_at="2026-07-16T14:30:22+00:00",
    )


def test_append_writes_to_jsonl_and_sqlite(brain_dir):
    store = FailureStore(brain_dir)
    store.append(_event())

    jsonl = (brain_dir / "events.jsonl").read_text().strip().splitlines()
    assert len(jsonl) == 1
    parsed = json.loads(jsonl[0])
    assert parsed["site"] == "branded_surveys"

    rows = store.recent(site="branded_surveys", event_type=None, limit=10)
    assert len(rows) == 1
    assert rows[0].event_type == "captcha_failed"


def test_append_multiple_events_are_all_queryable(brain_dir):
    store = FailureStore(brain_dir)
    for i in range(5):
        store.append(_event(event_id=f"7f3a0000-0000-4000-8000-00000000000{i}"))

    rows = store.recent(site="branded_surveys", event_type="captcha_failed", limit=10)
    assert len(rows) == 5


def test_recent_filters_by_site_and_event_type(brain_dir):
    store = FailureStore(brain_dir)
    store.append(_event(event_type="captcha_failed"))
    store.append(
        _event(
            event_type="disqualified",
            event_id="7f3a0000-0000-4000-8000-0000000000ff",
        )
    )

    rows = store.recent(site="branded_surveys", event_type="captcha_failed", limit=10)
    assert len(rows) == 1
    assert rows[0].event_type == "captcha_failed"


def test_recent_returns_most_recent_first(brain_dir):
    store = FailureStore(brain_dir)
    store.append(_event(event_id="7f3a0000-0000-4000-8000-000000000001", session_id="a"))
    store.append(_event(event_id="7f3a0000-0000-4000-8000-000000000002", session_id="b"))

    rows = store.recent(site="branded_surveys", event_type="captcha_failed", limit=10)
    assert rows[0].session_id == "b"  # newest first
