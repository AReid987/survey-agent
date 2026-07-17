"""File lock + atomic-write helpers for digest safety.

Spec: docs/superpowers/specs/2026-07-16-brain-foundation-design.md §4.3,
Appendix C.
"""

from __future__ import annotations

import contextlib
import fcntl  # noqa: F401  POSIX only
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically replace path's contents with content."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class DigestLock:
    """POSIX advisory lock around `data/brain/digest.lock`.

    On non-POSIX platforms (Windows without flock), falls back to creating
    a sentinel directory — two processes both creating the same dir name
    races, but we treat the failure as "lock held elsewhere".

    Designed as a context manager: `with DigestLock(brain_dir): ...`
    """

    def __init__(self, brain_dir: Path) -> None:
        self._brain_dir = brain_dir
        self._lock_path = brain_dir / "digest.lock"
        self._fd: int | None = None
        self._sentinel_dir: str | None = None

    def __enter__(self) -> DigestLock:
        self._brain_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            try:
                self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None

        # Fallback for non-POSIX or flock-unavailable systems.
        try:
            self._sentinel_dir = tempfile.mkdtemp(prefix="digest.", dir=str(self._brain_dir))
        except OSError as exc:
            raise RuntimeError(f"digest lock held: {exc}") from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
            with contextlib.suppress(OSError):
                os.unlink(str(self._lock_path))
        elif self._sentinel_dir is not None:
            shutil.rmtree(self._sentinel_dir, ignore_errors=True)
            self._sentinel_dir = None
