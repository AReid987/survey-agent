"""Pydantic models and persona denylist for the brain foundation.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §3.2, Appendix A, B
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class PersonaDenylist:
    """Prefix-based denylist for paths inside profile.json.

    The denylist is the spec §3.2 invariant: any path matching one of these
    prefixes MUST NOT be modified by a digest proposal. Enforcement happens
    in code, not in prompts.
    """

    DENYLIST_PREFIXES: frozenset[str] = frozenset(
        {
            "demographics",
            "employment",
            "income",
            "education",
            "health",
        }
    )

    @classmethod
    def is_denied(cls, dotted_path: str) -> bool:
        return any(dotted_path == prefix or dotted_path.startswith(prefix + ".") for prefix in cls.DENYLIST_PREFIXES)


class EventRecord(BaseModel):
    """Spec Appendix A."""

    event_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")  # UUID v4
    site: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    context_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")  # sha256 hex
    context: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] = Field(default_factory=dict)
    session_id: str = Field(min_length=1, max_length=128)
    created_at: str  # ISO 8601 UTC

    @field_validator("created_at")
    @classmethod
    def _validate_iso8601(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class Promotion(BaseModel):
    """A signature-level promotion (spec Appendix C).

    A promotion is keyed on the failure signature (site, event_type,
    context_hash) — not on a single event_id — because re-occurrences of the
    same signature increment one promotion row's count, not create new rows.
    """

    site: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    context_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    status: Literal["pending_digest", "digested"]
    count: int = Field(ge=1)
    first_seen_at: str
    last_seen_at: str

    @field_validator("first_seen_at", "last_seen_at")
    @classmethod
    def _validate_iso8601(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class PersonaHint(BaseModel):
    """A single suggested change to an allowlisted field in profile.json."""

    path: str  # e.g., "shopping.preferred_stores"
    new_value: Any
    rationale: str


class Rule(BaseModel):
    """Spec Appendix B."""

    rule_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    site: str
    applies_when: dict[str, Any]
    recommends: dict[str, Any]
    confidence_evidence: int = Field(ge=1)
    created_at: str
    source_digest_run: str
    status: Literal["active", "superseded", "retracted"]
    supersedes: str | None = None


class DigestResult(BaseModel):
    """Structured LLM output for a digest run."""

    new_rules: list[Rule] = Field(default_factory=list)
    persona_hints: list[PersonaHint] = Field(default_factory=list)
    rationale: str = ""
