"""Attention check detection and handling."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class AttentionCheck:
    """A detected attention check."""

    instruction: str  # e.g. "select strongly agree for this row"
    target_value: str  # e.g. "strongly agree"
    question_text: str


# Patterns that indicate attention checks
ATTENTION_PATTERNS = [
    # "To show you are paying attention, select X"
    r"(?:to\s+show\s+(?:you\s+)?(?:are\s+)?(?:paying\s+)?attention|attention\s+check|quality\s+check).*?(?:select|choose|pick|click)\s+(.+?)(?:\.|$)",
    # "For this question, please select X"
    r"(?:for\s+this\s+(?:question|row|item)).*?(?:please\s+)?(?:select|choose|pick|click)\s+(.+?)(?:\.|$)",
    # "Select X for this row"
    r"(?:select|choose|pick|click)\s+(.+?)\s+(?:for|in|on)\s+(?:this|the)\s+(?:row|question|item|line)",
    # "Please select X"
    r"(?:please\s+)?(?:select|choose|pick|click)\s+['\"]?(.+?)['\"]?\s+(?:to\s+(?:continue|proceed|verify))",
    # "Enter N in the box" (arithmetic attention checks)
    r"(?:enter|type|input)\s+(\d+)\s+(?:in|into)\s+(?:the\s+)?(?:box|field|input)",
    # Mathematical attention check: "What is X + Y?"
    r"(?:what\s+is|calculate|solve)\s+(\d+\s*[\+\-\*\/]\s*\d+)\s*(?:\?|equals|=)",
]


def detect_attention_check(question_text: str) -> AttentionCheck | None:
    """Detect if a question contains an attention check.

    Returns an AttentionCheck if detected, None otherwise.
    """
    text_lower = question_text.lower().strip()

    for pattern in ATTENTION_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            target = match.group(1).strip().strip("'\"")
            return AttentionCheck(
                instruction=question_text,
                target_value=target,
                question_text=question_text,
            )
    return None


class AttentionCheckHandler:
    """Handle attention checks by following the explicit instruction."""

    def __init__(self):
        self._detected_count = 0

    def handle(self, question_text: str, available_options: list[str] | None = None) -> str | None:
        """Process a question and return the answer if it's an attention check.

        Args:
            question_text: The question text.
            available_options: Available options for radio/checkbox questions.

        Returns:
            The answer to use if this is an attention check, None otherwise.
        """
        check = detect_attention_check(question_text)
        if not check:
            return None

        self._detected_count += 1
        target = check.target_value

        # Check for arithmetic attention checks
        arithmetic_match = re.search(r"(\d+)\s*([\+\-\*\/])\s*(\d+)", target)
        if arithmetic_match:
            a, op, b = int(arithmetic_match.group(1)), arithmetic_match.group(2), int(arithmetic_match.group(3))
            result = {
                "+": a + b,
                "-": a - b,
                "*": a * b,
                "/": a / b if b != 0 else 0,
            }.get(op, 0)
            return str(int(result) if result == int(result) else result)

        # Check for numeric entry ("enter N in the box")
        if target.isdigit():
            return target

        # Match against available options
        if available_options:
            for opt in available_options:
                if target.lower() in opt.lower() or opt.lower() in target.lower():
                    return opt

        return target

    @property
    def detected_count(self) -> int:
        return self._detected_count
