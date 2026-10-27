"""Tests for the brain's public API."""

from __future__ import annotations

from survey_agent import brain


def test_record_is_idempotent_under_repeated_calls():
    brain.record(
        site="branded_surveys",
        event_type="captcha_failed",
        context={"challenge_type": "iframe_grid"},
        session_id="sess-1",
    )
    brain.record(
        site="branded_surveys",
        event_type="captcha_failed",
        context={"challenge_type": "iframe_grid"},
        session_id="sess-2",
    )
    rows = brain.query_recent(site="branded_surveys", event_type="captcha_failed", limit=10)
    assert len(rows) == 2


def test_record_never_raises_even_on_bad_input(caplog):
    # Missing required fields should not crash
    brain.record(site=None, event_type="captcha_failed")  # type: ignore[arg-type]
    # The implementation wraps the whole body in `try/except Exception: log`,
    # so we expect a log record from the `brain` logger with `record` in the
    # message text AND no exception bubbles up to the test runner.
    matching = [r for r in caplog.records if r.name == "survey_agent.brain" and "record" in r.message.lower()]
    assert matching, f"expected a swallowed-log record, got: {caplog.records!r}"


def test_query_recent_returns_empty_on_no_data():
    rows = brain.query_recent(site="branded_surveys", event_type="captcha_failed")
    assert rows == []


def test_query_rules_returns_empty_when_no_yaml():
    rules = brain.query_rules(site="branded_surveys")
    assert rules == []


def test_query_rules_reads_yaml(brain_dir):
    rules_yaml = brain_dir / "rules.yaml"
    rules_yaml.write_text(
        """
- rule_id: 7f3a0000-0000-4000-8000-000000000001
  site: branded_surveys
  applies_when:
    event_type: captcha_failed
    context_hash: aaaa
  recommends:
    action: switch_captcha_solver
    args:
      solver: 2captcha_api
  confidence_evidence: 5
  created_at: '2026-07-16T14:30:22+00:00'
  source_digest_run: run-1
  status: active
  supersedes: null
"""
    )
    rules = brain.query_rules(site="branded_surveys")
    assert len(rules) == 1
    assert rules[0].recommends["action"] == "switch_captcha_solver"
