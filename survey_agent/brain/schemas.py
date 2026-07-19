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
    fix_proposals: list[dict[str, Any]] = Field(default_factory=list)
    rationale: str = ""


# ── Autoresearch Pipeline Schemas ──────────────────────────────────────


class ImplementationStep(BaseModel):
    """A single step in a fix proposal's implementation plan."""

    step_type: Literal["code_edit", "config_change", "command", "web_search", "test"]
    description: str
    target_file: str | None = None
    command: str | None = None
    expected_outcome: str
    actual_outcome: str | None = None
    status: Literal["pending", "running", "success", "failed", "skipped"] = "pending"


class TraceEntry(BaseModel):
    """A single entry in the fix execution trace."""

    timestamp: str
    phase: str  # "research", "implement", "verify"
    action: str
    result: str
    duration_ms: int = 0


class FixProposal(BaseModel):
    """A proposed fix for a recurring failure pattern.

    Unlike a Rule (which is passive — "skip when X"), a FixProposal is active:
    it diagnoses the root cause, researches solutions, and proposes concrete
    implementation steps to fix the problem.
    """

    proposal_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    trigger_event_type: str
    site: str
    diagnosis: str  # Root cause analysis
    research_findings: list[str] = Field(default_factory=list)
    solution: str  # Proposed fix description
    implementation_steps: list[ImplementationStep] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal[
        "proposed", "researching", "implementing", "verified", "applied", "failed"
    ] = "proposed"
    created_at: str
    applied_at: str | None = None
    trace: list[TraceEntry] = Field(default_factory=list)


class Learning(BaseModel):
    """A documented learning from a fix attempt."""

    learning_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    proposal_id: str
    site: str
    failure_pattern: str
    fix_description: str
    fix_type: str  # "code_edit", "config_change", "workaround"
    outcome: Literal["success", "partial", "failed"]
    evidence: str  # What proved it worked or didn't
    created_at: str
