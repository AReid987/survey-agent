"""Verify orchestrator + site adapters call brain.record() at the right points."""

import json
from unittest.mock import patch

from survey_agent import brain
from survey_agent.brain.schemas import EventRecord
from survey_agent.brain.store import FailureStore
from survey_agent.config import BrainConfig


def test_orchestrator_records_disqualification(tmp_data_dir, brain_dir):  # noqa: ARG001
    store = FailureStore(brain_dir)

    with patch("survey_agent.brain.query._BrainState.store", return_value=store):
        brain.record(
            site="branded_surveys",
            event_type="disqualified",
            context={"survey_id": "abc-123", "reason_keyword": "not a match"},
            session_id="test-orchestrator-1",
        )
        rows = brain.query_recent(site="branded_surveys", event_type="disqualified", limit=10)
        assert len(rows) == 1
        assert rows[0].context["survey_id"] == "abc-123"


def test_captcha_solver_records_attempt(tmp_data_dir, brain_dir):  # noqa: ARG001
    store = FailureStore(brain_dir)
    with patch("survey_agent.brain.query._BrainState.store", return_value=store):
        brain.record(
            site="branded_surveys",
            event_type="captcha_failed",
            context={"challenge_type": "iframe_grid", "iframe_url": "https://x.example/c"},
            outcome={"solver": "vision_llm", "error": "timeout"},
            session_id="test-captcha-1",
        )
        rows = brain.query_recent(site="branded_surveys", event_type="captcha_failed", limit=10)
        assert len(rows) == 1


def test_session_start_triggers_digest(tmp_data_dir, brain_dir, monkeypatch):
    """Verify trigger_if_due fires when conditions are met."""
    (tmp_data_dir / "profile.json").write_text(json.dumps({"shopping": {}}))

    store = FailureStore(brain_dir)
    for i in range(3):
        store.append(
            EventRecord(
                event_id=f"7f3a0000-0000-4000-8000-00000000000{i}",
                site="branded_surveys",
                event_type="captcha_failed",
                context_hash="a" * 64,
                session_id=f"prev-{i}",
                created_at="2026-07-16T14:30:22+00:00",
            )
        )
    store.mark_promotion(store.recent(site="branded_surveys", event_type="captcha_failed", limit=1)[0])

    from survey_agent.brain import digest as digest_mod
    from survey_agent.brain.autonomy import trigger_if_due

    monkeypatch.setattr(
        digest_mod,
        "call_llm",
        lambda _prompt: {
            "new_rules": [],
            "persona_hints": [],
            "rationale": "test",
        },
    )

    cfg = BrainConfig(interval_hours=0, events_per_digest=1000000)
    rid = trigger_if_due(store, cfg, tmp_data_dir)
    assert rid is not None
    assert (brain_dir / "digest_runs.jsonl").exists()
