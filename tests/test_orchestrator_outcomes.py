"""Tests for the orchestrator's outcome classification and threshold wiring.

These cover the seam added for #loop-classify and #payouts-notify: a backend
reporting ``success`` no longer counts as a completion on its own, and payout
thresholds text the owner once rather than once per survey.
"""

from __future__ import annotations

import pytest

from survey_agent.orchestrator import SurveySession


class _Result:
    def __init__(self, success: bool = True, output: str = "", error: str = ""):
        self.success = success
        self.output = output
        self.error = error


class _FakeAgent:
    """Minimal backend stub driving one survey then reporting none left."""

    def __init__(self, complete_result: _Result, balances: list[str]):
        self._complete_result = complete_result
        self._balances = balances
        self._balance_calls = 0
        self.discover_calls = 0

    async def login(self, site_url, site_name):  # noqa: ARG002
        return _Result(True, "logged in")

    async def check_balance(self, dashboard_url):  # noqa: ARG002
        idx = min(self._balance_calls, len(self._balances) - 1)
        self._balance_calls += 1
        return _Result(True, self._balances[idx])

    async def discover_surveys(self, dashboard_url):  # noqa: ARG002
        self.discover_calls += 1
        if self.discover_calls > 1:
            return _Result(True, "no surveys available")
        return _Result(True, "1. Survey A -> /survey/a")

    async def complete_survey(self, survey_info, profile_context):  # noqa: ARG002
        return self._complete_result


class _FakeProfile:
    def to_context_string(self):
        return "profile"


def _session(agent, config, site_slug="branded-surveys"):
    return SurveySession(
        agent=agent,
        site_slug=site_slug,
        site_info={"name": "Branded Surveys", "url": "https://x", "dashboard": "https://x/dash"},
        profile=_FakeProfile(),
        config=config,
    )


@pytest.fixture()
def config(tmp_data_dir):  # noqa: ARG001
    from survey_agent.config import AppConfig

    return AppConfig(payout_thresholds=[100.0, 500.0], notify_enabled=True)


@pytest.fixture()
def captured_texts(monkeypatch):
    """Capture threshold notifications instead of sending them."""
    calls: list[dict] = []

    def _fake_notify(*, site, balance, threshold, data_dir, currency="points"):  # noqa: ARG001
        from survey_agent.notify import NotifyResult

        calls.append({"site": site, "balance": balance, "threshold": threshold})
        return NotifyResult(True, destination="+1555")

    monkeypatch.setattr("survey_agent.orchestrator.notify_threshold", _fake_notify)
    return calls


# ── Page-count parsing ──────────────────────────────────────────────────


def test_pages_from_output_reads_question_count():
    assert SurveySession._pages_from_output("Survey completed after 12 questions") == 12


def test_pages_from_output_reads_page_marker():
    assert SurveySession._pages_from_output("Page 7: answered") == 7


def test_pages_from_output_returns_zero_when_unparseable():
    """0 is the safe default — the classifier will not call it a completion."""
    assert SurveySession._pages_from_output("done") == 0
    assert SurveySession._pages_from_output("") == 0


# ── Classification replaces bare result.success ─────────────────────────


async def test_backend_success_with_screenout_text_is_not_a_completion(config, captured_texts):  # noqa: ARG001
    """The core fix: 'success' plus disqualification text is a screen-out."""
    agent = _FakeAgent(
        _Result(True, "Survey completed after 6 questions. Unfortunately, you do not qualify."),
        balances=["BALANCE: 50", "BALANCE: 50"],
    )
    result = await _session(agent, config).run(max_surveys=1)

    assert result["stats"]["surveys_completed"] == 0
    assert result["stats"].get("screened_out") == 1
    assert result["outcomes"]["screened_out"] == 1


async def test_balance_increase_counts_as_a_completion(config, captured_texts):  # noqa: ARG001
    agent = _FakeAgent(
        _Result(True, "Survey completed after 10 questions"),
        balances=["BALANCE: 50", "BALANCE: 95"],
    )
    result = await _session(agent, config).run(max_surveys=1)

    assert result["stats"]["surveys_completed"] == 1
    assert result["stats"]["points_earned"] == 45
    assert result["outcomes"]["completed"] == 1


async def test_ambiguous_ending_is_counted_as_an_error_not_a_success(config, captured_texts):  # noqa: ARG001
    agent = _FakeAgent(
        _Result(True, "Lorem ipsum dolor"),
        balances=["BALANCE: 50", "BALANCE: 50"],
    )
    result = await _session(agent, config).run(max_surveys=1)

    assert result["stats"]["surveys_completed"] == 0
    assert result["stats"]["errors"] == 1
    assert result["outcomes"]["unknown"] == 1


async def test_outcomes_breakdown_is_returned_for_auditing(config, captured_texts):  # noqa: ARG001
    agent = _FakeAgent(
        _Result(True, "Survey completed after 8 questions. Thank you!"),
        balances=["BALANCE: 10", "BALANCE: 60"],
    )
    result = await _session(agent, config).run(max_surveys=1)
    assert "outcomes" in result
    assert sum(result["outcomes"].values()) == 1


# ── Threshold notification ──────────────────────────────────────────────


async def test_threshold_text_fires_when_balance_crosses(config, captured_texts):
    agent = _FakeAgent(
        _Result(True, "Survey completed after 9 questions"),
        balances=["BALANCE: 80", "BALANCE: 120"],
    )
    await _session(agent, config).run(max_surveys=1)

    assert len(captured_texts) == 1
    assert captured_texts[0]["threshold"] == 100.0
    assert captured_texts[0]["site"] == "Branded Surveys"


async def test_no_threshold_text_below_the_threshold(config, captured_texts):
    agent = _FakeAgent(
        _Result(True, "Survey completed after 9 questions"),
        balances=["BALANCE: 10", "BALANCE: 40"],
    )
    await _session(agent, config).run(max_surveys=1)
    assert captured_texts == []


async def test_notify_disabled_sends_nothing(tmp_data_dir, captured_texts):  # noqa: ARG001
    from survey_agent.config import AppConfig

    cfg = AppConfig(payout_thresholds=[100.0], notify_enabled=False)
    agent = _FakeAgent(
        _Result(True, "Survey completed after 9 questions"),
        balances=["BALANCE: 80", "BALANCE: 500"],
    )
    await _session(agent, cfg).run(max_surveys=1)
    assert captured_texts == []


async def test_empty_threshold_list_sends_nothing(tmp_data_dir, captured_texts):  # noqa: ARG001
    from survey_agent.config import AppConfig

    cfg = AppConfig(payout_thresholds=[], notify_enabled=True)
    agent = _FakeAgent(
        _Result(True, "Survey completed after 9 questions"),
        balances=["BALANCE: 80", "BALANCE: 900"],
    )
    await _session(agent, cfg).run(max_surveys=1)
    assert captured_texts == []


async def test_notification_failure_does_not_abort_the_run(config, monkeypatch):
    """A texting failure must never cost us a survey."""

    def _boom(**kwargs):  # noqa: ARG001
        raise RuntimeError("imsg exploded")

    monkeypatch.setattr("survey_agent.orchestrator.notify_threshold", _boom)
    agent = _FakeAgent(
        _Result(True, "Survey completed after 9 questions"),
        balances=["BALANCE: 80", "BALANCE: 300"],
    )
    result = await _session(agent, config).run(max_surveys=1)

    # The survey still counted despite the notification blowing up.
    assert result["stats"]["surveys_completed"] == 1
    assert "error" not in result
