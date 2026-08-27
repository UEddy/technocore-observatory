"""Append-only NDJSON archive of raw responses, rotated by month.

One JSON object per line, one line per request attempt, successes and failures
alike, so the request budget and the backoff ladder stay auditable from the
archive alone. Response bodies are stored verbatim as JSON strings: untrusted
third-party text that is recorded, never interpreted.

Files are `data/archive/YYYY-MM.ndjson`, chosen by the timestamp on the record
rather than by the clock at write time, so a run that crosses a month boundary
still files each attempt under the month it happened in. Monthly files keep any
single file small enough to review in a diff and keep the whole history
greppable with plain tools.

Reads that only need recent history seek backwards from the end of the newest
file and cross into older files only when they have to.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterator

# Bump when the record shape changes in a way a reader must notice.
RECORD_SCHEMA = 2

MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.ndjson$")
DEFAULT_ROOT = "data/archive"


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


def month_key(stamp: str | None) -> str:
    """The YYYY-MM a record belongs to, falling back to now if unreadable."""
    if isinstance(stamp, str):
        try:
            return parse_iso(stamp).strftime("%Y-%m")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m")


def body_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_body(raw: bytes) -> tuple[str, str, bool]:
    """Decode a response body to text. One path, every time.

    Returns (text, encoding_label, lossy). The endpoint serves UTF-8, so every
    body is stored as text: that is what keeps the NDJSON archive greppable and
    its diffs reviewable, per the spec.

    A body that does not decode cleanly is still stored, with the undecodable
    bytes replaced, and is labelled and flagged. Callers are expected to treat
    a lossy body as a broken snapshot, not as a detail: the digest is always
    taken over the bytes as they came off the wire, so a lossy record can
    always be proved to differ from its original.
    """
    try:
        return raw.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), "utf-8-replace", True


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
    """Build one archive record. The body is stored whole, never trimmed.

    Every record carries the same keys, whatever happened to the request.
    """
    body: str | None = None
    encoding: str | None = None
    lossy = False
    if raw_body is not None:
        body, encoding, lossy = decode_body(raw_body)

    return {
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
        "body_encoding": encoding,
        "body_lossy": lossy,
        "error": error,
        "backoff_seconds": backoff_seconds,
        # Parsing is a separate step that reads this archive. The field is
        # reserved so a reader can tell an unparsed record from one written by
        # a later version of the sampler.
        "parse_version": None,
        "body": body,
    }


def append_to_file(path: str, record: dict[str, Any]) -> None:
    """Append one record to one file, flushed to disk before returning."""
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


def iter_file_records(path: str) -> Iterator[dict[str, Any]]:
    """Yield every well-formed record in one file, skipping unparsable lines.

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


def read_file_tail(path: str, limit: int = 200) -> list[dict[str, Any]]:
    """Return up to `limit` most recent records from one file, oldest first.

    Reads backwards in chunks so a long file is never walked in full.
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


class Archive:
    """A directory of monthly NDJSON files, treated as one append-only log."""

    def __init__(self, root: str = DEFAULT_ROOT):
        self.root = root

    def __repr__(self) -> str:
        return f"Archive({self.root!r})"

    def path_for(self, stamp: str | None = None) -> str:
        """The file a record with this timestamp belongs in."""
        return os.path.join(self.root, f"{month_key(stamp)}.ndjson")

    def files(self) -> list[str]:
        """Every month file, oldest first.

        Names sort chronologically as strings, which is the whole point of
        YYYY-MM. Anything else in the directory is ignored.
        """
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        months = sorted(name for name in names if MONTH_FILE_RE.match(name))
        return [os.path.join(self.root, name) for name in months]

    def append(self, record: dict[str, Any]) -> str:
        """Append one record to its month file. Returns the file written."""
        path = self.path_for(record.get("fetched_at"))
        append_to_file(path, record)
        return path

    def read_tail(self, limit: int = 200) -> list[dict[str, Any]]:
        """Up to `limit` most recent records, oldest first, across files.

        Starts at the newest month and walks backwards only as far as it must,
        so the usual case reads the end of a single file. A month boundary is
        invisible to the caller: a tail that needs more records than the
        current month holds continues into the previous one.
        """
        if limit <= 0:
            return []

        collected: list[dict[str, Any]] = []
        for path in reversed(self.files()):
            needed = limit - len(collected)
            if needed <= 0:
                break
            collected = read_file_tail(path, needed) + collected
        return collected[-limit:]

    def iter_records(self) -> Iterator[dict[str, Any]]:
        """Every record ever archived, oldest first. The full walk, for
        rebuilds. Not for the sampling path."""
        for path in self.files():
            yield from iter_file_records(path)

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())
