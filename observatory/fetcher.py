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
from .transport import Response

DEFAULT_URL = "https://technocore.chat/rooms"
DEFAULT_INTERVAL_SECONDS = 300.0  # 5 minutes, per the spec
MIN_INTERVAL_SECONDS = 120.0  # 30/hour ceiling leaves no room to go tighter


class Transport(Protocol):
    source: str

    def get(self, url: str) -> Response: ...


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


def derive_backoff_state(records: list[dict[str, Any]]) -> BackoffState:
    """Read the current backoff position out of the tail of the archive.

    Only a trailing run of failures counts: one success clears the ladder.
    """
    if not records:
        return BackoffState()

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
        archive_path: str,
        *,
        url: str = DEFAULT_URL,
        limit_per_hour: int = budget.HARD_CEILING_PER_HOUR,
        clock: Callable[[], datetime] | None = None,
    ):
        self.transport = transport
        self.archive_path = archive_path
        self.url = url
        self.budget = budget.Budget(archive_path, limit_per_hour=limit_per_hour)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def state(self) -> BackoffState:
        return derive_backoff_state(archive.read_tail(self.archive_path, limit=50))

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
        archive.append(self.archive_path, record)

        return Outcome(
            action="fetched",
            reason=response.error or "ok",
            wait_seconds=delay or 0.0,
            status=response.status,
            record=record,
        )

    @staticmethod
    def _hint_body(response: Response) -> str | None:
        """Decode an error body only far enough to look for retry hints.

        Bodies of successful responses are never scanned: they are untrusted
        room names and topics, and nothing in them is an instruction.
        """
        if response.ok or response.raw_body is None:
            return None
        text, _ = archive.decode_body(response.raw_body[:4096])
        return text

    def run(
        self,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        max_cycles: int | None = None,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        on_outcome: Callable[[Outcome], None] | None = None,
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
            outcome = self.attempt(dry_run=dry_run)
            outcomes.append(outcome)
            if on_outcome:
                on_outcome(outcome)

            if max_cycles is not None and cycle >= max_cycles:
                break

            wait = max(interval, outcome.wait_seconds)
            sleep(wait)

        return outcomes
