"""Tests for atomic_write_bytes + DigestLock."""

from __future__ import annotations

from survey_agent.brain._lock import DigestLock, atomic_write_bytes


def test_atomic_write_replaces_file(tmp_path):
    target = tmp_path / "x.yaml"
    target.write_text("old")
    atomic_write_bytes(target, b"new")
    assert target.read_text() == "new"


def test_atomic_write_handles_missing_parent(tmp_path):
    target = tmp_path / "subdir" / "x.yaml"
    atomic_write_bytes(target, b"hello")
    assert target.read_text() == "hello"


def test_atomic_write_handles_utf8(tmp_path):
    target = tmp_path / "u.txt"
    atomic_write_bytes(target, "héllo ✨".encode())
    assert target.read_text(encoding="utf-8") == "héllo ✨"


def test_digest_lock_acquires_and_releases(tmp_path):
    lock = DigestLock(tmp_path)
    with lock:
        assert (tmp_path / "digest.lock").exists()
    assert not (tmp_path / "digest.lock").exists()
