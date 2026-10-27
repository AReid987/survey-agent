"""Ask-once escalation protocol (docs/adr/0002).

A question the profile cannot answer is escalated to the owner by text,
exactly once. The owner's reply is absorbed into the brain so the question
is never asked again. Bot-trap options (e.g. "ROBOT") are declined, never
selected.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from survey_agent.brain import absorb, hash_question, lookup

logger = logging.getLogger(__name__)

ESCALATIONS_FILE = "escalations.jsonl"
BOT_TRAP_TOKENS = ("robot",)


def is_bot_trap_option(text: str) -> bool:
    """A bot-trap option must be declined, never selected."""
    low = (text or "").strip().lower()
    return any(tok in low for tok in BOT_TRAP_TOKENS)


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / ESCALATIONS_FILE


def _load(data_dir: Path) -> list[dict[str, Any]]:
    p = _path(data_dir)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for raw_line in p.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    except OSError:
        logger.exception("escalation: failed to read %s", p)
    return out


def _save_all(data_dir: Path, entries: list[dict[str, Any]]) -> None:
    p = _path(data_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, default=str) + "\n")
    except OSError:
        logger.exception("escalation: failed to write %s", p)


def pending(data_dir: Path) -> list[dict[str, Any]]:
    return [e for e in _load(data_dir) if e.get("status") == "pending"]


def _find_pending(data_dir: Path, qhash: str) -> dict[str, Any] | None:
    for e in _load(data_dir):
        if e.get("question_hash") == qhash and e.get("status") == "pending":
            return e
    return None


def ask_or_remember(
    question: str,
    options: list[str] | None,
    site: str,
    data_dir: Path,
) -> tuple[str | None, bool]:
    """Return (stored_answer_or_None, escalated_flag).

    If the brain already holds an answer, return it and do not escalate.
    Otherwise escalate once (deduped at delivery layer) and record a pending
    entry so we never ask again. Never guesses.
    """
    opts = list(options or [])
    qhash = hash_question(question, opts)

    stored = lookup(question, opts)
    if stored is not None and getattr(stored, "answer", None):
        return stored.answer, False

    # Already asked once and still pending -> do not re-notify.
    if _find_pending(data_dir, qhash) is not None:
        return None, True

    # Decline bot-trap questions entirely: never select, never ask owner
    # (they are traps, not real questions).
    if any(is_bot_trap_option(o) for o in opts):
        return None, False

    try:
        from survey_agent.notify import notify_escalation  # noqa: PLC0415

        notify_escalation(
            question=question,
            site=site or "unknown",
            data_dir=Path(data_dir),
            options=opts,
            question_hash=qhash,
        )
    except Exception:
        logger.exception("escalation: notify_escalation failed")

    entries = _load(data_dir)
    entries.append(
        {
            "question_hash": qhash,
            "question": question,
            "options": opts,
            "site": site or "unknown",
            "asked_at": _now_iso(),
            "status": "pending",
        }
    )
    _save_all(data_dir, entries)
    return None, True


def _now_iso() -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.now(UTC).isoformat()


def _read_owner_replies() -> list[str]:
    """Best-effort fetch of recent owner replies via imsg. Returns text list."""
    try:
        from survey_agent.notify import PRIMARY_NUMBER, imsg_available  # noqa: PLC0415

        if not imsg_available():
            return []
        proc = subprocess.run(
            ["imsg", "history", "--participants", PRIMARY_NUMBER, "--json", "--limit", "50"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Some versions emit NDJSON.
            data = []
            for raw_line2 in proc.stdout.splitlines():
                _rl = raw_line2.strip()
                if not _rl:
                    continue
                try:
                    data.append(json.loads(_rl))
                except json.JSONDecodeError:
                    continue
        replies: list[str] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                # Only inbound (from owner) messages with text.
                if item.get("is_from_me") is True:
                    continue
                text = item.get("text") or item.get("body") or item.get("message")
                if isinstance(text, str) and text.strip():
                    replies.append(text.strip())
        return replies
    except Exception:
        logger.exception("escalation: failed to read owner replies")
        return []


def ingest_owner_replies(data_dir: Path) -> int:
    """Match owner replies to pending escalations and absorb them. Returns count."""
    data_dir = Path(data_dir)
    pend = pending(data_dir)
    if not pend:
        return 0
    replies = _read_owner_replies(data_dir)
    if not replies:
        return 0

    absorbed = 0
    entries = _load(data_dir)
    # Use the most recent reply per pending escalation, in asked_at order.
    reply_idx = 0
    for e in sorted(pend, key=lambda x: x.get("asked_at", "")):
        if reply_idx >= len(replies):
            break
        reply = replies[reply_idx]
        reply_idx += 1
        try:
            absorb(
                question=e["question"],
                answer=reply,
                site=e.get("site", "unknown"),
                options=e.get("options"),
            )
            absorbed += 1
        except Exception:
            logger.exception("escalation: absorb failed")
            continue
        # mark resolved
        for row in entries:
            if row.get("question_hash") == e.get("question_hash") and row.get("status") == "pending":
                row["status"] = "resolved"
                row["resolved_at"] = _now_iso()
                row["answer"] = reply
    _save_all(data_dir, entries)
    return absorbed
