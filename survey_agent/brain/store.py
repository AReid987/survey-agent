"""FailureStore: append-only JSONL + SQLite WAL mirror.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §3.1, §4.2,
Appendix C.

Concurrency:
- JSONL appends use POSIX O_APPEND semantics; serialized events must stay
  under PIPE_BUF (4 KiB Linux, 512 B macOS) for atomicity. FailureStore
  raises SerializationTooLargeError if a single event exceeds this; the
  caller (digest task) can choose to split or fall back.
- SQLite is opened in WAL mode with a single connection per store.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from survey_agent.brain.schemas import EventRecord, Promotion

logger = logging.getLogger(__name__)

# PIPE_BUF is typically 4096 on Linux, 512 on macOS. We use the more
# conservative 512 to be portable.
PIPE_BUF_BYTES = 512


class SerializationTooLargeError(RuntimeError):
    """A single event serialized above PIPE_BUF_BYTES — caller must split.

    Per spec §3.1 / Appendix C, the caller decides whether to split into
    multiple smaller events or fall back to per-session JSONL. There is
    no per-process advisory lock; O_APPEND on POSIX is the actual
    concurrency guarantee.
    """


class FailureStore:
    """Append + query interface for the medium-term (SQLite) and short-term (JSONL) stores."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = data_dir / "events.jsonl"
        self._db_path = data_dir / "brain.db"
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _create_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                site TEXT NOT NULL,
                event_type TEXT NOT NULL,
                context_hash TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_site_type_created
                ON events(site, event_type, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_events_signature
                ON events(site, event_type, context_hash, created_at DESC);

            CREATE TABLE IF NOT EXISTS promotions (
                site TEXT NOT NULL,
                event_type TEXT NOT NULL,
                context_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending_digest','digested')),
                count INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (site, event_type, context_hash, status)
            );
            """
        )
        self._conn.commit()

    def append(self, event: EventRecord) -> None:
        """Append an event to JSONL (one OS write call) and SQLite (one transaction).

        Per spec §4.4, this NEVER raises to the caller; it logs and falls back
        to JSONL-only if SQLite is unhealthy.
        """
        payload = event.model_dump_json()
        encoded = (payload + "\n").encode("utf-8")

        if len(encoded) > PIPE_BUF_BYTES:
            raise SerializationTooLargeError(
                f"event serialized to {len(encoded)} bytes; PIPE_BUF={PIPE_BUF_BYTES}"
            )

        # 1) JSONL append — one OS write call, atomic with O_APPEND.
        try:
            with open(self._jsonl_path, "ab") as f:
                f.write(encoded)
        except OSError:
            logger.exception("brain: failed to write events.jsonl")
            return  # JSONL failed — no point trying SQLite either

        # 2) SQLite mirror
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO events
                    (event_id, site, event_type, context_hash, session_id, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.site,
                    event.event_type,
                    event.context_hash,
                    event.session_id,
                    event.created_at,
                    payload,
                ),
            )
            self._conn.commit()
        except sqlite3.Error:
            logger.exception("brain: failed to insert into brain.db — JSONL kept")
            # Spec §5: SQLite failure falls back to JSONL-only; survey continues.

    def recent(
        self,
        site: str,
        event_type: str | None,
        limit: int,
    ) -> list[EventRecord]:
        cur = self._conn.cursor()
        # Stable secondary sort by event_id so two events sharing the same
        # created_at still have a deterministic ordering (newest insertion
        # first by lexical ordering of UUIDv4).
        if event_type is None:
            cur.execute(
                """
                SELECT payload_json FROM events
                WHERE site = ?
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                (site, limit),
            )
        else:
            cur.execute(
                """
                SELECT payload_json FROM events
                WHERE site = ? AND event_type = ?
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                (site, event_type, limit),
            )
        rows = cur.fetchall()
        out: list[EventRecord] = []
        for r in rows:
            try:
                out.append(EventRecord.model_validate_json(r["payload_json"]))
            except Exception:
                logger.exception("brain: bad row in events table, skipping")
        return out

    def mark_promotion(self, event: EventRecord) -> bool:
        """Insert/update pending-digest promotion row.

        Returns True if this call caused a NEW pending promotion row to
        exist (first crossing of the threshold). Returns False on re-occurrence
        (counter increment only).
        """
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS c FROM events
            WHERE site = ? AND event_type = ? AND context_hash = ?
              AND created_at > datetime('now', '-7 days')
            """,
            (event.site, event.event_type, event.context_hash),
        )
        count = cur.fetchone()["c"]

        if count < 3:
            return False

        cur.execute(
            """
            SELECT 1 FROM promotions
            WHERE site = ? AND event_type = ? AND context_hash = ?
              AND status = 'pending_digest'
            """,
            (event.site, event.event_type, event.context_hash),
        )
        already_pending = cur.fetchone() is not None

        cur.execute(
            """
            INSERT INTO promotions (site, event_type, context_hash, status, count, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, 'pending_digest', ?, ?, ?)
            ON CONFLICT (site, event_type, context_hash, status) DO UPDATE
              SET count = excluded.count, last_seen_at = excluded.last_seen_at
            """,
            (
                event.site,
                event.event_type,
                event.context_hash,
                count,
                event.created_at,
                event.created_at,
            ),
        )
        self._conn.commit()
        return not already_pending

    def pending_promotions(self) -> list[Promotion]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT site, event_type, context_hash, count, first_seen_at, last_seen_at
            FROM promotions WHERE status = 'pending_digest'
            ORDER BY first_seen_at ASC
            """
        )
        rows = cur.fetchall()
        return [
            Promotion(
                site=r["site"],
                event_type=r["event_type"],
                context_hash=r["context_hash"],
                status="pending_digest",
                count=r["count"],
                first_seen_at=r["first_seen_at"],
                last_seen_at=r["last_seen_at"],
            )
            for r in rows
        ]
