"""A backoff floor that survives a lost archive write.

The sampler derives its backoff position from the archive, which is the right
design when the archive is durable: there is one record of what happened and no
side-car state to drift out of sync with it.

Under GitHub Actions the archive is only durable once it is pushed. If a push
fails, the runner is discarded and the record of that attempt goes with it. The
next run reads an archive that has never heard of the failure, finds a clean
ladder, and samples a struggling service at the full cron cadence exactly when
it should be backing off.

This guard is the floor under that case. It is a small file kept outside the
repository, in the Actions cache, holding when the next attempt is due. The
sampler waits for the later of the two: what the archive says, and what the
guard says. Losing the guard is safe, since the archive is then authoritative
again, and losing the archive write is safe, since the guard still holds the
ladder.

It deliberately does not try to preserve the hourly request budget. At a
fifteen minute cron that is four requests an hour against a ceiling of thirty,
so a lost record cannot push the sampler near the limit. The ladder is the part
that protects a service already returning 503, so the ladder is the part kept.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import archive as archive_module

SCHEMA = 1


@dataclass
class GuardState:
    """What a guard file says about the last attempt anyone made."""

    next_attempt_at: datetime | None = None
    consecutive_failures: int = 0
    last_status: int | None = None
    updated_at: datetime | None = None

    def wait_seconds(self, now: datetime) -> float:
        if self.next_attempt_at is None:
            return 0.0
        return max(0.0, (self.next_attempt_at - now).total_seconds())


class Guard:
    """A file holding the backoff floor. Every failure mode reads as no floor.

    A missing file, an unreadable one, or one written by a later version all
    return an empty state rather than raising. The guard can only ever delay an
    attempt, so failing open means falling back to the archive, which is the
    normal source of truth.
    """

    def __init__(self, path: str | None):
        self.path = path

    def __bool__(self) -> bool:
        return bool(self.path)

    def read(self) -> GuardState:
        if not self.path or not os.path.exists(self.path):
            return GuardState()
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return GuardState()
        if not isinstance(payload, dict):
            return GuardState()

        state = GuardState()
        state.next_attempt_at = _read_time(payload.get("next_attempt_at"))
        state.updated_at = _read_time(payload.get("updated_at"))
        failures = payload.get("consecutive_failures")
        if isinstance(failures, int) and not isinstance(failures, bool):
            state.consecutive_failures = failures
        status = payload.get("last_status")
        if isinstance(status, int) and not isinstance(status, bool):
            state.last_status = status
        return state

    def not_before(self, now: datetime | None = None) -> datetime | None:
        """The instant the guard will allow an attempt, or None for no floor."""
        state = self.read()
        if state.next_attempt_at is None:
            return None
        reference = now or datetime.now(timezone.utc)
        if state.next_attempt_at <= reference:
            return None
        return state.next_attempt_at

    def record(
        self,
        *,
        now: datetime,
        ok: bool,
        delay_seconds: float | None,
        consecutive_failures: int,
        http_status: int | None = None,
    ) -> None:
        """Write the floor after an attempt. A success clears it."""
        if not self.path:
            return

        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "updated_at": _write_time(now),
            "consecutive_failures": 0 if ok else consecutive_failures,
            "last_status": http_status,
            "next_attempt_at": None,
        }
        if not ok and delay_seconds:
            payload["next_attempt_at"] = _write_time(now + timedelta(seconds=delay_seconds))

        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = self.path + ".writing"
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def describe(self, now: datetime | None = None) -> str:
        if not self.path:
            return "not in use"
        state = self.read()
        if not os.path.exists(self.path):
            return f"{self.path} (not written yet)"
        wait = state.wait_seconds(now or datetime.now(timezone.utc))
        if wait <= 0:
            return f"{self.path} (no floor, {state.consecutive_failures} consecutive failures)"
        return (
            f"{self.path} (holding for {int(wait)}s after "
            f"{state.consecutive_failures} consecutive failures)"
        )


def _read_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return archive_module.parse_iso(value)
    except ValueError:
        return None


def _write_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
