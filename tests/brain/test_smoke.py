"""End-to-end smoke test of the full brain loop (spec success criteria 1-7)."""

from __future__ import annotations

import json

import yaml

from survey_agent import brain
from survey_agent.brain import autonomy
from survey_agent.brain import digest as digest_mod
from survey_agent.brain.store import FailureStore
from survey_agent.config import BrainConfig


def test_full_loop_record_to_digest(tmp_data_dir, brain_dir, monkeypatch):
    # 1. Setup: profile + config
    (tmp_data_dir / "profile.json").write_text(
        json.dumps(
            {
                "demographics": {"age": 39, "gender": "Male"},
                "shopping": {"preferred_stores": ["Amazon"], "decision_factors": ["Price"]},
            }
        )
    )

    # 2. Capture (Success Criterion 1)
    store = FailureStore(brain_dir)
    monkeypatch.setattr(brain.query._BrainState, "_store", store)
    monkeypatch.setattr(brain.query._BrainState, "_data_dir", tmp_data_dir)

    for i in range(3):
        brain.record(
            site="branded_surveys",
            event_type="captcha_failed",
            context={"challenge_type": "iframe_grid"},
            session_id=f"smoke-{i}",
        )
    rows = brain.query_recent(site="branded_surveys", event_type="captcha_failed", limit=10)
    assert len(rows) == 3, "Capture criterion failed"

    # 3. Promotion works (Success Criterion 2)
    assert len(store.pending_promotions()) >= 1, "Promotion criterion failed"

    # 4. Digest end-to-end (Success Criterion 3)
    monkeypatch.setattr(
        digest_mod,
        "call_llm",
        lambda _prompt: {
            "new_rules": [
                {
                    "rule_id": "7f3a0000-0000-4000-8000-000000000001",
                    "site": "branded_surveys",
                    "applies_when": {"event_type": "captcha_failed", "context_hash": rows[0].context_hash},
                    "recommends": {"action": "switch_captcha_solver", "args": {"solver": "2captcha_api"}},
                    "confidence_evidence": 3,
                    "created_at": "2026-07-16T14:30:22+00:00",
                    "source_digest_run": "smoke",
                    "status": "active",
                    "supersedes": None,
                }
            ],
            "persona_hints": [
                {
                    "path": "shopping.preferred_stores",
                    "new_value": ["Amazon", "Target"],
                    "rationale": "More options help",
                }
            ],
            "rationale": "smoke",
        },
    )
    rid = digest_mod.run_digest("smoke", store, BrainConfig(), tmp_data_dir)
    assert rid, "Digest produced no run_id"

    rules = yaml.safe_load((brain_dir / "rules.yaml").read_text())
    assert len(rules) == 1, "rules.yaml not updated"
    hints = json.loads((brain_dir / "persona_hints.json").read_text())
    assert len(hints) >= 1, "persona_hints.json not updated"

    # 5. Denylist hold (Success Criterion 4)
    profile_after = json.loads((tmp_data_dir / "profile.json").read_text())
    assert profile_after["demographics"]["age"] == 39, "Age mutated!"
    assert profile_after["demographics"]["gender"] == "Male", "Gender mutated!"
    assert profile_after["shopping"]["preferred_stores"] == ["Amazon", "Target"], "Allowlist change missing"

    # 6. Diff + audit (Success Criterion 3d, 3e)
    diff = brain_dir / "diffs" / f"{rid}.json"
    assert diff.exists(), "Diff not written"
    runs = (brain_dir / "digest_runs.jsonl").read_text().strip().splitlines()
    assert any(rid in line for line in runs), "Digest run not recorded"

    # 7. Autonomy triggers (Success Criterion 7)
    cfg = BrainConfig(interval_hours=0)
    store.mark_promotion(rows[0])  # ensure pending
    trig = autonomy.should_trigger_digest(store, cfg, tmp_data_dir)
    assert trig in ("time", "promotion"), f"Unexpected trigger: {trig}"
