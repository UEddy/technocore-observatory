"""Append-only NDJSON archive of raw responses.

One JSON object per line, one line per request attempt (successes and failures
alike, so the request budget and the backoff ladder stay auditable from the
archive alone). Response bodies are stored verbatim as JSON strings: untrusted
third-party text that is recorded, never interpreted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator

# Bump when the record shape changes in a way a reader must notice.
RECORD_SCHEMA = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(stamp: str) -> datetime:
    """Parse a timestamp written by utc_now_iso, tolerating a trailing Z."""
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def body_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_body(raw: bytes) -> tuple[str, str | None]:
    """Decode a response body to text without ever losing the original bytes.

    Returns (text, base64_of_raw_or_None). The archive keeps text so diffs stay
    reviewable and greppable, per the spec. If the bytes are not valid UTF-8,
    a base64 copy is kept alongside so the response is still recoverable
    byte for byte.
    """
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), base64.b64encode(raw).decode("ascii")


def make_record(
    *,
    url: str,
    source: str,
    ok: bool,
    http_status: int | None,
    headers: dict[str, str] | None,
    raw_body: bytes | None,
    elapsed_ms: int | None,
    error: str | None,
    backoff_seconds: float | None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Build one archive record. The body is stored whole, never trimmed."""
    body: str | None = None
    body_base64: str | None = None
    if raw_body is not None:
        body, body_base64 = decode_body(raw_body)

    record: dict[str, Any] = {
        "schema": RECORD_SCHEMA,
        "fetched_at": fetched_at or utc_now_iso(),
        "url": url,
        "source": source,
        "ok": ok,
        "http_status": http_status,
        "headers": headers or {},
        "elapsed_ms": elapsed_ms,
        "body_bytes": len(raw_body) if raw_body is not None else None,
        "body_sha256": body_digest(raw_body) if raw_body is not None else None,
        "error": error,
        "backoff_seconds": backoff_seconds,
        # Parsing is step 2. The field is reserved so a reader can tell an
        # unparsed archive from one written by a later version.
        "parse_version": None,
        "body": body,
    }
    if body_base64 is not None:
        record["body_base64"] = body_base64
    return record


def append(path: str, record: dict[str, Any]) -> None:
    """Append one record, flushed to disk before returning."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=False)
    if "\n" in line:  # json.dumps escapes newlines, so this should never fire
        raise ValueError("record serialised to a multi-line string")
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def iter_records(path: str) -> Iterator[dict[str, Any]]:
    """Yield every well-formed record, skipping lines that do not parse.

    A corrupt or half-written line must not stop the sampler from running.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def read_tail(path: str, limit: int = 200) -> list[dict[str, Any]]:
    """Return up to `limit` most recent records, oldest first.

    Reads backwards in chunks so a large archive does not have to be walked in
    full on every run.
    """
    if limit <= 0 or not os.path.exists(path):
        return []

    chunk_size = 65536
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        lines: list[bytes] = []
        while position > 0 and len(lines) <= limit:
            step = min(chunk_size, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
            lines = buffer.split(b"\n")
        if position > 0:
            # The first element is a partial line from an earlier chunk.
            lines = lines[1:]

    records: list[dict[str, Any]] = []
    for raw in lines[-(limit + 1):]:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records[-limit:]
