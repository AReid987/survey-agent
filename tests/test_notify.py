"""Tests for owner notification (escalations + payout thresholds).

No real texts are sent: the imsg subprocess call is patched throughout. The
behaviour that matters most here is duplicate suppression — an unattended daemon
that re-texts the same threshold every cycle would be worse than no
notifications at all.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from survey_agent import notify


class _FakeProc:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


@pytest.fixture()
def sent(monkeypatch):
    """Patch imsg to always succeed; return the list of captured argv."""
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):  # noqa: ARG001
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/local/bin/imsg")
    monkeypatch.setattr(notify.subprocess, "run", _run)
    return calls


@pytest.fixture()
def failing_send(monkeypatch):
    """Patch imsg to always fail with a non-zero exit."""
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):  # noqa: ARG001
        calls.append(cmd)
        return _FakeProc(1, stderr="no such contact")

    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/local/bin/imsg")
    monkeypatch.setattr(notify.subprocess, "run", _run)
    return calls


# ── Availability ────────────────────────────────────────────────────────


def test_imsg_available_reflects_path_lookup(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda _: None)
    assert notify.imsg_available() is False
    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/local/bin/imsg")
    assert notify.imsg_available() is True


def test_missing_imsg_returns_unsent_without_raising(monkeypatch, tmp_path):
    """A missing CLI must not abort a survey run."""
    monkeypatch.setattr(notify.shutil, "which", lambda _: None)
    result = notify.send_text("hello", data_dir=tmp_path)
    assert result.sent is False
    assert "not installed" in result.detail
    assert bool(result) is False


# ── Basic sending ───────────────────────────────────────────────────────


def test_send_text_invokes_imsg_with_destination_and_body(sent, tmp_path):
    result = notify.send_text("hello owner", data_dir=tmp_path)
    assert result.sent is True
    assert len(sent) == 1
    assert sent[0][:2] == ["imsg", "send"]
    assert notify.PRIMARY_NUMBER in sent[0]
    assert "hello owner" in sent[0]


def test_empty_message_is_refused(sent, tmp_path):
    assert notify.send_text("   ", data_dir=tmp_path).sent is False
    assert sent == []


def test_send_failure_falls_back_to_backup_number(failing_send, tmp_path):
    result = notify.send_text("hi", data_dir=tmp_path)
    assert result.sent is False
    targets = [c[c.index("--to") + 1] for c in failing_send]
    assert targets == [notify.PRIMARY_NUMBER, notify.BACKUP_NUMBER]


def test_timeout_is_swallowed_and_reported(monkeypatch, tmp_path):
    def _run(cmd, **kwargs):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd="imsg", timeout=30)

    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/local/bin/imsg")
    monkeypatch.setattr(notify.subprocess, "run", _run)

    result = notify.send_text("hi", data_dir=tmp_path)
    assert result.sent is False  # did not raise


# ── Duplicate suppression ───────────────────────────────────────────────


def test_dedupe_key_blocks_a_second_identical_send(sent, tmp_path):
    first = notify.send_text("threshold hit", data_dir=tmp_path, dedupe_key="threshold:x:100")
    second = notify.send_text("threshold hit", data_dir=tmp_path, dedupe_key="threshold:x:100")

    assert first.sent is True
    assert second.sent is False
    assert second.suppressed is True
    assert len(sent) == 1  # only one real send


def test_different_dedupe_keys_both_send(sent, tmp_path):
    notify.send_text("a", data_dir=tmp_path, dedupe_key="threshold:x:100")
    notify.send_text("b", data_dir=tmp_path, dedupe_key="threshold:x:200")
    assert len(sent) == 2


def test_no_dedupe_key_means_always_send(sent, tmp_path):
    notify.send_text("a", data_dir=tmp_path)
    notify.send_text("a", data_dir=tmp_path)
    assert len(sent) == 2


@pytest.mark.usefixtures("failing_send")
def test_failed_sends_are_not_journalled_so_they_can_retry(tmp_path):
    """A send that never arrived must not be suppressed on the next attempt."""
    notify.send_text("hi", data_dir=tmp_path, dedupe_key="k")
    journal = tmp_path / "notifications.jsonl"
    assert not journal.exists() or journal.read_text().strip() == ""


@pytest.mark.usefixtures("sent")
def test_journal_records_what_was_sent(tmp_path):
    notify.send_text("payout time", data_dir=tmp_path, dedupe_key="threshold:branded:500")
    entries = [json.loads(line) for line in (tmp_path / "notifications.jsonl").read_text().strip().splitlines()]
    assert len(entries) == 1
    assert entries[0]["dedupe_key"] == "threshold:branded:500"
    assert entries[0]["destination"] == notify.PRIMARY_NUMBER


@pytest.mark.usefixtures("sent")
def test_corrupt_journal_does_not_block_sending(tmp_path):
    (tmp_path / "notifications.jsonl").write_text("{not json\n\n")
    result = notify.send_text("hi", data_dir=tmp_path, dedupe_key="k")
    assert result.sent is True


# ── Threshold arithmetic ────────────────────────────────────────────────


def test_threshold_crossed_returns_none_below_all_thresholds():
    assert notify.threshold_crossed(50, [100, 500, 1000]) is None


def test_threshold_crossed_returns_the_crossed_threshold():
    assert notify.threshold_crossed(150, [100, 500, 1000]) == 100


def test_threshold_crossed_returns_highest_when_several_are_passed_at_once():
    """One text for a big jump, not three."""
    assert notify.threshold_crossed(1200, [100, 500, 1000]) == 1000


def test_threshold_crossed_respects_already_notified():
    assert notify.threshold_crossed(150, [100, 500], last_notified=100) is None
    assert notify.threshold_crossed(600, [100, 500], last_notified=100) == 500


def test_threshold_crossed_is_inclusive_at_the_boundary():
    assert notify.threshold_crossed(100, [100]) == 100


def test_threshold_crossed_handles_empty_threshold_list():
    assert notify.threshold_crossed(9999, []) is None


# ── Message composition ─────────────────────────────────────────────────


def test_notify_threshold_mentions_site_balance_and_the_choice(sent, tmp_path):
    notify.notify_threshold("branded-surveys", 520, 500, data_dir=tmp_path)
    body = sent[0][sent[0].index("--text") + 1]
    assert "branded-surveys" in body
    assert "520" in body
    assert "500" in body
    assert "keep going" in body.lower()


def test_notify_threshold_dedupes_per_site_and_threshold(sent, tmp_path):
    notify.notify_threshold("branded-surveys", 520, 500, data_dir=tmp_path)
    notify.notify_threshold("branded-surveys", 530, 500, data_dir=tmp_path)
    notify.notify_threshold("prime-opinion", 520, 500, data_dir=tmp_path)
    assert len(sent) == 2  # same site+threshold suppressed; other site sent


def test_notify_escalation_includes_question_and_options(sent, tmp_path):
    notify.notify_escalation(
        "Which streaming services do you pay for?",
        site="branded-surveys",
        data_dir=tmp_path,
        options=["Netflix", "Hulu", "Disney+"],
        question_hash="a" * 64,
    )
    body = sent[0][sent[0].index("--text") + 1]
    assert "Which streaming services do you pay for?" in body
    assert "Netflix" in body
    assert "Disney+" in body
    assert "won't ask again" in body


def test_notify_escalation_dedupes_on_question_hash(sent, tmp_path):
    for _ in range(3):
        notify.notify_escalation(
            "Same question?",
            site="branded-surveys",
            data_dir=tmp_path,
            question_hash="b" * 64,
        )
    assert len(sent) == 1  # the ask-once rule, enforced at the delivery layer


@pytest.mark.usefixtures("sent")
def test_notify_escalation_without_options_still_sends(tmp_path):
    result = notify.notify_escalation(
        "Free text question?",
        site="branded-surveys",
        data_dir=tmp_path,
        question_hash="c" * 64,
    )
    assert result.sent is True
