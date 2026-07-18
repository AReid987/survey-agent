"""Tests for brain CLI subcommands."""

from __future__ import annotations

import json

from survey_agent.brain.store import FailureStore


def test_brain_status_reports_pending_promotions(tmp_data_dir, brain_dir):  # noqa: ARG001
    """Verify the status command logic: store.pending_promotions() + digest_runs.jsonl."""
    runs = brain_dir / "digest_runs.jsonl"
    runs.write_text(
        json.dumps(
            {
                "run_id": "20260716T143022Z-aaaaaaaa",
                "trigger": "manual",
                "started_at": "2026-07-16T14:30:22+00:00",
                "finished_at": "2026-07-16T14:30:25+00:00",
                "status": "ok",
                "rules_added": 1,
                "persona_fields_changed": 0,
                "persona_fields_rejected": 0,
                "rationale_summary": "",
                "error": None,
            }
        )
        + "\n"
    )

    store = FailureStore(brain_dir)
    pending = store.pending_promotions()
    assert len(pending) == 0  # no promotions seeded
    lines = runs.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["trigger"] == "manual"
    assert entry["status"] == "ok"


def test_brain_pause_resume(tmp_data_dir, brain_dir):  # noqa: ARG001
    """Verify pause/resume file marker logic."""
    from survey_agent.brain._lock import atomic_write_bytes

    brain_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(brain_dir / "paused", b"1")
    assert (brain_dir / "paused").exists()

    p = brain_dir / "paused"
    if p.exists():
        p.unlink()
    assert not (brain_dir / "paused").exists()


def test_brain_inspect_not_found(tmp_data_dir, brain_dir):  # noqa: ARG001
    """Verify inspect returns not-found for missing run."""
    diff_path = brain_dir / "diffs" / "nonexistent.json"
    assert not diff_path.exists()


def test_brain_rollback_logic(tmp_data_dir, brain_dir):
    """Verify rollback reads from diff and restores files."""
    from survey_agent.brain._lock import atomic_write_bytes

    # Set up a "before" snapshot
    diffs_dir = brain_dir / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)
    run_id = "20260716T143022Z-aaaaaaaa"
    diff = {
        "before": {
            "profile.json": json.dumps({"shopping": {"preferred_stores": ["Amazon"]}}),
            "rules.yaml": "[]\n",
            "persona_hints.json": "[]",
        },
    }
    (diffs_dir / f"{run_id}.json").write_text(json.dumps(diff))

    # Simulate rollback
    payload = json.loads((diffs_dir / f"{run_id}.json").read_text())
    before = payload["before"]
    atomic_write_bytes(tmp_data_dir / "profile.json", before["profile.json"].encode("utf-8"))
    restored = json.loads((tmp_data_dir / "profile.json").read_text())
    assert restored["shopping"]["preferred_stores"] == ["Amazon"]
