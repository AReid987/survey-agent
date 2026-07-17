"""Frequency-based promotion trigger.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §4.2,
Appendix C.
"""

from __future__ import annotations

from survey_agent.brain.schemas import EventRecord
from survey_agent.brain.store import FailureStore

THRESHOLD = 3  # spec §4.2


def should_promote(event: EventRecord, store: FailureStore) -> bool:
    """Return True if the signature `(site, event_type, context_hash)` has
    been seen >= THRESHOLD times in the trailing 7-day window.

    The actual INSERT into promotions is the responsibility of the caller
    (the orchestrator site-adapter loop, or a post-append hook). This
    function only checks the predicate.
    """
    cur = store.connection.cursor()
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM events
        WHERE site = ? AND event_type = ? AND context_hash = ?
          AND created_at > datetime('now', '-7 days')
        """,
        (event.site, event.event_type, event.context_hash),
    )
    row = cur.fetchone()
    count = row["c"] if hasattr(row, "keys") else row[0]
    return bool(count >= THRESHOLD)
