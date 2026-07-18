"""Trigger logic for the autonomous digest.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §4.3.

Three trigger sources:
- time:    since last digest > interval_hours
- volume:  since last digest > events_per_digest new events
- promotion: any pending promotion row, throttled to once per hour

Failures within the last 5 minutes back off ALL triggers to avoid
hitting a sick LLM repeatedly on every session start.

If more than one fires, the first-match wins (priority: promotion > volume > time).
Digest runs in the last 24h are capped by `BrainConfig.max_per_day`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from survey_agent.brain._lock import atomic_write_bytes
from survey_agent.brain.store import FailureStore

if TYPE_CHECKING:
    from survey_agent.config import BrainConfig

logger = logging.getLogger(__name__)

PROMOTION_THROTTLE_HOURS = 1
FAILURE_BACKOFF_MINUTES = 5


def _read_last_digest(brain_dir: Path) -> datetime | None:
    marker = brain_dir / "last_digest.txt"
    if not marker.exists():
        return None
    try:
        return datetime.fromisoformat(marker.read_text().strip().replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None


def _write_last_digest(brain_dir: Path, ts: datetime) -> None:
    atomic_write_bytes(brain_dir / "last_digest.txt", ts.isoformat().encode("utf-8"))


def _event_count_since(store: FailureStore, since_iso: str) -> int:
    cur = store.connection.cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM events WHERE created_at > ?",
        (since_iso,),
    )
    row = cur.fetchone()
    return int(row["c"] if hasattr(row, "keys") else row[0])


def _digests_in_last_24h(brain_dir: Path, tail_lines: int = 500) -> int:
    """Count digest runs in the last 24h, scanning only the tail of the log.

    With BrainConfig.max_per_day=4 default, hitting the tail_lines cap is
    impossible in normal use; the cap bounds O(n) on every trigger check.
    """
    runs_path = brain_dir / "digest_runs.jsonl"
    if not runs_path.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    try:
        with open(runs_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = b""
            pos = size
            while pos > 0:
                read_size = min(64 * 1024, pos)
                pos -= read_size
                f.seek(pos)
                block = f.read(read_size) + block
            recent_lines = block.splitlines()[-tail_lines:]
    except OSError:
        return 0
    count = 0
    for line_bytes in recent_lines:
        try:
            entry = json.loads(line_bytes.decode("utf-8"))
        except Exception:
            continue
        started_raw = entry.get("started_at")
        if not started_raw:
            continue
        try:
            started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if started > cutoff and entry.get("status") in ("ok", "skipped_no_signal"):
            count += 1
    return count


def _last_run_status_was_failed_within(brain_dir: Path, minutes: int) -> bool:
    """Read the very last line of digest_runs.jsonl (cheap). If it was a
    failure within the last `minutes`, return True so the trigger backs off.
    """
    runs_path = brain_dir / "digest_runs.jsonl"
    if not runs_path.exists():
        return False
    try:
        with open(runs_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            last_byte = b""
            while pos > 0:
                pos -= 1
                f.seek(pos)
                byte = f.read(1)
                if byte == b"\n" and last_byte:
                    break
                last_byte = byte + last_byte
            if not last_byte:
                return False
            entry = json.loads(last_byte.decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if entry.get("status") != "failed":
        return False
    started_raw = entry.get("started_at")
    if not started_raw:
        return False
    try:
        started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (datetime.now(UTC) - started) < timedelta(minutes=minutes)


def should_trigger_digest(
    store: FailureStore,
    config: BrainConfig,
    data_dir: Path,
) -> str | None:
    brain_dir = data_dir / "brain"
    last = _read_last_digest(brain_dir)

    if last is not None and _last_run_status_was_failed_within(brain_dir, FAILURE_BACKOFF_MINUTES):
        return None

    if store.pending_promotions() and (
        last is None or (datetime.now(UTC) - last) > timedelta(hours=PROMOTION_THROTTLE_HOURS)
    ):
        return "promotion"

    if last is not None:
        recent_count = _event_count_since(store, last.isoformat())
        if recent_count >= config.events_per_digest and _digests_in_last_24h(brain_dir) < config.max_per_day:
            return "volume"

    if last is not None and (datetime.now(UTC) - last) >= timedelta(hours=config.interval_hours) and _digests_in_last_24h(brain_dir) < config.max_per_day:
        return "time"

    return None


def trigger_if_due(
    store: FailureStore,
    config: BrainConfig,
    data_dir: Path,
) -> str | None:
    """If a trigger fires, run the digest synchronously (caller decides async).
    Returns the run_id, or None if no trigger fired.
    """
    trigger = should_trigger_digest(store, config, data_dir)
    if trigger is None:
        return None

    from survey_agent.brain.digest import run_digest  # noqa: PLC0415

    run_id = run_digest(
        trigger=trigger,
        store=store,
        config=config,
        data_dir=data_dir,
    )
    _write_last_digest(data_dir / "brain", datetime.now(UTC))
    return run_id
