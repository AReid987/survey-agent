"""Survey outcome classification — did the attempt finish, screen out, or break?

Owner requirement (2026-08-26): a run that ends because the screener genuinely
ruled the profile out is a *legitimate* ending and counts as a resolved attempt.
A run that ends because the agent got stuck is NOT. Until those two are told
apart, "success rate" is meaningless and the reliability bar (#loop-bar) cannot
be measured.

This module is the single authority for that judgement. The keyword sets were
previously inlined in ``survey_agent/browser/harness_backend.py``; they live here
now so every backend and the CUA runners reach the same verdict.

Usage::

    verdict = classify_outcome(
        page_text=final_page_text,
        current_url=final_url,
        pages_seen=page_num,
        points_delta=post_points - pre_points,
        progress_stalled=same_url_count >= 3,
    )
    if verdict.outcome is SurveyOutcome.COMPLETED:
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ── Signal vocabularies ─────────────────────────────────────────────────
# Sourced from observed live pages (data/survey_run.log, July 2026) and the
# keyword lists previously embedded in harness_backend.py.

COMPLETION_KEYWORDS: frozenset[str] = frozenset(
    {
        "thank you",
        "survey complete",
        "successfully completed",
        "congratulations",
        "you've earned",
        "you have earned",
        "points have been added",
        "reward has been credited",
        "completion recorded",
    }
)

SCREENOUT_KEYWORDS: frozenset[str] = frozenset(
    {
        "not a match",
        "don't qualify",
        "do not qualify",
        "doesn't qualify",
        "disqualified",
        "screened out",
        "not eligible",
        "we appreciate your interest",
        "unfortunately, you",
        "better luck next time",
        "survey is full",
        "quota has been reached",
        "quota full",
        "no longer available",
    }
)

BROKEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "technical problem",
        "something went wrong",
        "an error occurred",
        "internal server error",
        "service unavailable",
        "page not found",
        "session expired",
        "session has timed out",
        "try again later",
    }
)

# A survey that ends this early was almost certainly a screener decision rather
# than a finished questionnaire, even if the page says something congratulatory.
MIN_PAGES_FOR_COMPLETION = 3


class SurveyOutcome(StrEnum):
    """How a survey attempt ended.

    COMPLETED    — the questionnaire was finished; points should follow.
    SCREENED_OUT — the provider legitimately ruled the profile out. Not a
                   failure of the agent; expected and frequent.
    BROKEN       — the survey itself failed (error page, dead end, provider
                   fault). Not the agent's fault, but no points either.
    STUCK        — the agent stopped making progress. THIS is the agent's
                   failure and the only outcome that indicts the automation.
    UNKNOWN      — insufficient signal to judge. Never counted as success.
    """

    COMPLETED = "completed"
    SCREENED_OUT = "screened_out"
    BROKEN = "broken"
    STUCK = "stuck"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OutcomeVerdict:
    """A classification plus the evidence behind it.

    ``reasons`` exists so a human auditing data/survey_run.log can see *why* a
    run was called a completion, rather than trusting a bare label.
    """

    outcome: SurveyOutcome
    confidence: float
    reasons: list[str] = field(default_factory=list)
    points_delta: int | None = None

    @property
    def is_resolved(self) -> bool:
        """True when the survey ended for a legitimate reason.

        Completions, screen-outs, and provider breakage are all *resolved*: the
        attempt reached a real ending. Only STUCK and UNKNOWN mean the agent
        failed to drive the survey to any conclusion.
        """
        return self.outcome in (
            SurveyOutcome.COMPLETED,
            SurveyOutcome.SCREENED_OUT,
            SurveyOutcome.BROKEN,
        )

    @property
    def earned_points(self) -> bool:
        return bool(self.points_delta and self.points_delta > 0)


def _matched(text: str, vocabulary: frozenset[str]) -> list[str]:
    return sorted(kw for kw in vocabulary if kw in text)


def classify_outcome(
    page_text: str = "",
    current_url: str = "",
    *,
    pages_seen: int = 0,
    points_delta: int | None = None,
    progress_stalled: bool = False,
    page_read_failed: bool = False,
) -> OutcomeVerdict:
    """Judge how a survey attempt ended.

    Args:
        page_text: Visible text of the final page reached.
        current_url: URL of the final page.
        pages_seen: How many survey pages were traversed.
        points_delta: Post-run balance minus pre-run balance, when known.
            A positive delta is the strongest possible completion signal — it is
            the site itself confirming payment — and outranks page text.
        progress_stalled: Caller's stuck detector fired (e.g. the same page
            survived a refresh).
        page_read_failed: The page could not be read at all.

    Precedence is deliberate: hard evidence (a balance change) beats page text,
    and being stuck beats an ambiguous page, because calling a stuck run
    "complete" is the failure mode that made the old success numbers unusable.
    """
    text = page_text.lower()
    url = current_url.lower()
    reasons: list[str] = []

    # 1. A confirmed balance increase is the site paying us. Nothing outranks it.
    if points_delta is not None and points_delta > 0:
        reasons.append(f"balance increased by {points_delta}")
        return OutcomeVerdict(SurveyOutcome.COMPLETED, 1.0, reasons, points_delta)

    # 2. The agent stopped making progress — its own failure, not the survey's.
    if progress_stalled:
        reasons.append("no progress across repeated page reads")
        if pages_seen:
            reasons.append(f"stalled after {pages_seen} pages")
        return OutcomeVerdict(SurveyOutcome.STUCK, 0.9, reasons, points_delta)

    if page_read_failed:
        reasons.append("page could not be read")
        return OutcomeVerdict(SurveyOutcome.STUCK, 0.8, reasons, points_delta)

    # 3. Explicit screen-out language. Checked before completion language because
    #    disqualification pages are frequently polite ("thank you for your
    #    interest, unfortunately you do not qualify") and would otherwise be
    #    misread as completions.
    if screenout_hits := _matched(text, SCREENOUT_KEYWORDS):
        reasons.append(f"screen-out language: {', '.join(screenout_hits[:3])}")
        return OutcomeVerdict(SurveyOutcome.SCREENED_OUT, 0.95, reasons, points_delta)

    # 4. Provider-side breakage.
    if broken_hits := _matched(text, BROKEN_KEYWORDS):
        reasons.append(f"error language: {', '.join(broken_hits[:3])}")
        return OutcomeVerdict(SurveyOutcome.BROKEN, 0.85, reasons, points_delta)

    # 5. Completion language, but only if enough of a survey was actually seen.
    if completion_hits := _matched(text, COMPLETION_KEYWORDS):
        if pages_seen >= MIN_PAGES_FOR_COMPLETION:
            reasons.append(f"completion language: {', '.join(completion_hits[:3])}")
            reasons.append(f"{pages_seen} pages traversed")
            confidence = 0.75 if points_delta is None else 0.6
            if points_delta == 0:
                reasons.append("balance unchanged — completion unconfirmed")
            return OutcomeVerdict(SurveyOutcome.COMPLETED, confidence, reasons, points_delta)
        reasons.append(
            f"completion language after only {pages_seen} pages "
            f"(< {MIN_PAGES_FOR_COMPLETION}) — reads as a screener ending"
        )
        return OutcomeVerdict(SurveyOutcome.SCREENED_OUT, 0.6, reasons, points_delta)

    # 6. Bounced back to a dashboard without any completion signal.
    if any(marker in url for marker in ("/dashboard", "/members", "/surveys")):
        if pages_seen < MIN_PAGES_FOR_COMPLETION:
            reasons.append(f"returned to dashboard after {pages_seen} pages")
            return OutcomeVerdict(SurveyOutcome.SCREENED_OUT, 0.55, reasons, points_delta)
        reasons.append(f"returned to dashboard after {pages_seen} pages, no completion text")
        return OutcomeVerdict(SurveyOutcome.UNKNOWN, 0.4, reasons, points_delta)

    reasons.append("no recognizable completion, screen-out, or error signal")
    return OutcomeVerdict(SurveyOutcome.UNKNOWN, 0.2, reasons, points_delta)
