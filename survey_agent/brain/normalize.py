"""Stable hashing and string normalization helpers.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §4.1
"""

from __future__ import annotations

import hashlib
import json


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
