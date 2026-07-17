"""Tests for the brain's pydantic schemas and persona denylist."""

from __future__ import annotations

import pytest

from survey_agent.brain.schemas import EventRecord, PersonaDenylist

DENY_EXAMPLES = [
    "demographics.age",
    "demographics.gender",
    "demographics.ethnicity",
    "demographics.marital_status",
    "demographics.zip_code",
    "demographics.state",
    "demographics.city",
    "demographics.country",
    "demographics.birth_month",
    "demographics.birth_day",
    "demographics.birth_year",
    "employment.status",
    "employment.industry",
    "employment.job_title",
    "income.household_income",
    "income.personal_income",
    "income.credit_score_range",
    "income.homeownership",
    "education.highest_degree",
    "education.field_of_study",
    "education.currently_enrolled",
    "health.general_health",
    "health.smoker",
    "health.exercise_frequency",
    "health.conditions",
    "health.insurance_type",
]

ALLOW_EXAMPLES = [
    "shopping.preferred_stores",
    "shopping.decision_factors",
    "shopping.online_shopping_frequency",
    "shopping.annual_spending_range",
    "politics.political_views",
    "technology.used_products",
]


def test_denylist_blocks_demographic_fields():
    for path in DENY_EXAMPLES:
        assert PersonaDenylist.is_denied(path), f"must deny {path}"


def test_denylist_allows_soft_preference_fields():
    for path in ALLOW_EXAMPLES:
        assert not PersonaDenylist.is_denied(path), f"must allow {path}"


def test_denylist_matches_prefixes():
    # Nested keys under a denied prefix are also denied
    assert PersonaDenylist.is_denied("demographics.foo")
    assert PersonaDenylist.is_denied("income.sub.field")
    assert not PersonaDenylist.is_denied("shopping.sub.field")


def test_event_record_rejects_bad_event_id():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        EventRecord(
            event_id="not-a-uuid",
            site="branded_surveys",
            event_type="captcha_failed",
            context_hash="0" * 64,
            session_id="sess-1",
            created_at="2026-07-16T14:30:22+00:00",
        )


def test_event_record_accepts_valid_payload():
    rec = EventRecord(
        event_id="7f3a0000-0000-4000-8000-000000000001",
        site="branded_surveys",
        event_type="captcha_failed",
        context_hash="a" * 64,
        session_id="sess-1",
        created_at="2026-07-16T14:30:22+00:00",
    )
    assert rec.site == "branded_surveys"
