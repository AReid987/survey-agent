"""Session identity — consistent fingerprint per session, rotate between sessions."""

from __future__ import annotations

from .fingerprint import Fingerprint, FingerprintGenerator


class SessionIdentity:
    """Manages fingerprint consistency within a session.

    A session gets one fingerprint at creation and uses it throughout.
    New sessions get new fingerprints.
    """

    def __init__(self):
        self._generator = FingerprintGenerator()
        self._current: Fingerprint | None = None
        self._session_count = 0

    def new_session(self) -> Fingerprint:
        """Generate a new fingerprint for a new session."""
        self._current = self._generator.generate()
        self._session_count += 1
        return self._current

    @property
    def current(self) -> Fingerprint | None:
        """Get the current session's fingerprint."""
        return self._current

    @property
    def session_count(self) -> int:
        return self._session_count

    def reset(self) -> None:
        """Reset (e.g., after too many sessions from same identity)."""
        self._current = None
        self._generator = FingerprintGenerator()
