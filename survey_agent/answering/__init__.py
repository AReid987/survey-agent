"""Question answering engine — maps questions to profile answers and fills forms."""

from __future__ import annotations

from .attention_check import AttentionCheckHandler, detect_attention_check
from .form_filler import FormFiller
from .matcher import match_question_to_profile, resolve_answer, resolve_answer_with_options
from .open_ended import OpenEndedGenerator
from .question_classifier import QuestionType, classify

__all__ = [
    "classify",
    "QuestionType",
    "match_question_to_profile",
    "resolve_answer",
    "resolve_answer_with_options",
    "FormFiller",
    "OpenEndedGenerator",
    "detect_attention_check",
    "AttentionCheckHandler",
]
