"""Integration tests for digest<->call_llm indirection."""

from __future__ import annotations

import json

from survey_agent.brain import digest as digest_mod
from survey_agent.brain.schemas import EventRecord
from survey_agent.brain.store import FailureStore


def test_call_llm_uses_injected_provider(brain_dir, tmp_data_dir):
    """call_llm must be monkeypatchable; this verifies the indirection holds."""
    original = getattr(digest_mod, "call_llm", None)
    seen = {"prompt": None}

    def _capture(prompt: str) -> dict:
        seen["prompt"] = prompt
        return {"new_rules": [], "persona_hints": [], "rationale": "captured"}

    digest_mod.call_llm = _capture  # type: ignore[assignment]
    try:
        (tmp_data_dir / "profile.json").write_text(json.dumps({"shopping": {}}))
        store = FailureStore(brain_dir)
        store.append(EventRecord(
            event_id="7f3a0000-0000-4000-8000-000000000001",
            site="branded_surveys",
            event_type="captcha_failed",
            context_hash="a" * 64,
            session_id="sess-1",
            created_at="2026-07-16T14:30:22+00:00",
        ))
        store.append(EventRecord(
            event_id="7f3a0000-0000-4000-8000-000000000002",
            site="branded_surveys",
            event_type="captcha_failed",
            context_hash="a" * 64,
            session_id="sess-2",
            created_at="2026-07-16T14:30:22+00:00",
        ))
        store.append(EventRecord(
            event_id="7f3a0000-0000-4000-8000-000000000003",
            site="branded_surveys",
            event_type="captcha_failed",
            context_hash="a" * 64,
            session_id="sess-3",
            created_at="2026-07-16T14:30:22+00:00",
        ))
        digest_mod.run_digest("test_capture", store, None, tmp_data_dir)
        assert seen["prompt"] is not None
        assert "PROMOTIONS" in seen["prompt"]
    finally:
        digest_mod.call_llm = original  # type: ignore[assignment]
