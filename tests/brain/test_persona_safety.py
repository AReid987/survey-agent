"""CRITICAL: verifies the denylist invariant holds against adversarial LLM outputs.

Spec §3.2, Success Criterion #4 — across 100 simulated adversarial digest
runs, no denylist field is modified in profile.json.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from survey_agent.brain import digest as digest_mod
from survey_agent.brain.schemas import PersonaDenylist
from survey_agent.brain.store import FailureStore
from survey_agent.config import BrainConfig

DENYLIST_FIELDS = [
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
    "demographics.birth_date",
    "employment.status",
    "employment.industry",
    "employment.job_title",
    "employment.company_size",
    "employment.years_experience",
    "employment.work_remote",
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


PROFILE_BASE = {
    "demographics": {
        "age": 39,
        "gender": "Male",
        "ethnicity": "Black/African American",
        "marital_status": "Domestic partnership",
        "zip_code": "78736",
        "state": "TX",
        "city": "Austin",
        "country": "US",
        "birth_month": 3,
        "birth_day": 15,
        "birth_year": 1986,
        "birth_date": "1986-03-15",
    },
    "employment": {
        "status": "Self-employed",
        "industry": "Technology",
        "job_title": "Founder/CTO",
        "company_size": "1-10",
        "years_experience": 8,
        "work_remote": "Fully remote",
    },
    "income": {
        "household_income": "$50,000-$74,999",
        "personal_income": "$50,000-$74,999",
        "credit_score_range": "Poor",
        "homeownership": "Rent",
    },
    "education": {
        "highest_degree": "Trade/vocational",
        "field_of_study": "Software Engineering",
        "currently_enrolled": False,
    },
    "health": {
        "general_health": "Very good",
        "smoker": "Former",
        "exercise_frequency": "Several times a week",
        "conditions": [],
        "insurance_type": "Uninsured",
    },
    "shopping": {"preferred_stores": ["Amazon"], "decision_factors": ["Price"]},
}


@pytest.fixture()
def baseline_profile_path(tmp_data_dir):
    p = tmp_data_dir / "profile.json"
    p.write_text(json.dumps(PROFILE_BASE))
    return p


def _baseline_value(profile: dict, dotted_path: str) -> Any:
    cur = profile
    for p in dotted_path.split("."):
        cur = cur[p]
    return cur


def _adversarial_hint(denylist_field: str) -> dict:
    """Produce a persona hint that tries to change a denylist field."""
    if denylist_field == "demographics.age":
        return {"path": denylist_field, "new_value": 25, "rationale": "would qualify for more surveys"}
    if denylist_field == "demographics.gender":
        return {"path": denylist_field, "new_value": "Female", "rationale": "test"}
    if denylist_field == "demographics.zip_code":
        return {"path": denylist_field, "new_value": "90210", "rationale": "test"}
    if denylist_field == "income.household_income":
        return {"path": denylist_field, "new_value": "$200,000+", "rationale": "test"}
    if denylist_field == "education.highest_degree":
        return {"path": denylist_field, "new_value": "Doctorate", "rationale": "test"}
    if denylist_field == "health.insurance_type":
        return {"path": denylist_field, "new_value": "Private", "rationale": "test"}
    if denylist_field == "employment.industry":
        return {"path": denylist_field, "new_value": "Healthcare", "rationale": "test"}
    return {"path": denylist_field, "new_value": "X", "rationale": "test"}


def test_denylist_holds_under_100_adversarial_runs(brain_dir, baseline_profile_path, tmp_data_dir, monkeypatch):
    """Run 100 adversarial digests; no denylist field changes."""
    store = FailureStore(brain_dir)
    monkeypatch.setattr(
        digest_mod,
        "call_llm",
        lambda _prompt: {
            "new_rules": [],
            "persona_hints": [_adversarial_hint(random.choice(DENYLIST_FIELDS)) for _ in range(random.randint(1, 5))],
            "rationale": "adversarial",
        },
    )

    for i in range(100):
        # Reset profile to baseline before each run to keep state deterministic
        baseline_profile_path.write_text(json.dumps(PROFILE_BASE))

        digest_mod.run_digest(
            trigger=f"adversarial_test_{i}",
            store=store,
            config=BrainConfig(),
            data_dir=tmp_data_dir,
        )
        # After every run, ALL denylist fields MUST equal baseline
        after = json.loads(baseline_profile_path.read_text())
        for f in DENYLIST_FIELDS:
            assert _baseline_value(after, f) == _baseline_value(PROFILE_BASE, f), f"DENYLIST VIOLATED on run {i}: {f}"


def test_denylist_is_denied_returns_true_for_every_blocked_path():
    for f in DENYLIST_FIELDS:
        assert PersonaDenylist.is_denied(f), f"{f} must be denied"
