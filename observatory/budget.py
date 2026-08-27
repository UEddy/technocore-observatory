"""Request budget and single-worker enforcement.

Two hard constraints from the spec are enforced here:

  * an absolute ceiling of 30 requests per hour across all endpoints
  * never run concurrent requests, one sequential worker, always

The budget window is derived from the NDJSON archive rather than from a
side-car state file, so the archive is the single auditable record of every
request this tool has made.
"""

from __future__ import annotations

import errno
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import archive

HARD_CEILING_PER_HOUR = 30
WINDOW_SECONDS = 3600.0
# Older than this and a lock file is assumed to be from a crashed run.
STALE_LOCK_SECONDS = 3 * 3600.0


def attempt_times(records: Iterable[dict[str, Any]]) -> list[datetime]:
    """Timestamps of every request attempt in the given records, sorted."""
    stamps: list[datetime] = []
    for record in records:
        raw = record.get("fetched_at")
        if not isinstance(raw, str):
            continue
        try:
            stamps.append(archive.parse_iso(raw))
        except ValueError:
            continue
    stamps.sort()
    return stamps


class Budget:
    """Sliding one hour window over recorded request attempts."""

    def __init__(self, archive_path: str, limit_per_hour: int = HARD_CEILING_PER_HOUR):
        if limit_per_hour < 1:
            raise ValueError("limit_per_hour must be at least 1")
        # The configured limit can lower the ceiling but never raise it.
        self.limit = min(int(limit_per_hour), HARD_CEILING_PER_HOUR)
        self.archive_path = archive_path

    def _window(self, now: datetime) -> list[datetime]:
        cutoff = now - timedelta(seconds=WINDOW_SECONDS)
        # A window can hold at most `limit` attempts, so a modest tail is
        # always enough. The margin covers clock skew and hand-edited files.
        tail = archive.read_tail(self.archive_path, limit=max(200, self.limit * 4))
        return [stamp for stamp in attempt_times(tail) if stamp > cutoff]

    def used(self, now: datetime | None = None) -> int:
        return len(self._window(now or datetime.now(timezone.utc)))

    def remaining(self, now: datetime | None = None) -> int:
        return max(0, self.limit - self.used(now))

    def next_allowed_at(self, now: datetime | None = None) -> datetime:
        """When the next request may be made without breaching the ceiling."""
        now = now or datetime.now(timezone.utc)
        window = self._window(now)
        if len(window) < self.limit:
            return now
        # The oldest attempt in the window has to age out first.
        oldest_kept = window[-self.limit]
        return oldest_kept + timedelta(seconds=WINDOW_SECONDS)


class LockHeld(Exception):
    """Raised when another sampler worker holds the lock."""


class WorkerLock:
    """Exclusive lock so only one sequential worker ever runs.

    Context manager. Uses O_CREAT | O_EXCL, which is atomic on Windows and
    POSIX alike, so no third party library is needed.
    """

    def __init__(self, path: str, stale_after: float = STALE_LOCK_SECONDS):
        self.path = path
        self.stale_after = stale_after
        self._acquired = False

    def _try_create(self) -> bool:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"pid {os.getpid()} at {archive.utc_now_iso()}\n")
        return True

    def acquire(self) -> None:
        if self._try_create():
            self._acquired = True
            return
        try:
            age = os.path.getmtime(self.path)
        except OSError:
            age = None
        if age is not None:
            import time as _time

            if _time.time() - age > self.stale_after:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                if self._try_create():
                    self._acquired = True
                    return
        raise LockHeld(f"another worker holds {self.path}")

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self._acquired = False

    def __enter__(self) -> "WorkerLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
