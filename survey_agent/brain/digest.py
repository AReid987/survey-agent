"""Autonomous LLM digest job.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §4.3,
§4.4.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from survey_agent.brain._lock import DigestLock, atomic_write_bytes
from survey_agent.brain.schemas import DigestResult, EventRecord, PersonaDenylist
from survey_agent.brain.store import FailureStore

logger = logging.getLogger(__name__)


# Stub; tests monkeypatch this. Production binds to llm_provider.py in Task 9.
def call_llm(prompt: str) -> dict:
    """Stub — real implementation is wired in Task 9 (LLM wiring)."""
    raise NotImplementedError("call_llm must be wired by the integration layer")


def _now_iso() -> str:
    return datetime.now().isoformat()


def _select_allowlisted(hints: list) -> tuple[list, list]:
    """Split persona_hints into (accepted, rejected) by denylist."""
    accepted: list = []
    rejected: list[str] = []
    for h in hints:
        path = h.path if hasattr(h, "path") else h.get("path", "")
        if PersonaDenylist.is_denied(path):
            rejected.append(path)
        else:
            accepted.append(h)
    return accepted, rejected


def _run_id() -> str:
    ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


def run_digest(  # noqa: PLR0915  # function is intentionally long per spec §4.3
    trigger: str,
    store: FailureStore,
    config: Any | None,
    data_dir: Path,
) -> str:
    """Run one digest end-to-end. Returns the run_id.

    Errors are caught and logged; no exception is raised to the caller.
    """
    brain_dir = data_dir / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    run_id = _run_id()
    started_at = _now_iso()
    summary: dict[str, Any] = {
        "run_id": run_id,
        "trigger": trigger,
        "started_at": started_at,
        "finished_at": None,
        "status": "running",
        "rules_added": 0,
        "persona_fields_changed": 0,
        "persona_fields_rejected": 0,
        "rationale_summary": "",
        "error": None,
    }

    try:
        with DigestLock(brain_dir):
            promotions = store.pending_promotions()
            recent: list = []
            for p in promotions:
                recent.extend(
                    store.recent(site=p.site, event_type=p.event_type, limit=p.count)
                )

            # Volume-only triggers fire with no promotions but plenty of
            # recent events. We must collect those too.
            if not recent:
                cur = store.connection.cursor()
                cur.execute(
                    """
                    SELECT payload_json FROM events
                    WHERE created_at > datetime('now', '-7 days')
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (
                        getattr(config, "events_per_digest", 200)
                        if config is not None
                        else 200,
                    ),
                )
                for r in cur.fetchall():
                    try:
                        recent.append(EventRecord.model_validate_json(r["payload_json"]))
                    except Exception:
                        logger.exception("brain: skipping bad event row")

            if not promotions and not recent:
                summary["status"] = "skipped_no_signal"
                summary["finished_at"] = _now_iso()
                _append_run_log(brain_dir, summary)
                return run_id

            # Read current rules
            rules_path = brain_dir / "rules.yaml"
            current_rules: list = []
            if rules_path.exists():
                try:
                    current_rules = yaml.safe_load(rules_path.read_text()) or []
                except yaml.YAMLError:
                    current_rules = []

            # Build prompt + call LLM
            prompt = _build_prompt(promotions, recent, current_rules)
            raw = call_llm(prompt)

            try:
                result = DigestResult.model_validate(raw)
            except Exception as exc:
                summary["status"] = "failed"
                summary["error"] = f"schema_validation: {exc!r}"
                summary["finished_at"] = _now_iso()
                _append_run_log(brain_dir, summary)
                return run_id

            # Denylist enforcement on persona hints
            accepted, rejected = _select_allowlisted(result.persona_hints)
            summary["persona_fields_rejected"] = len(rejected)

            # Snapshot before-state for diff
            profile_path = data_dir / "profile.json"
            before_profile = profile_path.read_text() if profile_path.exists() else ""
            before_rules = rules_path.read_text() if rules_path.exists() else ""
            hints_path = brain_dir / "persona_hints.json"
            before_hints = hints_path.read_text() if hints_path.exists() else ""

            # Atomic write rules.yaml
            all_rules = current_rules + [
                r.model_dump(mode="json") for r in result.new_rules
            ]
            atomic_write_bytes(
                rules_path,
                yaml.safe_dump(all_rules, sort_keys=False).encode("utf-8"),
            )
            summary["rules_added"] = len(result.new_rules)

            # Atomic write persona_hints.json (append-only)
            accepted_dicts = [
                h.model_dump(mode="json") if hasattr(h, "model_dump") else h
                for h in accepted
            ]
            prior_hints: list = []
            if before_hints.strip():
                try:
                    prior_hints = json.loads(before_hints)
                except json.JSONDecodeError:
                    prior_hints = []
            if not isinstance(prior_hints, list):
                prior_hints = []
            atomic_write_bytes(
                hints_path,
                json.dumps(prior_hints + accepted_dicts, indent=2).encode("utf-8"),
            )

            # Apply allowlisted persona hints to profile.json (atomic)
            if accepted_dicts and profile_path.exists():
                profile = json.loads(profile_path.read_text())
                changed = _apply_hints(profile, accepted_dicts)
                summary["persona_fields_changed"] = changed
                if changed:
                    atomic_write_bytes(
                        profile_path,
                        json.dumps(profile, indent=2).encode("utf-8"),
                    )

            # Write diff
            after_profile = profile_path.read_text() if profile_path.exists() else ""
            after_rules = rules_path.read_text() if rules_path.exists() else ""
            after_hints = hints_path.read_text() if hints_path.exists() else ""
            diff_payload = {
                "run_id": run_id,
                "before": {
                    "profile.json": before_profile,
                    "rules.yaml": before_rules,
                    "persona_hints.json": before_hints,
                },
                "after": {
                    "profile.json": after_profile,
                    "rules.yaml": after_rules,
                    "persona_hints.json": after_hints,
                },
            }
            diffs_dir = brain_dir / "diffs"
            diffs_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(
                diffs_dir / f"{run_id}.json",
                json.dumps(diff_payload, indent=2).encode("utf-8"),
            )

            # Mark promotions as digested
            cur = store.connection.cursor()
            cur.execute(
                "UPDATE promotions SET status = 'digested' WHERE status = 'pending_digest'"
            )
            store.connection.commit()

            summary["status"] = "ok"
            summary["rationale_summary"] = result.rationale[:500]
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("brain: digest failed")

    summary["finished_at"] = _now_iso()
    _append_run_log(brain_dir, summary)
    return run_id


