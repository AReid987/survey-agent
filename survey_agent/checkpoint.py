"""Checkpointing system for survey progress persistence.

Saves progress after each survey page so partial completions survive crashes.
On resume, detects the checkpoint and picks up where it left off.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_PATH = Path(__file__).resolve().parent.parent / "data" / "checkpoint.json"
MAX_RETRIES = 3
CHECKPOINT_TTL_SECONDS = 7200  # 2 hours — discard stale checkpoints


def save(page: int, survey_url: str, current_url: str, profile_context: str = "") -> None:
    """Save a checkpoint for the current survey progress.

    Args:
        page: Current page number in the survey.
        survey_url: The original survey URL.
        current_url: The current page URL (for resume).
        profile_context: Profile string used for answers.
    """
    checkpoint = {
        "survey_url": survey_url,
        "page": page,
        "current_url": current_url,
        "profile_context": profile_context,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "retry_count": 0,  # Reset on each save (new page reached)
    }

    # If there's an existing checkpoint, preserve the retry_count
    existing = _load_raw()
    if existing:
        checkpoint["retry_count"] = existing.get("retry_count", 0)

    _write(checkpoint)
    logger.debug("Checkpoint saved: page %d, URL: %s", page, current_url[:80])


def load(survey_url: str) -> dict | None:
    """Load a checkpoint for a given survey URL.

    Returns None if:
    - No checkpoint exists
    - Checkpoint is for a different survey
    - Checkpoint is stale (older than TTL)
    - Too many retries already exhausted

    Args:
        survey_url: The survey URL to check against.
    """
    checkpoint = _load_raw()
    if not checkpoint:
        return None

    # Wrong survey
    if checkpoint.get("survey_url") != survey_url:
        logger.info("Checkpoint is for a different survey — ignoring")
        # Don't clear it — the new survey will overwrite on its first save
        return None

    # Stale checkpoint
    ts_str = checkpoint.get("timestamp", "")
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > CHECKPOINT_TTL_SECONDS:
                logger.info("Checkpoint is stale (%d sec old) — discarding", int(age))
                clear()
                return None
        except (ValueError, TypeError):
            pass

    # Too many retries
    retries = checkpoint.get("retry_count", 0)
    if retries >= MAX_RETRIES:
        logger.warning("Checkpoint exceeded max retries (%d) — discarding", retries)
        clear()
        return None

    logger.info("Checkpoint found: page %d, URL: %s, age: %ds",
                checkpoint.get("page", 0), checkpoint.get("current_url", "")[:60],
                int(time.time()) - (checkpoint.get("timestamp", time.time()) if isinstance(checkpoint.get("timestamp"), (int, float)) else 0))

    return checkpoint


def increment_retry() -> None:
    """Increment the retry counter on the existing checkpoint."""
    checkpoint = _load_raw()
    if checkpoint:
        checkpoint["retry_count"] = checkpoint.get("retry_count", 0) + 1
        logger.info("Checkpoint retry incremented to %d", checkpoint["retry_count"])
        _write(checkpoint)


def clear() -> None:
    """Clear the checkpoint file."""
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        logger.debug("Checkpoint cleared")


def _load_raw() -> dict | None:
    """Load the raw checkpoint data from disk."""
    if not CHECKPOINT_PATH.exists():
        return None
    try:
        with open(CHECKPOINT_PATH) as f:
            data = json.load(f)
        if not data or not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load checkpoint: %s", e)
        return None


def _write(data: dict) -> None:
    """Write checkpoint data to disk atomically."""
    try:
        CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CHECKPOINT_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(CHECKPOINT_PATH)
    except OSError as e:
        logger.warning("Failed to write checkpoint: %s", e)
