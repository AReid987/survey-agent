"""Tests for the autonomous digest job."""

from __future__ import annotations

import json

import yaml

from survey_agent.brain.schemas import EventRecord
from survey_agent.brain.store import FailureStore


def _write_profile(data_dir, profile):
    (data_dir / "profile.json").write_text(json.dumps(profile))


def _bootstrap_promotions(store: FailureStore) -> None:
    for i in range(3):
        store.append(EventRecord(
            event_id=f"7f3a0000-0000-4000-8000-00000000000{i}",
            site="branded_surveys",
            event_type="captcha_failed",
            context_hash="a" * 64,
            session_id=f"sess-{i}",
            created_at="2026-07-16T14:30:22+00:00",
        ))
    last = EventRecord(
        event_id="7f3a0000-0000-4000-8000-0000000000ff",
        site="branded_surveys",
        event_type="captcha_failed",
        context_hash="a" * 64,
        session_id="sess-final",
        created_at="2026-07-16T14:30:22+00:00",
    )
    store.append(last)
    store.mark_promotion(last)


def test_run_digest_writes_rules_yaml(brain_dir, fake_llm, tmp_data_dir):  # noqa: F811
    _write_profile(tmp_data_dir, {"demographics": {}, "shopping": {}})
    store = FailureStore(brain_dir)
    _bootstrap_promotions(store)

    fake_llm(
        {
            "new_rules": [
                {
                    "rule_id": "7f3a0000-0000-4000-8000-000000000001",
                    "site": "branded_surveys",
                    "applies_when": {
                        "event_type": "captcha_failed",
                        "context_hash": "a" * 64,
                    },
                    "recommends": {
                        "action": "switch_captcha_solver",
                        "args": {"solver": "2captcha_api"},
                    },
                    "confidence_evidence": 3,
                    "created_at": "2026-07-16T14:30:22+00:00",
                    "source_digest_run": "test",
                    "status": "active",
                    "supersedes": None,
                }
            ],
            "persona_hints": [],
            "rationale": "captcha failed 3x — switch to 2captcha",
        }
    )

    from survey_agent.brain.digest import run_digest

    run_id = run_digest(
        trigger="manual_test",
        store=store,
        config=None,
        data_dir=tmp_data_dir,
    )

    rules_data = yaml.safe_load((brain_dir / "rules.yaml").read_text())
    assert len(rules_data) == 1
    assert rules_data[0]["recommends"]["args"]["solver"] == "2captcha_api"

    # audit trail
    runs = (brain_dir / "digest_runs.jsonl").read_text().strip().splitlines()
    assert len(runs) == 1
    parsed = json.loads(runs[0])
    assert parsed["run_id"] == run_id


def test_run_digest_blocks_denylist_persona_changes(brain_dir, fake_llm, tmp_data_dir):
    profile = {
        "demographics": {"age": 39, "gender": "Male"},
        "shopping": {"preferred_stores": ["Amazon"]},
    }
    _write_profile(tmp_data_dir, profile)

    store = FailureStore(brain_dir)
    _bootstrap_promotions(store)

    fake_llm(
        {
            "new_rules": [],
            "persona_hints": [
                {
                    "path": "demographics.age",
                    "new_value": 25,
                    "rationale": "would qualify for more surveys",
                },
                {
                    "path": "demographics.gender",
                    "new_value": "Female",
                    "rationale": "would qualify for more surveys",
                },
                {
                    "path": "shopping.preferred_stores",
                    "new_value": ["Amazon", "Target"],
                    "rationale": "more options help",
                },
            ],
            "rationale": "test",
        }
    )

    from survey_agent.brain.digest import run_digest

    run_id = run_digest(
        trigger="manual_test",
        store=store,
        config=None,
        data_dir=tmp_data_dir,
    )

    after = json.loads((tmp_data_dir / "profile.json").read_text())
    assert after["demographics"]["age"] == 39
    assert after["demographics"]["gender"] == "Male"
    assert after["shopping"]["preferred_stores"] == ["Amazon", "Target"]

    assert run_id, "run_id should be non-empty"
    run = json.loads((brain_dir / "digest_runs.jsonl").read_text().strip().splitlines()[-1])
    assert run["run_id"] == run_id
    assert run["persona_fields_rejected"] >= 2


def test_run_digest_writes_diff_file(brain_dir, fake_llm, tmp_data_dir):
    _write_profile(tmp_data_dir, {"demographics": {}, "shopping": {}})
    store = FailureStore(brain_dir)
    _bootstrap_promotions(store)
    fake_llm({"new_rules": [], "persona_hints": [], "rationale": "noop"})

    from survey_agent.brain.digest import run_digest

    run_id = run_digest(
        trigger="manual_test",
        store=store,
        config=None,
        data_dir=tmp_data_dir,
    )
    diff = brain_dir / "diffs" / f"{run_id}.json"
    assert diff.exists()
    payload = json.loads(diff.read_text())
    assert "before" in payload and "after" in payload


def test_run_digest_schema_validation_failure_is_swallowed(brain_dir, tmp_data_dir):
    _write_profile(tmp_data_dir, {"demographics": {}, "shopping": {}})
    store = FailureStore(brain_dir)
    _bootstrap_promotions(store)

    import survey_agent.brain.digest as digest_mod

    def _bad_call(prompt: str) -> dict:  # noqa: ARG001
        return {"new_rules": "this should be a list"}  # invalid

    original = getattr(digest_mod, "call_llm", None)
    digest_mod.call_llm = _bad_call  # type: ignore[assignment]
    try:
        from survey_agent.brain.digest import run_digest

        run_digest(
            trigger="manual_test",
            store=store,
            config=None,
            data_dir=tmp_data_dir,
        )
    finally:
        digest_mod.call_llm = original  # type: ignore[assignment]

    runs = (brain_dir / "digest_runs.jsonl").read_text().strip().splitlines()
    assert runs
    assert json.loads(runs[0])["status"] == "failed"
