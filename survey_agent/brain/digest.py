"""Autonomous LLM digest job.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §4.3,
§4.4.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from browser_use.llm.messages import UserMessage

from survey_agent.brain._lock import DigestLock, atomic_write_bytes
from survey_agent.brain.schemas import DigestResult, EventRecord, PersonaDenylist
from survey_agent.brain.store import FailureStore
from survey_agent.config import load_config
from survey_agent.llm_provider import PROVIDERS, get_llm

logger = logging.getLogger(__name__)
__all__ = ["call_llm", "run_digest"]


def call_llm(prompt: str) -> dict:  # noqa: PLR0915
    """Production call_llm: routes through the existing llm_provider.

    Tests monkeypatch this function directly at the module level
    (`survey_agent.brain.digest.call_llm = my_fn`), so the real
    implementation only runs in production digests.

    Returns the parsed JSON dict the digest consumes. Raises any error
    up; the digest's outer try/except logs and marks the run failed.
    """
    cfg = load_config()
    settings = cfg.harness_llm_settings() if hasattr(cfg, "harness_llm_settings") else None
    if settings and settings.get("llm_model"):
        provider = settings["llm_provider"]
        model = settings["llm_model"]
        api_key = settings["llm_api_key"]
        base_url = settings["llm_base_url"]
    else:
        provider = cfg.llm_provider
        model = cfg.llm_model or PROVIDERS[provider]["default_model"]
        api_key = cfg.llm_api_key
        base_url = cfg.llm_base_url or None

    llm = get_llm(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.0,  # deterministic for structured output
    )

    # ainvoke supports both sync and async; we're called from sync digest
    # code path, so we run sync via asyncio.run rather than blocking the
    # event loop. For higher-throughput cases this can move to an async
    # digest path.
    async def _invoke() -> Any:
        return await llm.ainvoke([UserMessage(content=prompt)])

    completion = asyncio.run(_invoke())

    # ChatInvokeCompletion has .completion (the text), not .content
    raw = getattr(completion, "completion", completion)
    if isinstance(raw, str):
        text = raw
    else:
        content = getattr(completion, "content", raw)
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        text = str(content)

    # Strip   ...   blocks
    text = re.sub(r" .*? ", "", text, flags=re.DOTALL).strip()

    # Find first JSON object in the response
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in LLM response: {text[:200]!r}")

    return json.loads(text[start : end + 1])


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
            # Gather research context for better diagnosis
            research_context = None
            try:
                from survey_agent.brain.research import gather_research_context  # noqa: PLC0415
                from survey_agent.config import DATA_DIR as _DATA_DIR  # noqa: PLC0415
                from pathlib import Path as _Path  # noqa: PLC0415
                root_dir = _Path(__file__).resolve().parent.parent.parent
                research_context = gather_research_context(
                    [e.model_dump() for e in recent], _DATA_DIR, root_dir
                )
            except Exception:
                logger.debug("Research context gathering failed (non-blocking)", exc_info=True)

            prompt = _build_prompt(promotions, recent, current_rules, research_context)
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

            # Save fix proposals from digest result
            fix_proposals_saved = 0
            if result.fix_proposals:
                import uuid as _uuid  # noqa: PLC0415
                from survey_agent.brain.schemas import FixProposal, ImplementationStep  # noqa: PLC0415
                from survey_agent.brain.fixer import save_proposal  # noqa: PLC0415

                for fp_raw in result.fix_proposals:
                    try:
                        steps = []
                        for s in fp_raw.get("implementation_steps", []):
                            steps.append(ImplementationStep(
                                step_type=s.get("step_type", "command"),
                                description=s.get("description", ""),
                                target_file=s.get("target_file"),
                                command=s.get("command"),
                                expected_outcome=s.get("expected_outcome", ""),
                            ))
                        proposal = FixProposal(
                            proposal_id=str(_uuid.uuid4()),
                            trigger_event_type=promotions[0].event_type if promotions else "unknown",
                            site=promotions[0].site if promotions else "unknown",
                            diagnosis=fp_raw.get("diagnosis", ""),
                            solution=fp_raw.get("solution", ""),
                            implementation_steps=steps,
                            confidence=float(fp_raw.get("confidence", 0.5)),
                            created_at=_now_iso(),
                        )
                        save_proposal(proposal, brain_dir)
                        fix_proposals_saved += 1
                    except Exception:
                        logger.exception("Failed to save fix proposal")
            summary["fix_proposals_saved"] = fix_proposals_saved

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
    research_context: dict | None = None,
) -> str:
    research_section = ""
    if research_context:
        web = research_context.get("web_results", [])
        code = research_context.get("code_references", [])
        learnings = research_context.get("past_learnings", [])

        if web or code or learnings:
            research_section = "\nRESEARCH CONTEXT:\n"
            if web:
                research_section += "Web search results:\n"
                for w in web[:3]:
                    research_section += f"  - {w.get('title', '?')}: {w.get('snippet', '')[:150]}\n"
            if code:
                research_section += "Relevant code:\n"
                for c in code[:5]:
                    research_section += f"  - {c.get('file', '?')}:{c.get('line', '?')}: {c.get('excerpt', '')[:100]}\n"
            if learnings:
                research_section += "Past learnings:\n"
                for l in learnings[:3]:
                    research_section += f"  - {l.get('fix_description', '?')[:100]} (outcome: {l.get('outcome', '?')})\n"
            research_section += "\n"

    return (
        "You are the survey-agent brain. Analyze observed failures and propose fixes.\n\n"
        "Your job is NOT to skip surveys. Your job is to DIAGNOSE the root cause and "
        "propose CONCRETE IMPLEMENTATION STEPS to fix the problem.\n\n"
        "RULES:\n"
        "- Only propose changes to soft-preference persona fields; never "
        "demographics/employment/income/education/health.\n"
        "- Output STRICTLY valid JSON matching the schema below.\n"
        "- For fix_proposals: be specific about what code to change, what config to set, etc.\n"
        "- NEVER propose 'skip' or 'avoid' as a solution. Always propose a fix.\n\n"
        "OUTPUT SCHEMA:\n"
        "{\n"
        '  "new_rules": [\n'
        "    {\n"
        '      "rule_id": "uuid-v4-string",\n'
        '      "site": "site_slug",\n'
        '      "applies_when": {"event_type": "...", "context": {...}},\n'
        '      "recommends": {"action": "...", "args": {...}},\n'
        '      "confidence_evidence": 3,\n'
        '      "created_at": "ISO-8601",\n'
        '      "source_digest_run": "run_id",\n'
        '      "status": "active",\n'
        '      "supersedes": null\n'
        "    }\n"
        "  ],\n"
        '  "persona_hints": [\n'
        "    {\n"
        '      "path": "category.field_name",\n'
        '      "new_value": "the new value",\n'
        '      "rationale": "why this helps"\n'
        "    }\n"
        "  ],\n"
        '  "fix_proposals": [\n'
        "    {\n"
        '      "diagnosis": "root cause analysis",\n'
        '      "solution": "proposed fix description",\n'
        '      "implementation_steps": [\n'
        "        {\n"
        '          "step_type": "code_edit|config_change|command|test",\n'
        '          "description": "what to do",\n'
        '          "target_file": "path/to/file.py or null",\n'
        '          "command": "shell command or null",\n'
        '          "expected_outcome": "what should happen"\n'
        "        }\n"
        "      ],\n"
        '      "confidence": 0.7\n'
        "    }\n"
        "  ],\n"
        '  "rationale": "overall analysis summary"\n'
        "}\n\n"
        f"PROMOTIONS ({len(promotions)}):\n{json.dumps([p.model_dump() for p in promotions])}\n\n"
        f"RECENT EVENTS ({len(recent)}):\n{json.dumps([e.model_dump() for e in recent])}\n\n"
        f"CURRENT RULES ({len(current_rules)}):\n{json.dumps(current_rules)}\n"
        f"{research_section}"
    )


def _append_run_log(brain_dir: Path, summary: dict) -> None:
    log_path = brain_dir / "digest_runs.jsonl"
    prior = log_path.read_text() if log_path.exists() else ""
    atomic_write_bytes(log_path, (prior + json.dumps(summary) + "\n").encode("utf-8"))
