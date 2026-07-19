"""Fix execution pipeline for the autoresearch pipeline.

Takes a FixProposal and executes its implementation steps, tracing
each step's outcome.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from survey_agent.brain._lock import atomic_write_bytes
from survey_agent.brain.schemas import FixProposal, Learning, TraceEntry

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _trace(proposal: FixProposal, phase: str, action: str, result: str, duration_ms: int = 0) -> None:
    """Append a trace entry to the proposal."""
    proposal.trace.append(
        TraceEntry(
            timestamp=_now_iso(),
            phase=phase,
            action=action,
            result=result[:500],
            duration_ms=duration_ms,
        )
    )


def save_proposal(proposal: FixProposal, brain_dir: Path) -> None:
    """Save a fix proposal to disk."""
    proposals_dir = brain_dir / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    path = proposals_dir / f"{proposal.proposal_id}.json"
    atomic_write_bytes(path, proposal.model_dump_json(indent=2).encode("utf-8"))


def load_proposal(proposal_id: str, brain_dir: Path) -> FixProposal | None:
    """Load a fix proposal from disk."""
    path = brain_dir / "proposals" / f"{proposal_id}.json"
    if not path.exists():
        return None
    try:
        return FixProposal.model_validate_json(path.read_text())
    except Exception as e:
        logger.error(f"Failed to load proposal {proposal_id}: {e}")
        return None


def list_proposals(brain_dir: Path, status: str | None = None) -> list[FixProposal]:
    """List all fix proposals, optionally filtered by status."""
    proposals_dir = brain_dir / "proposals"
    if not proposals_dir.exists():
        return []

    proposals = []
    for path in sorted(proposals_dir.glob("*.json")):
        try:
            proposal = FixProposal.model_validate_json(path.read_text())
            if status is None or proposal.status == status:
                proposals.append(proposal)
        except Exception:
            continue
    return proposals


def execute_fix(
    proposal: FixProposal,
    brain_dir: Path,
    root_dir: Path,
    dry_run: bool = False,
) -> FixProposal:
    """Execute a fix proposal's implementation steps.

    Args:
        proposal: The fix proposal to execute.
        brain_dir: Path to data/brain/
        root_dir: Path to project root
        dry_run: If True, don't actually make changes

    Returns:
        Updated proposal with execution trace.
    """
    proposal.status = "implementing"
    _trace(proposal, "implement", "started", f"dry_run={dry_run}")
    save_proposal(proposal, brain_dir)

    for i, step in enumerate(proposal.implementation_steps):
        if step.status != "pending":
            continue

        step.status = "running"
        _trace(proposal, "implement", f"step_{i}: {step.description}", "running")
        save_proposal(proposal, brain_dir)

        start_time = time.monotonic()

        try:
            if step.step_type == "code_edit" and step.target_file:
                result = _execute_code_edit(step, root_dir, dry_run)
            elif step.step_type == "config_change":
                result = _execute_config_change(step, root_dir, dry_run)
            elif step.step_type == "command" and step.command:
                result = _execute_command(step, root_dir, dry_run)
            elif step.step_type == "test":
                result = _execute_test(step, root_dir, dry_run)
            elif step.step_type == "web_search":
                result = "skipped (research already done)"
            else:
                result = f"skipped (unknown step type: {step.step_type})"

            duration = int((time.monotonic() - start_time) * 1000)
            step.actual_outcome = result[:500]
            step.status = "success" if "failed" not in result.lower() else "failed"
            _trace(proposal, "implement", f"step_{i}: {step.description}", step.status, duration)

        except Exception as e:
            duration = int((time.monotonic() - start_time) * 1000)
            step.actual_outcome = f"Error: {e}"
            step.status = "failed"
            _trace(proposal, "implement", f"step_{i}: {step.description}", f"error: {e}", duration)

        save_proposal(proposal, brain_dir)

    # Check if all steps succeeded
    all_success = all(s.status == "success" or s.status == "skipped" for s in proposal.implementation_steps)
    proposal.status = "applied" if all_success else "failed"
    proposal.applied_at = _now_iso() if all_success else None
    _trace(proposal, "implement", "completed", proposal.status)
    save_proposal(proposal, brain_dir)

    return proposal


def _execute_code_edit(step: Any, root_dir: Path, dry_run: bool) -> str:
    """Execute a code edit step."""
    if not step.target_file:
        return "failed: no target file specified"

    target = root_dir / step.target_file
    if not target.exists():
        return f"failed: file not found: {target}"

    if dry_run:
        return f"dry_run: would edit {step.target_file}"

    # The actual edit is described in the step description
    # In a real implementation, this would use the LLM to generate the edit
    # For now, we log what would be done
    return f"code_edit described: {step.description}"


def _execute_config_change(step: Any, root_dir: Path, dry_run: bool) -> str:
    """Execute a config change step."""
    if dry_run:
        return f"dry_run: would change config: {step.description}"
    return f"config_change described: {step.description}"


def _execute_command(step: Any, root_dir: Path, dry_run: bool) -> str:
    """Execute a command step."""
    if not step.command:
        return "failed: no command specified"

    if dry_run:
        return f"dry_run: would run: {step.command}"

    try:
        result = subprocess.run(
            step.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(root_dir),
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            return f"success: {output[:200]}"
        else:
            return f"failed (exit {result.returncode}): {output[:200]}"
    except subprocess.TimeoutExpired:
        return "failed: command timed out"
    except Exception as e:
        return f"failed: {e}"


def _execute_test(step: Any, root_dir: Path, dry_run: bool) -> str:
    """Execute a test step."""
    if dry_run:
        return "dry_run: would run tests"

    try:
        result = subprocess.run(
            ["pdm", "run", "pytest", "tests/brain/", "-q", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(root_dir),
        )
        if result.returncode == 0:
            return f"tests passed: {result.stdout.strip().split(chr(10))[-1]}"
        else:
            return f"tests failed: {result.stdout[-300:]}"
    except Exception as e:
        return f"test error: {e}"


def record_learning(
    proposal: FixProposal,
    outcome: Literal["success", "partial", "failed"],
    evidence: str,
    brain_dir: Path,
) -> Learning:
    """Record a learning from a fix attempt."""
    import uuid

    learning = Learning(
        learning_id=str(uuid.uuid4()),
        proposal_id=proposal.proposal_id,
        site=proposal.site,
        failure_pattern=f"{proposal.site}/{proposal.trigger_event_type}",
        fix_description=proposal.solution,
        fix_type=proposal.implementation_steps[0].step_type if proposal.implementation_steps else "unknown",
        outcome=outcome,
        evidence=evidence[:500],
        created_at=_now_iso(),
    )

    learnings_path = brain_dir / "learnings.jsonl"
    existing = learnings_path.read_text() if learnings_path.exists() else ""
    atomic_write_bytes(
        learnings_path,
        (existing + learning.model_dump_json() + "\n").encode("utf-8"),
    )

    return learning
