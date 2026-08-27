"""Backoff policy.

Rules come straight from the spec:

  * On 429, honor Retry-After and the bucket details in the response body.
  * On 503, back off exponentially starting at 60s, capped at 30 minutes.
  * Never retry tighter than the previous interval.

The last rule is why every decision takes the previous delay as an input: a
failure episode's delays are non-decreasing, no matter what the server says.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

BASE_DELAY_SECONDS = 60.0
MAX_EXPONENTIAL_SECONDS = 1800.0  # 30 minutes
# A server may legitimately ask for longer than the exponential cap. Honor it,
# but refuse anything absurd, which is more likely a parsing mistake than a
# real instruction.
MAX_HONORED_SECONDS = 86400.0

# Tolerant hints for the bucket details a 429 body may carry. The exact wording
# is not contractual, so several shapes are accepted and the largest wins.
_BODY_HINT_PATTERNS = (
    re.compile(r"retry[_\-\s]*after[\"'\s:=]+(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"(?:retry|reset|resets|wait|try\s+again)\D{0,20}?(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds?\b)", re.IGNORECASE),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds?\b)\s*(?:until|before|till)\s*(?:retry|reset)", re.IGNORECASE),
    re.compile(r"reset[_\-\s]*in[\"'\s:=]+(\d+(?:\.\d+)?)", re.IGNORECASE),
)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header: delta-seconds or an HTTP-date."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (when - reference).total_seconds())


def parse_body_hint(body: str | None) -> float | None:
    """Pull a retry hint out of a rate limit body. Largest match wins."""
    if not body:
        return None
    # Only the first stretch of the body is worth scanning; a rate limit
    # response is short, and a 200 body full of untrusted room topics must not
    # be mined for numbers that look like instructions.
    window = body[:4096]
    candidates: list[float] = []
    for pattern in _BODY_HINT_PATTERNS:
        for match in pattern.finditer(window):
            try:
                candidates.append(float(match.group(1)))
            except (TypeError, ValueError):
                continue
    sane = [value for value in candidates if 0.0 <= value <= MAX_HONORED_SECONDS]
    if not sane:
        return None
    return max(sane)


def exponential_delay(consecutive_failures: int) -> float:
    """60s, 120s, 240s ... capped at 30 minutes."""
    if consecutive_failures < 1:
        consecutive_failures = 1
    exponent = min(consecutive_failures - 1, 32)
    return min(BASE_DELAY_SECONDS * (2.0 ** exponent), MAX_EXPONENTIAL_SECONDS)


def next_delay(
    *,
    http_status: int | None,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    consecutive_failures: int = 1,
    previous_delay: float = 0.0,
    now: datetime | None = None,
) -> float:
    """Seconds to wait before the next attempt after a failed one.

    `consecutive_failures` counts this attempt. `previous_delay` is the delay
    that preceded this attempt, which the result is never allowed to undercut.
    """
    ladder = exponential_delay(consecutive_failures)
    delay = ladder

    if http_status == 429:
        header_value = None
        if headers:
            for key, value in headers.items():
                if key.lower() == "retry-after":
                    header_value = value
                    break
        server_hints = [
            hint
            for hint in (parse_retry_after(header_value, now=now), parse_body_hint(body))
            if hint is not None
        ]
        if server_hints:
            # Honor the server even when it asks for longer than the ladder.
            delay = max(ladder, max(server_hints))

    delay = max(delay, float(previous_delay))
    return min(delay, MAX_HONORED_SECONDS)


def is_failure(http_status: int | None) -> bool:
    """Anything that is not a clean 200 counts as a failed attempt."""
    return http_status != 200
