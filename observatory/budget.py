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
import json
import os
import platform
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import archive

HARD_CEILING_PER_HOUR = 30
WINDOW_SECONDS = 3600.0
# A held lock this quiet is treated as abandoned. One attempt has a 30 second
# transport timeout and a running loop touches its lock every cycle, so nothing
# healthy is ever silent this long.
STALE_LOCK_SECONDS = 900.0


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

    def __init__(self, store: archive.Archive, limit_per_hour: int = HARD_CEILING_PER_HOUR):
        if limit_per_hour < 1:
            raise ValueError("limit_per_hour must be at least 1")
        # The configured limit can lower the ceiling but never raise it.
        self.limit = min(int(limit_per_hour), HARD_CEILING_PER_HOUR)
        self.store = store

    def _window(self, now: datetime) -> list[datetime]:
        cutoff = now - timedelta(seconds=WINDOW_SECONDS)
        # Every archived record is one request attempt, so the newest
        # `limit` of them decide the window on their own. Reading double that
        # covers clock skew and hand-edited files, and it is a bounded tail
        # seek rather than a walk over the whole archive. The seek crosses a
        # month boundary on its own when the newest file is short.
        tail = self.store.read_tail(limit=max(50, self.limit * 2))
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


def pid_is_running(pid: int | None) -> bool:
    """Best effort liveness check for a process id.

    On Windows, os.kill is not a probe: for any signal other than the console
    events it calls TerminateProcess, so it would kill the very worker it was
    asked about. OpenProcess is used instead.

    Errs toward reporting True. A worker that might still be alive keeps its
    lock, and the age based timeout is what breaks a genuine deadlock.
    """
    if pid is None or pid <= 0:
        return False

    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        process_query_limited_information = 0x1000
        error_access_denied = 5
        still_active = 259

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            # Access denied means the process exists and belongs to another
            # user. Any other error means it is gone.
            return ctypes.get_last_error() == error_access_denied
        try:
            code = ctypes.wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                # A process that exited with code 259 reads as alive. The
                # timeout below covers that rare case.
                return code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


@dataclass
class LockInfo:
    """What a lock file says about the worker holding it."""

    pid: int | None = None
    started: str | None = None
    host: str | None = None
    age_seconds: float = 0.0
    raw: str = ""


class LockHeld(Exception):
    """Raised when another sampler worker holds the lock."""

    def __init__(self, message: str, info: "LockInfo | None" = None):
        super().__init__(message)
        self.info = info


class WorkerLock:
    """Exclusive lock so only one sequential worker ever runs.

    Context manager. Creation is atomic on Windows and POSIX alike via
    O_CREAT | O_EXCL, so no third party library is needed.

    A crash must not stop collection forever, so a held lock is broken when
    either test says the holder is gone:

      * the recorded pid is not running, or is this process finding its own
        leftover lock
      * the lock has not been touched for `stale_after` seconds

    Long runs keep the second test honest by calling `heartbeat` every cycle,
    so an age based takeover only ever hits a worker that stopped working.
    """

    def __init__(self, path: str, stale_after: float = STALE_LOCK_SECONDS):
        self.path = path
        self.stale_after = stale_after
        self._acquired = False
        self.broke_stale_lock: str | None = None

    def read_info(self) -> LockInfo | None:
        """Read the holder details. None if the lock file is not there."""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return None
        except OSError:
            return LockInfo(raw="")

        info = LockInfo(raw=raw)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            pid = payload.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool):
                info.pid = pid
            started = payload.get("started")
            if isinstance(started, str):
                info.started = started
            host = payload.get("host")
            if isinstance(host, str):
                info.host = host
        try:
            info.age_seconds = max(0.0, time.time() - os.path.getmtime(self.path))
        except OSError:
            info.age_seconds = 0.0
        return info

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
        payload = {
            "pid": os.getpid(),
            "started": archive.utc_now_iso(),
            "host": platform.node(),
        }
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload) + "\n")
        return True

    def _stale_reason(self, info: LockInfo) -> str | None:
        """Why the existing lock can be broken, or None to leave it alone."""
        if info.pid is not None:
            # This process holding the lock twice is a real conflict, not a
            # leftover, so our own pid is never grounds for a takeover.
            # A pid recorded on another machine says nothing about this one.
            same_host = info.host is None or info.host == platform.node()
            if same_host and info.pid != os.getpid() and not pid_is_running(info.pid):
                return f"holder pid {info.pid} is not running"
        if info.age_seconds > self.stale_after:
            holder = f"pid {info.pid}" if info.pid is not None else "unknown pid"
            return (
                f"lock untouched for {int(info.age_seconds)}s "
                f"(limit {int(self.stale_after)}s, holder {holder})"
            )
        if info.pid is None and not info.raw.strip():
            # An empty file means a worker died between create and write.
            return "lock file is empty"
        return None

    def acquire(self) -> None:
        if self._try_create():
            self._acquired = True
            return

        info = self.read_info()
        if info is None:
            # The holder released it between the two calls.
            if self._try_create():
                self._acquired = True
                return
            raise LockHeld(f"another worker holds {self.path}")

        reason = self._stale_reason(info)
        if reason is None:
            raise LockHeld(
                f"another worker holds {self.path}: pid {info.pid}, "
                f"started {info.started}, touched {int(info.age_seconds)}s ago",
                info,
            )

        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise LockHeld(f"cannot clear stale lock {self.path}: {exc}", info) from exc

        if not self._try_create():
            # Another worker got there first. It is alive, so it wins.
            raise LockHeld(f"another worker took {self.path} while it was being cleared", info)

        self._acquired = True
        self.broke_stale_lock = reason

    def heartbeat(self) -> None:
        """Mark the lock as still held. Cheap enough to call every cycle."""
        if not self._acquired:
            return
        try:
            os.utime(self.path, None)
        except OSError:
            pass

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
