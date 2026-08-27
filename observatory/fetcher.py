"""The sampler itself: one sequential worker, one request per interval.

Everything the sampler needs to know about its own past behaviour is derived
from the NDJSON archive: how many requests it has made in the last hour, and
where it currently sits on the backoff ladder. There is no separate state file
to drift out of sync with the record, and no way to make a request that is not
written down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from . import archive, backoff, budget
from .guard import Guard
from .transport import Response

DEFAULT_URL = "https://technocore.chat/rooms"

# 15 minutes, not the 5 the spec first proposed. The whole 50 room window has
# idle times spanning about a minute, so it turns over roughly once a minute:
# every cadence the 30/hour ceiling permits, 2 minutes included, undersamples
# the churn by an order of magnitude and can only ever report a lower bound.
# What ships first is the exhaustion projection and the engagement series, and
# those aggregates move slowly enough that 4 samples an hour serve them fully.
# Meanwhile 15 minutes is a third of the load on a service that is already
# returning 503, a third of the commits and repo growth for an archive that is
# itself the deliverable, and a cadence a scheduled runner can actually keep.
DEFAULT_INTERVAL_SECONDS = 900.0
MIN_INTERVAL_SECONDS = 120.0  # the 30/hour ceiling leaves no room to go tighter


class Transport(Protocol):
    source: str

    def get(self, url: str) -> Response: ...


def _record_time(record: dict[str, Any]) -> datetime:
    """Sort key for archive records. Undateable records sort oldest."""
    stamp = record.get("fetched_at")
    if isinstance(stamp, str):
        try:
            return archive.parse_iso(stamp)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class BackoffState:
    """Where the sampler sits on the ladder, read back off the archive."""

    consecutive_failures: int = 0
    previous_delay: float = 0.0
    next_attempt_at: datetime | None = None

    def wait_seconds(self, now: datetime) -> float:
        if self.next_attempt_at is None:
            return 0.0
        return max(0.0, (self.next_attempt_at - now).total_seconds())


@dataclass
class Outcome:
    """What one call to `attempt` did."""

    action: str  # "fetched", "skipped"
    reason: str = ""
    wait_seconds: float = 0.0
    status: int | None = None
    record: dict[str, Any] | None = None
    lossy: bool = False

    @property
    def usable(self) -> bool:
        """A 200 whose body arrived intact.

        A body that did not decode cleanly is a broken snapshot, not a detail.
        It is reported as loudly as an http failure, but it does not touch the
        backoff ladder: the service answered, so there is nothing to back off
        from.
        """
        return self.action == "fetched" and self.status == 200 and not self.lossy


def derive_backoff_state(records: list[dict[str, Any]]) -> BackoffState:
    """Read the current backoff position out of the tail of the archive.

    Only a trailing run of failures counts: one success clears the ladder.

    Records are ordered by their own timestamps rather than by their position
    in the file. Appends are normally in order, but a union merge of two runs
    that both wrote to the same month file can interleave them, and a hand
    edited archive can put them in any order at all.
    """
    if not records:
        return BackoffState()

    records = sorted(records, key=_record_time)
    last = records[-1]
    if last.get("ok"):
        return BackoffState()

    consecutive = 0
    for record in reversed(records):
        if record.get("ok"):
            break
        consecutive += 1

    raw_delay = last.get("backoff_seconds")
    previous_delay = float(raw_delay) if isinstance(raw_delay, (int, float)) else 0.0

    next_attempt_at = None
    stamp = last.get("fetched_at")
    if isinstance(stamp, str):
        try:
            next_attempt_at = archive.parse_iso(stamp) + timedelta(seconds=previous_delay)
        except ValueError:
            next_attempt_at = None

    return BackoffState(
        consecutive_failures=consecutive,
        previous_delay=previous_delay,
        next_attempt_at=next_attempt_at,
    )


class Fetcher:
    """Fetches one URL, archives the raw response, and nothing else.

    No parsing happens here. Step 2 reads the archive; it does not hook in.
    """

    def __init__(
        self,
        transport: Transport,
        store: archive.Archive,
        *,
        url: str = DEFAULT_URL,
        limit_per_hour: int = budget.HARD_CEILING_PER_HOUR,
        clock: Callable[[], datetime] | None = None,
        guard: Guard | None = None,
    ):
        self.transport = transport
        self.store = store
        self.url = url
        self.budget = budget.Budget(store, limit_per_hour=limit_per_hour)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Optional floor for the case where the archive write may not survive.
        # It can only ever delay an attempt, never bring one forward.
        self.guard = guard

    def state(self) -> BackoffState:
        return derive_backoff_state(self.store.read_tail(limit=50))

    def attempt(self, *, dry_run: bool = False) -> Outcome:
        """Make at most one request, and write exactly one record if it does."""
        now = self.clock()
        state = self.state()

        backoff_wait = state.wait_seconds(now)
        if backoff_wait > 0:
            return Outcome(
                action="skipped",
                reason=f"backing off after {state.consecutive_failures} failed attempt(s)",
                wait_seconds=backoff_wait,
            )

        if self.guard:
            floor = self.guard.not_before(now)
            if floor is not None:
                guard_state = self.guard.read()
                return Outcome(
                    action="skipped",
                    reason=(
                        "guard is holding the backoff from an earlier run "
                        f"({guard_state.consecutive_failures} failed attempt(s)) "
                        "whose record is not in this archive"
                    ),
                    wait_seconds=max(0.0, (floor - now).total_seconds()),
                )

        budget_wait = max(0.0, (self.budget.next_allowed_at(now) - now).total_seconds())
        if budget_wait > 0:
            return Outcome(
                action="skipped",
                reason=f"hourly request budget spent ({self.budget.limit}/hour)",
                wait_seconds=budget_wait,
            )

        if dry_run:
            return Outcome(
                action="skipped",
                reason="dry run, no request made",
                wait_seconds=0.0,
            )

        response = self.transport.get(self.url)
        failed = not response.ok

        delay = None
        if failed:
            delay = backoff.next_delay(
                http_status=response.status,
                headers=response.headers,
                body=self._hint_body(response),
                consecutive_failures=state.consecutive_failures + 1,
                previous_delay=state.previous_delay,
                now=now,
            )

        record = archive.make_record(
            url=self.url,
            source=getattr(self.transport, "source", getattr(self.transport, "name", "unknown")),
            ok=not failed,
            http_status=response.status,
            headers=response.headers,
            raw_body=response.raw_body,
            elapsed_ms=response.elapsed_ms,
            error=response.error,
            backoff_seconds=delay,
            fetched_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.store.append(record)

        if self.guard:
            self.guard.record(
                now=now,
                ok=not failed,
                delay_seconds=delay,
                consecutive_failures=state.consecutive_failures + 1,
                http_status=response.status,
            )

        lossy = bool(record.get("body_lossy"))
        if lossy:
            reason = "body did not decode cleanly as utf-8"
        else:
            reason = response.error or "ok"

        return Outcome(
            action="fetched",
            reason=reason,
            wait_seconds=delay or 0.0,
            status=response.status,
            record=record,
            lossy=lossy,
        )

    @staticmethod
    def _hint_body(response: Response) -> str | None:
        """Decode an error body only far enough to look for retry hints.

        Bodies of successful responses are never scanned: they are untrusted
        room names and topics, and nothing in them is an instruction.
        """
        if response.ok or response.raw_body is None:
            return None
        text, _encoding, _lossy = archive.decode_body(response.raw_body[:4096])
        return text

    def run(
        self,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        max_cycles: int | None = None,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        on_outcome: Callable[[Outcome], None] | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> list[Outcome]:
        """Sample sequentially, forever or for `max_cycles` iterations.

        One request at a time, always. The wait between attempts is the longest
        of the sampling interval, the backoff ladder, and the budget window.
        """
        interval = max(float(interval), MIN_INTERVAL_SECONDS)
        outcomes: list[Outcome] = []
        cycle = 0

        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            if heartbeat:
                # Tells the lock this worker is alive, so a long run is never
                # mistaken for a crashed one.
                heartbeat()
            outcome = self.attempt(dry_run=dry_run)
            outcomes.append(outcome)
            if on_outcome:
                on_outcome(outcome)

            if max_cycles is not None and cycle >= max_cycles:
                break

            wait = max(interval, outcome.wait_seconds)
            sleep(wait)

        return outcomes
