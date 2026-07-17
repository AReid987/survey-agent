"""BrainConfig wiring into AppConfig."""

from __future__ import annotations

from survey_agent.config import AppConfig, load_config


def test_brain_config_defaults():
    cfg = AppConfig()
    assert cfg.brain.interval_hours == 6
    assert cfg.brain.events_per_digest == 200
    assert cfg.brain.max_per_day == 4
    assert cfg.brain.promotion_threshold == 3


def test_brain_config_loads_from_yaml(tmp_data_dir):
    cfg_path = tmp_data_dir / "config.yaml"
    cfg_path.write_text(
        """
brain:
  interval_hours: 12
  events_per_digest: 500
  max_per_day: 8
  promotion_threshold: 5
"""
    )
    cfg = load_config()
    assert cfg.brain.interval_hours == 12
    assert cfg.brain.events_per_digest == 500
    assert cfg.brain.max_per_day == 8
    assert cfg.brain.promotion_threshold == 5


def test_brain_config_partial_yaml_uses_defaults(tmp_data_dir):
    cfg_path = tmp_data_dir / "config.yaml"
    cfg_path.write_text("brain:\n  interval_hours: 2\n")
    cfg = load_config()
    assert cfg.brain.interval_hours == 2
    assert cfg.brain.events_per_digest == 200  # default
    assert cfg.brain.max_per_day == 4  # default
