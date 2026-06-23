"""Question classifier — determine question type from text and page structure."""

from __future__ import annotations

import re
from enum import Enum


class QuestionType(str, Enum):
    RADIO = "radio"
    CHECKBOX = "checkbox"
    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    SCALE = "scale"
    SLIDER = "slider"
    DATE = "date"
    MATRIX = "matrix"
    OPEN_ENDED = "open_ended"


# Keyword patterns for classification
_PATTERNS = [
    (r"select all (that|which) apply", QuestionType.CHECKBOX),
    (r"check all", QuestionType.CHECKBOX),
    (r"choose (all|any|multiple)", QuestionType.CHECKBOX),
    (r"rate\s+(on\s+a\s+)?(scale|1[\-\s]*(?:to|through)\s*10|\d+\s*(?:to|through)\s*\d+)", QuestionType.SCALE),
    (r"(strongly\s+)?(?:agree|disagree)", QuestionType.SCALE),
    (r"(?:very\s+)?(?:satisfied|dissatisfied)", QuestionType.SCALE),
    (r"how (?:much|often|likely|satisfied|happy|comfortable)", QuestionType.SCALE),
    (r"(?:on\s+a\s+)?(?:scale|slider|range)", QuestionType.SLIDER),
    (
        r"(?:please\s+)?explain|describe|tell\s+us\s+(?:more|why)|elaborate|in\s+your\s+own\s+words",
        QuestionType.OPEN_ENDED,
    ),
    (
        r"what\s+(?:do\s+you|is\s+your|are\s+your)\s+(?:think|opinion|reason|experience|favorite)",
        QuestionType.OPEN_ENDED,
    ),
    (r"(?:any\s+)?(?:additional|other)\s+(?:comments|feedback|thoughts|notes)", QuestionType.OPEN_ENDED),
    (r"(?:please\s+)?specify|other\s*:", QuestionType.TEXT),
    (r"date\s+of\s+birth|birth\s+date|dob", QuestionType.DATE),
    (r"(?:enter|type|input|provide)\s+(?:your|the)", QuestionType.TEXT),
    (r"which\s+of\s+the\s+following", QuestionType.RADIO),
    (r"(?:pick|choose)\s+(?:one|a)\s+", QuestionType.RADIO),
    (r"(?:what|which)\s+is\s+your\s+(?:favorite|preferred|primary)", QuestionType.RADIO),
]


def classify(question_text: str, html_context: str = "") -> QuestionType:
    """Classify a question type from its text and optional HTML context.

    Uses keyword pattern matching. For higher accuracy, pass HTML context
    that includes form element types (radio inputs, checkboxes, etc.).
    """
    text_lower = question_text.lower().strip()

    # Check HTML context for form elements first (most reliable)
    if html_context:
        html_lower = html_context.lower()
        if "<textarea" in html_lower:
            return QuestionType.TEXTAREA
        if 'type="radio"' in html_lower or "type='radio'" in html_lower:
            return QuestionType.RADIO
        if 'type="checkbox"' in html_lower or "type='checkbox'" in html_lower:
            return QuestionType.CHECKBOX
        if "<select" in html_lower:
            return QuestionType.SELECT
        if 'type="range"' in html_lower or "type='range'" in html_lower:
            return QuestionType.SLIDER
        if 'type="date"' in html_lower or "type='date'" in html_lower:
            return QuestionType.DATE

    # Pattern matching on question text
    for pattern, qtype in _PATTERNS:
        if re.search(pattern, text_lower):
            return qtype

    # Default to text input
    return QuestionType.TEXT


def classify_from_page_elements(elements: list[dict]) -> QuestionType:
    """Classify based on actual page form elements.

    Args:
        elements: List of dicts with 'tag', 'type', 'role' keys from DOM inspection.
    """
    tags = {e.get("tag", "").lower() for e in elements}
    types = {e.get("type", "").lower() for e in elements}

    if "textarea" in tags:
        return QuestionType.TEXTAREA
    if "radio" in types:
        return QuestionType.RADIO
    if "checkbox" in types:
        return QuestionType.CHECKBOX
    if "select" in tags:
        return QuestionType.SELECT
    if "range" in types:
        return QuestionType.SLIDER
    if "date" in types:
        return QuestionType.DATE
    if "input" in tags or "text" in types:
        return QuestionType.TEXT
    return QuestionType.OPEN_ENDED
