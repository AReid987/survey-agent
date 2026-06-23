"""Question matcher — maps survey questions to profile attributes."""

from __future__ import annotations

from typing import Any

from ..profile.resolver import KEYWORD_MAP
from ..profile.store import ProfileStore


def match_question_to_profile(question_text: str) -> str | None:
    """Match a survey question to a profile field key using keyword matching.

    Returns the dotted key (e.g. 'demographics.age') or None.
    """
    q_lower = question_text.lower()
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
    """Given a question, look up the matching profile attribute and return the value."""
    key = match_question_to_profile(question_text)
    if key:
        return store.get(key)
    return None


def resolve_answer_with_options(
    store: ProfileStore,
    question_text: str,
    available_options: list[str],
) -> str | None:
    """Resolve an answer and map it to the best matching option from available choices."""
    answer = resolve_answer(store, question_text)
    if answer is None:
        return None

    answer_str = str(answer).lower()

    # Exact match
    for opt in available_options:
        if opt.lower() == answer_str:
            return opt

    # Substring match
    for opt in available_options:
        if answer_str in opt.lower() or opt.lower() in answer_str:
            return opt

    # Edit distance
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
