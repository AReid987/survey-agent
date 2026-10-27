"""Tests for survey outcome classification.

The owner's requirement drives these cases: a genuine screen-out or a broken
survey is a legitimate ending, but a stuck agent is not. Mixing them up is what
made earlier success claims unusable, so the precedence rules are tested
explicitly — especially the polite-disqualification page, which contains
completion language and must still classify as a screen-out.
"""

from __future__ import annotations

from survey_agent.outcome import (
    MIN_PAGES_FOR_COMPLETION,
    SurveyOutcome,
    classify_outcome,
)

# ── Hard evidence: balance delta ────────────────────────────────────────


def test_balance_increase_is_a_completion():
    v = classify_outcome(page_text="", pages_seen=8, points_delta=45)
    assert v.outcome is SurveyOutcome.COMPLETED
    assert v.confidence == 1.0
    assert v.earned_points is True


def test_balance_increase_outranks_screenout_text():
    """If the site paid us, it completed — whatever the page says."""
    v = classify_outcome(
        page_text="Unfortunately, you do not qualify for this survey.",
        pages_seen=6,
        points_delta=30,
    )
    assert v.outcome is SurveyOutcome.COMPLETED


def test_balance_increase_outranks_a_stalled_signal():
    v = classify_outcome(page_text="", pages_seen=9, points_delta=12, progress_stalled=True)
    assert v.outcome is SurveyOutcome.COMPLETED


# ── The agent's own failure: stuck ──────────────────────────────────────


def test_stalled_progress_is_stuck_not_broken():
    v = classify_outcome(page_text="Question 4 of 20", pages_seen=4, progress_stalled=True)
    assert v.outcome is SurveyOutcome.STUCK
    assert v.is_resolved is False


def test_unreadable_page_is_stuck():
    v = classify_outcome(page_read_failed=True, pages_seen=2)
    assert v.outcome is SurveyOutcome.STUCK
    assert v.is_resolved is False


def test_stuck_outranks_completion_text():
    """A frozen page that happens to say 'thank you' is not a completion."""
    v = classify_outcome(
        page_text="Thank you for your responses so far",
        pages_seen=5,
        progress_stalled=True,
    )
    assert v.outcome is SurveyOutcome.STUCK


# ── Legitimate screen-out ───────────────────────────────────────────────


def test_explicit_disqualification_is_a_screenout():
    v = classify_outcome(page_text="You have been screened out of this survey.", pages_seen=2)
    assert v.outcome is SurveyOutcome.SCREENED_OUT
    assert v.is_resolved is True


def test_polite_disqualification_is_not_read_as_completion():
    """The exact trap: a page with BOTH thank-you and disqualification text."""
    v = classify_outcome(
        page_text=("Thank you for your interest in this study. Unfortunately, you do not qualify at this time."),
        pages_seen=6,
    )
    assert v.outcome is SurveyOutcome.SCREENED_OUT


def test_quota_full_is_a_screenout():
    v = classify_outcome(page_text="Sorry, this survey is full.", pages_seen=1)
    assert v.outcome is SurveyOutcome.SCREENED_OUT
    assert v.is_resolved is True


def test_completion_language_too_early_reads_as_screener_ending():
    v = classify_outcome(page_text="Thank you!", pages_seen=MIN_PAGES_FOR_COMPLETION - 1)
    assert v.outcome is SurveyOutcome.SCREENED_OUT
    assert "screener ending" in " ".join(v.reasons)


# ── Provider breakage ───────────────────────────────────────────────────


def test_error_page_is_broken():
    v = classify_outcome(page_text="We're sorry, a technical problem occurred.", pages_seen=3)
    assert v.outcome is SurveyOutcome.BROKEN
    assert v.is_resolved is True  # legitimate ending, just not our fault


def test_expired_session_is_broken():
    v = classify_outcome(page_text="Your session has timed out.", pages_seen=5)
    assert v.outcome is SurveyOutcome.BROKEN


def test_screenout_outranks_broken_when_both_present():
    v = classify_outcome(
        page_text="You do not qualify. If you think this is an error occurred, contact support.",
        pages_seen=4,
    )
    assert v.outcome is SurveyOutcome.SCREENED_OUT


# ── Completion ──────────────────────────────────────────────────────────


def test_completion_language_after_enough_pages():
    v = classify_outcome(page_text="Survey complete! Congratulations.", pages_seen=10)
    assert v.outcome is SurveyOutcome.COMPLETED
    assert v.is_resolved is True


def test_completion_with_zero_delta_is_flagged_as_unconfirmed():
    """Completion text but no points: still a completion, lower confidence."""
    v = classify_outcome(page_text="You've earned your reward.", pages_seen=12, points_delta=0)
    assert v.outcome is SurveyOutcome.COMPLETED
    assert v.earned_points is False
    assert any("unconfirmed" in r for r in v.reasons)


def test_completion_without_balance_info_has_moderate_confidence():
    v = classify_outcome(page_text="Thank you, your responses were recorded.", pages_seen=9)
    assert v.outcome is SurveyOutcome.COMPLETED
    assert 0.5 < v.confidence < 1.0


# ── Ambiguity is never success ──────────────────────────────────────────


def test_dashboard_bounce_early_is_a_screenout():
    v = classify_outcome(page_text="Available surveys", current_url="https://x.com/dashboard", pages_seen=1)
    assert v.outcome is SurveyOutcome.SCREENED_OUT


def test_dashboard_bounce_late_is_unknown_not_completed():
    v = classify_outcome(page_text="Available surveys", current_url="https://x.com/dashboard", pages_seen=9)
    assert v.outcome is SurveyOutcome.UNKNOWN
    assert v.is_resolved is False


def test_no_signal_at_all_is_unknown():
    v = classify_outcome(page_text="Lorem ipsum", current_url="https://x.com/q", pages_seen=4)
    assert v.outcome is SurveyOutcome.UNKNOWN
    assert v.is_resolved is False


def test_empty_input_is_unknown_never_completed():
    v = classify_outcome()
    assert v.outcome is SurveyOutcome.UNKNOWN
    assert v.earned_points is False


def test_every_verdict_carries_auditable_reasons():
    """A human must be able to see why a run was judged the way it was."""
    for verdict in (
        classify_outcome(points_delta=10, pages_seen=5),
        classify_outcome(progress_stalled=True, pages_seen=3),
        classify_outcome(page_text="you do not qualify", pages_seen=2),
        classify_outcome(page_text="technical problem", pages_seen=3),
        classify_outcome(page_text="survey complete", pages_seen=8),
        classify_outcome(),
    ):
        assert verdict.reasons, f"{verdict.outcome} produced no reasons"


def test_case_insensitivity():
    v = classify_outcome(page_text="YOU DO NOT QUALIFY FOR THIS STUDY", pages_seen=2)
    assert v.outcome is SurveyOutcome.SCREENED_OUT
