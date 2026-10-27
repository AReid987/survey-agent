"""Owner notification over iMessage — escalations and payout thresholds.

Two things need to reach the owner while the agent runs unattended:

1. **Escalations** — a question the profile cannot answer. Per docs/adr/0002 the
   agent never clicks "I can't answer"; it texts the owner, stores the reply via
   ``brain.absorb``, and never asks that question again.
2. **Threshold texts** — a site balance crossed the cashout threshold. The owner
   decides whether to withdraw or keep going (docs/adr/0002 replaced automated
   withdrawal with this).

Delivery uses the ``imsg`` CLI (``brew install steipete/tap/imsg``). Every send
is best-effort: a texting failure must never abort a survey run, because the
messages are advisory and the run is the thing earning money.

Duplicate suppression matters more here than anywhere else. A daemon looping
every few minutes would otherwise re-text the same threshold on every cycle, so
sent notifications are journalled to disk and re-sends are refused.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

IMSG_BINARY = "imsg"
SEND_TIMEOUT_SECONDS = 30

# Owner destinations (owner-supplied 2026-08-26).
PRIMARY_NUMBER = "+15125840648"
BACKUP_NUMBER = "+15122701496"


class NotifyError(RuntimeError):
    """Raised only by callers that explicitly demand delivery confirmation."""


@dataclass(frozen=True)
class NotifyResult:
    """Outcome of an attempted send."""

    sent: bool
    destination: str = ""
    detail: str = ""
    suppressed: bool = False

    def __bool__(self) -> bool:
        return self.sent


def imsg_available() -> bool:
    """True when the imsg CLI is installed and on PATH."""
    return shutil.which(IMSG_BINARY) is not None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _journal_path(data_dir: Path) -> Path:
    return data_dir / "notifications.jsonl"


def _already_sent(data_dir: Path, dedupe_key: str) -> bool:
    """Has a notification with this key already gone out?

    The journal is append-only JSONL. A corrupt or unreadable journal returns
    False (we would rather send a duplicate text than silently drop a payout
    notification the owner is waiting on).
    """
    path = _journal_path(data_dir)
    if not dedupe_key or not path.exists():
        return False
    try:
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    if json.loads(stripped).get("dedupe_key") == dedupe_key:
                        return True
                except json.JSONDecodeError:
                    continue
    except OSError:
        logger.exception("notify: could not read journal — allowing send")
    return False


def _journal(data_dir: Path, dedupe_key: str, destination: str, message: str) -> None:
    entry = {
        "sent_at": _now_iso(),
        "dedupe_key": dedupe_key,
        "destination": destination,
        "message": message[:500],
    }
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with open(_journal_path(data_dir), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.exception("notify: failed to journal a sent notification")


def send_text(
    message: str,
    data_dir: Path,
    *,
    destination: str = PRIMARY_NUMBER,
    dedupe_key: str = "",
    fallback_destination: str = BACKUP_NUMBER,
) -> NotifyResult:
    """Text the owner. Never raises.

    Args:
        message: Body to send.
        data_dir: Where the dedupe journal lives (normally ``config.DATA_DIR``).
        destination: Primary phone number.
        dedupe_key: When non-empty, a send is skipped if this key was already
            used. This is what stops a daemon re-texting the same threshold on
            every cycle.
        fallback_destination: Tried once if the primary send fails.

    Returns a NotifyResult; callers that need hard delivery should check it
    rather than assuming success.
    """
    if not message.strip():
        return NotifyResult(False, detail="empty message")

    if dedupe_key and _already_sent(data_dir, dedupe_key):
        logger.info("notify: suppressed duplicate (%s)", dedupe_key)
        return NotifyResult(False, detail="already sent", suppressed=True)

    if not imsg_available():
        logger.warning("notify: imsg not installed — would have sent: %s", message[:120])
        return NotifyResult(False, detail="imsg not installed")

    for target in [destination, fallback_destination]:
        if not target:
            continue
        try:
            proc = subprocess.run(
                [IMSG_BINARY, "send", "--to", target, "--text", message],
                capture_output=True,
                text=True,
                timeout=SEND_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("notify: send to %s failed: %s", target, exc)
            continue

        if proc.returncode == 0:
            _journal(data_dir, dedupe_key, target, message)
            logger.info("notify: sent to %s (%s)", target, dedupe_key or "no key")
            return NotifyResult(True, destination=target)

        logger.warning(
            "notify: imsg exit %d for %s: %s",
            proc.returncode,
            target,
            (proc.stderr or "").strip()[:200],
        )

    return NotifyResult(False, detail="all destinations failed")


# ── Payout thresholds ───────────────────────────────────────────────────


def threshold_crossed(balance: float, thresholds: list[float], last_notified: float = 0.0) -> float | None:
    """Return the highest threshold newly crossed by ``balance``, else None.

    ``last_notified`` is the highest threshold the owner has already been told
    about. Returning the *highest* newly-crossed threshold means a balance that
    jumps past several at once produces one text, not a burst.
    """
    crossed = [t for t in sorted(thresholds) if balance >= t > last_notified]
    return crossed[-1] if crossed else None


def notify_threshold(
    site: str,
    balance: float,
    threshold: float,
    data_dir: Path,
    currency: str = "points",
) -> NotifyResult:
    """Tell the owner a payout threshold was reached and let them decide.

    Deduped on (site, threshold) so a daemon cycling every few minutes texts
    once per threshold rather than once per cycle.
    """
    message = (
        f"survey-agent: {site} balance is {balance:g} {currency} "
        f"(threshold {threshold:g} reached).\n"
        f"Cash out yourself, or reply 'keep going' and I'll text at the next threshold."
    )
    return send_text(
        message,
        data_dir=data_dir,
        dedupe_key=f"threshold:{site}:{threshold:g}",
    )


# ── Question escalation ─────────────────────────────────────────────────


def notify_escalation(
    question: str,
    site: str,
    data_dir: Path,
    options: list[str] | None = None,
    question_hash: str = "",
) -> NotifyResult:
    """Ask the owner a question the profile could not answer.

    Deduped on the question hash, which is what enforces the ask-once rule at
    the delivery layer even if a caller forgets to check ``brain.lookup`` first.
    """
    lines = [f"survey-agent needs an answer ({site}):", "", question.strip()]
    if options:
        lines.append("")
        lines.extend(f"  {i + 1}. {opt}" for i, opt in enumerate(options[:12]))
    lines.append("")
    lines.append("Reply with your answer — I'll remember it and won't ask again.")

    return send_text(
        "\n".join(lines),
        data_dir=data_dir,
        dedupe_key=f"escalation:{question_hash}" if question_hash else "",
    )
