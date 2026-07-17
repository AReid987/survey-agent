"""Tests for hash_context + normalize_site."""

from __future__ import annotations

from survey_agent.brain.normalize import hash_context, normalize_site


def test_hash_context_is_stable_across_key_order():
    a = hash_context({"challenge_type": "iframe_grid", "iframe_url": "https://x.example/w"})
    b = hash_context({"iframe_url": "https://x.example/w", "challenge_type": "iframe_grid"})
    assert a == b
    assert len(a) == 64


def test_hash_context_changes_with_value():
    a = hash_context({"k": "v1"})
    b = hash_context({"k": "v2"})
    assert a != b


def test_hash_context_nested_dicts():
    a = hash_context({"outer": {"inner": 1}})
    b = hash_context({"outer": {"inner": 2}})
    assert a != b


def test_hash_context_lists_are_ordered():
    a = hash_context({"x": [1, 2, 3]})
    b = hash_context({"x": [3, 2, 1]})
    assert a != b  # lists are intentionally ordered (not deduped)


def test_normalize_site_lowercases_and_strips():
    assert normalize_site("Branded_Surveys") == "branded_surveys"
    assert normalize_site("  prime opinion ") == "prime opinion"
    assert normalize_site("yougov") == "yougov"


def test_normalize_site_handles_nonascii():
    assert normalize_site("yougov-br") == "yougov-br"
