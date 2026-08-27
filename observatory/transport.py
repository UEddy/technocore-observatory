"""Transports.

A transport performs exactly one request and returns what came back. It never
retries, never parses, and never decides anything about scheduling: those are
the fetcher's job.

Two implementations:

  * FixtureTransport reads a saved response off disk. This is the default, so
    running the tool during development cannot touch the live service.
  * HttpTransport talks to the network. The fetcher only builds one when the
    operator asks for it explicitly.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_SECONDS = 30.0
USER_AGENT = (
    "technocore-observatory/0.1 (read-only sampler; one request per interval; "
    "ceiling 30/hour; +https://github.com/technocore-observatory)"
)


@dataclass
class Response:
    """One request attempt's outcome."""

    status: int | None
    headers: dict[str, str] = field(default_factory=dict)
    raw_body: bytes | None = None
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None


class FixtureTransport:
    """Replays a saved response body. Makes no network calls, ever.

    `status` and `headers` can be overridden to exercise the backoff paths
    against a fixture without touching the service.
    """

    name = "fixture"

    def __init__(
        self,
        fixture_path: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.fixture_path = fixture_path
        self.status = status
        self.headers = dict(headers or {})

    @property
    def source(self) -> str:
        return f"fixture:{self.fixture_path}"

    def get(self, url: str) -> Response:
        started = time.monotonic()
        try:
            with open(self.fixture_path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            return Response(
                status=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"fixture read failed: {exc.__class__.__name__}",
            )
        return Response(
            status=self.status,
            headers=dict(self.headers),
            raw_body=raw,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=None if self.status == 200 else f"fixture replay status {self.status}",
        )


class HttpTransport:
    """A single plain GET over HTTPS. Stdlib only, no session reuse needed."""

    name = "http"
    source = "http"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = timeout

    def get(self, url: str) -> Response:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": USER_AGENT, "Accept": "text/plain, */*"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                raw = reply.read()
                return Response(
                    status=reply.status,
                    headers={key.lower(): value for key, value in reply.headers.items()},
                    raw_body=raw,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
        except urllib.error.HTTPError as exc:
            # An error response still carries a body, and on 429 that body holds
            # the bucket details the backoff policy needs.
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 - a body we cannot read is not fatal
                raw = b""
            return Response(
                status=exc.code,
                headers={key.lower(): value for key, value in (exc.headers or {}).items()},
                raw_body=raw,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"http {exc.code}",
            )
        except urllib.error.URLError as exc:
            return Response(
                status=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"network error: {exc.reason}",
            )
        except TimeoutError:
            return Response(
                status=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error="network error: timeout",
            )
