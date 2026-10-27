"""Stable hashing and string normalization helpers.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §4.1
"""

from __future__ import annotations

import hashlib
import json
import re


def hash_context(context: dict) -> str:
    """Return sha256 hex digest of a JSON-serialized context dict.

    Uses sort_keys=True so key order is irrelevant. Lists are serialized
    in order (positional) because in a survey failure context, list order
    can carry meaning (e.g., challenge sequences).
    """
    payload = json.dumps(context, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_site(site: str) -> str:
    return site.strip().lower()


def normalize_question(text: str) -> str:
    """Collapse a question to its comparable form.

    Survey engines re-render the same question with different whitespace,
    numbering, or casing between pages and sessions. Normalizing before
    hashing is what makes the ask-once rule (docs/adr/0002) hold: the owner
    should not be texted twice because a question gained a trailing space.
    """
    collapsed = " ".join(text.split()).strip().lower()
    # Strip a leading enumeration like "12." or "3)" that surveys add per page.
    collapsed = re.sub(r"^\d+\s*[.)]\s*", "", collapsed)
    return collapsed.rstrip("*: ").strip()


def hash_question(text: str, options: list[str] | None = None) -> str:
    """Return the stable identity of a question, including its options.

    Options participate in the hash because the same prompt with a different
    option set is a genuinely different question and may warrant a new answer.
    Option order is ignored — surveys shuffle options to defeat automation.
    """
    payload = {
        "q": normalize_question(text),
        "opts": sorted(normalize_question(o) for o in (options or [])),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
