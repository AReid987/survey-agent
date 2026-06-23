"""Profile resolver — maps survey questions to profile answers."""

from __future__ import annotations

from typing import Any

from .store import ProfileStore

# Keyword mappings from common survey question phrases to profile keys
KEYWORD_MAP: dict[str, str] = {
    # Demographics
    "how old": "demographics.age",
    "your age": "demographics.age",
    "age range": "demographics.age",
    "gender": "demographics.gender",
    "sex": "demographics.gender",
    "ethnicity": "demographics.ethnicity",
    "race": "demographics.ethnicity",
    "marital": "demographics.marital_status",
    "married": "demographics.marital_status",
    "relationship status": "demographics.marital_status",
    "zip code": "demographics.zip_code",
    "postal code": "demographics.zip_code",
    "state": "demographics.state",
    "city": "demographics.city",
    "country": "demographics.country",
    # Employment
    "employment": "employment.status",
    "employed": "employment.status",
    "work status": "employment.status",
    "job": "employment.job_title",
    "occupation": "employment.job_title",
    "industry": "employment.industry",
    "company size": "employment.company_size",
    "years of experience": "employment.years_experience",
    "remote": "employment.work_remote",
    # Income
    "household income": "income.household_income",
    "annual income": "income.household_income",
    "personal income": "income.personal_income",
    "credit score": "income.credit_score_range",
    "homeowner": "income.homeownership",
    "housing": "income.homeownership",
    "rent or own": "income.homeownership",
    # Education
    "education": "education.highest_degree",
    "degree": "education.highest_degree",
    "highest level": "education.highest_degree",
    "field of study": "education.field_of_study",
    "major": "education.field_of_study",
    "enrolled": "education.currently_enrolled",
    # Health
    "health": "health.general_health",
    "smoke": "health.smoker",
    "exercise": "health.exercise_frequency",
    "work out": "health.exercise_frequency",
    "insurance": "health.insurance_type",
    # Shopping
    "online shopping": "shopping.online_shopping_frequency",
    "shop online": "shopping.online_shopping_frequency",
    "purchase decision": "shopping.decision_factors",
    # Politics
    "political": "politics.political_affiliation",
    "party": "politics.political_affiliation",
    "liberal": "politics.political_views",
    "conservative": "politics.political_views",
    # Household
    "household size": "household.household_size",
    "people in household": "household.household_size",
    "children": "household.children_count",
    "kids": "household.children_count",
    "pets": "household.pets",
    # Technology
    "device": "technology.primary_device",
    "operating system": "technology.os_preference",
    "social media": "technology.social_media",
    "streaming": "technology.streaming_services",
    # Lifestyle
    "hobbies": "lifestyle.hobbies",
    "interests": "lifestyle.hobbies",
    "travel": "lifestyle.travel_frequency",
    "dining out": "lifestyle.dining_out_frequency",
    "alcohol": "lifestyle.alcohol_consumption",
}


def match_question_to_profile(question_text: str) -> str | None:
    """Match a survey question to a profile field key using keyword matching.

    Returns the dotted key (e.g. 'demographics.age') or None.
    """
    q_lower = question_text.lower()

    # Direct keyword matching
    best_match = None
    best_score = 0
    for keywords, key in KEYWORD_MAP.items():
        if keywords in q_lower:
            score = len(keywords)
            if score > best_score:
                best_score = score
                best_match = key

    return best_match


def resolve_answer(store: ProfileStore, question_text: str) -> Any:
    """Given a question, look up the matching profile attribute and return the value.

    Returns None if no match found or no value stored.
    """
    key = match_question_to_profile(question_text)
    if key:
        return store.get(key)
    return None


def resolve_answer_with_options(
    store: ProfileStore,
    question_text: str,
    available_options: list[str],
) -> str | None:
    """Resolve an answer and map it to the best matching option from available choices.

    Uses fuzzy matching when the exact profile value doesn't match an option.
    """
    answer = resolve_answer(store, question_text)
    if answer is None:
        return None

    answer_str = str(answer).lower()

    # Try exact match first
    for opt in available_options:
        if opt.lower() == answer_str:
            return opt

    # Try substring match
    for opt in available_options:
        if answer_str in opt.lower() or opt.lower() in answer_str:
            return opt

    # Return the closest option by edit distance (simple)
    def edit_distance(a: str, b: str) -> int:
        m, n = len(a), len(b)
        dp = list(range(n + 1))
        for i in range(1, m + 1):
            prev, dp[0] = dp[0], i
            for j in range(1, n + 1):
                temp = dp[j]
                dp[j] = prev if a[i - 1] == b[j - 1] else 1 + min(prev, dp[j], dp[j - 1])
                prev = temp
        return dp[n]

    best_opt = min(available_options, key=lambda o: edit_distance(answer_str, o.lower()))
    return best_opt