def _apply_hints(profile: dict, hints: list[dict]) -> int:
    """Apply hints to profile in-place. Returns count of fields changed."""
    changed = 0
    for h in hints:
        path = h["path"]
        new_value = h["new_value"]
        cur = profile
        parts = path.split(".")
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                return changed
            cur = cur[p]
        cur[parts[-1]] = new_value
        changed += 1
    return changed


def _build_prompt(
    promotions: list,
    recent: list,
    current_rules: list,
) -> str:
    return (
        "You are the survey-agent brain. Analyze observed failures and "
        "propose (a) new rules and (b) persona refinements.\n\n"
        "RULES:\n"
        "- Only propose changes to soft-preference persona fields; never "
        "demographics/employment/income/education/health.\n"
        '- Output strictly JSON with keys: "new_rules", "persona_hints", "rationale".\n\n'
        f"PROMOTIONS ({len(promotions)}):\n{json.dumps([p.model_dump() for p in promotions])}\n\n"
        f"RECENT EVENTS ({len(recent)}):\n{json.dumps([e.model_dump() for e in recent])}\n\n"
        f"CURRENT RULES ({len(current_rules)}):\n{json.dumps(current_rules)}\n"
    )


def _append_run_log(brain_dir: Path, summary: dict) -> None:
    log_path = brain_dir / "digest_runs.jsonl"
    prior = log_path.read_text() if log_path.exists() else ""
    atomic_write_bytes(log_path, (prior + json.dumps(summary) + "\n").encode("utf-8"))
