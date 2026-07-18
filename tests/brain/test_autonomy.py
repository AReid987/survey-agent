"""Tests for autonomy triggers (time / volume / promotion)."""

from __future__ import annotations

import json
from datetime import UTC

from survey_agent.brain import autonomy
from survey_agent.brain import digest as digest_mod
from survey_agent.brain.schemas import EventRecord
from survey_agent.brain.store import FailureStore
from survey_agent.config import BrainConfig


def _bootstrap_promotions(store: FailureStore) -> None:
    for i in range(3):
        store.append(
            EventRecord(
                event_id=f"7f3a0000-0000-4000-8000-00000000000{i}",
                site="branded_surveys",
                event_type="captcha_failed",
                context_hash="a" * 64,
                session_id=f"sess-{i}",
                created_at="2026-07-16T14:30:22+00:00",
            )
        )
    last = EventRecord(
        event_id="7f3a0000-0000-4000-8000-0000000000ff",
        site="branded_surveys",
        event_type="captcha_failed",
        context_hash="a" * 64,
        session_id="sess-final",
        created_at="2026-07-16T14:30:22+00:00",
    )
    store.append(last)
    store.mark_promotion(last)


def test_promotion_trigger_fires(brain_dir, tmp_data_dir):
    (tmp_data_dir / "profile.json").write_text(json.dumps({"shopping": {}}))
    store = FailureStore(brain_dir)
    _bootstrap_promotions(store)

    cfg = BrainConfig()
    trigger = autonomy.should_trigger_digest(store, cfg, tmp_data_dir)
    assert trigger == "promotion"


def test_no_trigger_when_no_signal(brain_dir, tmp_data_dir):
    store = FailureStore(brain_dir)
    cfg = BrainConfig()
    trigger = autonomy.should_trigger_digest(store, cfg, tmp_data_dir)
    assert trigger is None


def test_recent_failure_backs_off(brain_dir, tmp_data_dir):
    (tmp_data_dir / "profile.json").write_text(json.dumps({"shopping": {}}))
    store = FailureStore(brain_dir)
    cfg = BrainConfig()
    _bootstrap_promotions(store)

    # Use a started_at that's within the 5-minute failure backoff window
    from datetime import datetime

    recent_started = datetime.now(UTC).replace(microsecond=0).isoformat()

    (brain_dir / "digest_runs.jsonl").write_text(
        json.dumps(
            {
                "run_id": "test-fail",
                "trigger": "manual",
                "started_at": recent_started,
                "finished_at": recent_started,
                "status": "failed",
            }
        )
        + "\n"
    )
    (brain_dir / "last_digest.txt").write_text(recent_started)
    trigger = autonomy.should_trigger_digest(store, cfg, tmp_data_dir)
    # Promotion exists but recent failure => backoff
    assert trigger is None


def test_trigger_if_due_runs_digest(brain_dir, tmp_data_dir, monkeypatch):
    (tmp_data_dir / "profile.json").write_text(json.dumps({"shopping": {}}))
    store = FailureStore(brain_dir)
    cfg = BrainConfig()
    _bootstrap_promotions(store)

    calls = {"count": 0}

    def _spy(*args, **kwargs):  # noqa: ARG001
        calls["count"] += 1
        return "spy-run-id"

    monkeypatch.setattr(digest_mod, "run_digest", _spy)
    result = autonomy.trigger_if_due(store, cfg, tmp_data_dir)
    assert calls["count"] == 1
    assert result == "spy-run-id"


def test_trigger_if_due_no_trigger_skips(monkeypatch, brain_dir, tmp_data_dir):
    (tmp_data_dir / "profile.json").write_text(json.dumps({"shopping": {}}))
    store = FailureStore(brain_dir)
    cfg = BrainConfig()
    calls = {"count": 0}

    def _spy(*args, **kwargs):  # noqa: ARG001
        calls["count"] += 1
        return "should-not-run"

    monkeypatch.setattr(digest_mod, "run_digest", _spy)
    # No promotions, no recent events, last_digest from far past but
    # we'd need to be careful with max_per_day cap; here we use interval=0
    # so time trigger fires — but promotions absent AND events count below
    # threshold AND last_digest == None => no trigger
    result = autonomy.trigger_if_due(store, cfg, tmp_data_dir)
    assert result is None
    assert calls["count"] == 0
