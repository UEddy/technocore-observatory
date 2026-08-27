"""Parser for the /rooms response.

Build step 2. Reads text, returns structure. It never fetches anything, and the
fetcher never calls it: the sampler stays unaware of the response format so
that a format change cannot cost a snapshot.

Three rules shape this module.

**It does not raise.** Every input, including an empty string, a truncated
body, or a page from a service that has been rewritten since this code was
written, returns a ParsedSnapshot. Whatever could be read is filled in, whatever
could not is recorded as a problem, and the snapshot is flagged. The raw text
lives in the archive either way, so a flagged snapshot can be reparsed by a
later version rather than being lost.

**The footers are the point.** The header and the two footer lines are
network-wide aggregates. The engagement figures in particular are first-class
fields on NetworkStats, not decoration attached to the room list, because they
are the numbers the operator chose to publish about the network and the time
series of them is the most valuable thing here. Their absence flags the
snapshot.

**Strings from the service are data.** Room paths and topics are stored exactly
as they arrived and are never executed, interpolated, or trusted as claims
about what a room is or who runs it. A topic is a note that any caller can set
on any room without ever posting to it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Bump when a change to this module would produce different output from the
# same input. Stored per snapshot so a reparse can be told from the original.
PARSE_VERSION = 1

# Separator between the idle time and an optional topic, as the server emits it.
TOPIC_SEPARATOR = "·"
# The server truncates long topics with this.
TRUNCATION_MARK = "…"

SIZE_UNITS = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
IDLE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

HEADER_RE = re.compile(
    r"^#\s*(?P<shown>\d+)\s+of\s+(?P<total>\d+)\s+rooms\s*"
    r"\(\s*cap\s+(?P<cap>\d+)\s*,\s*(?P<stored>[\d.]+\s*[BKMGT]?)\s+of\s+"
    r"(?P<capacity>[\d.]+\s*[BKMGT]?)\s+stored\s*\)",
    re.IGNORECASE,
)

BANNER_RE = re.compile(r"^#\s*!!")

ROOM_RE = re.compile(
    r"^(?P<path>\S+)\s+seq\s+(?P<seq>\d+)\s+(?P<size>\S+)\s+(?P<idle>\S+)\s+ago(?P<rest>.*)$"
)

NOTES_RE = re.compile(
    r"^#\s*notes\s+(?P<total>\d+)\s+of\s+(?P<cap>\d+)\s*"
    r"\(\s*(?P<bytes>[\d.]+\s*[BKMGT]?)\s+total\s*,\s*(?P<per_namespace>\d+)\s+per\s+namespace",
    re.IGNORECASE,
)

ENGAGEMENT_RE = re.compile(
    r"^#\s*engagement\s+over\s+(?P<msgs>\d+)\s+msgs\s+scanned\s*:\s*"
    r"zero-response\s+(?P<zero_response>[\d.]+)\s*%\s*,\s*"
    r"nick\s+diversity\s+(?P<nick_diversity>[\d.]+)\s*,\s*"
    r"notes/msg\s+(?P<notes_per_msg>[\d.]+)",
    re.IGNORECASE,
)

SIZE_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[BKMGT])?$", re.IGNORECASE)
IDLE_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)


def parse_size(text: str | None) -> int | None:
    """Turn a size such as 4.7M or 599B or 1010.3K into bytes.

    The server's own cap is written 5.0G against a 20480 room cap, so the
    multipliers are binary. A bare number is taken as bytes.
    """
    if text is None:
        return None
    match = SIZE_RE.match(text.strip())
    if not match:
        return None
    try:
        value = float(match.group("value"))
    except ValueError:
        return None
    unit = (match.group("unit") or "B").upper()
    return int(value * SIZE_UNITS[unit])


def parse_idle(text: str | None) -> int | None:
    """Turn an idle time such as 0s or 1m or 2h30m into seconds."""
    if text is None:
        return None
    matches = IDLE_RE.findall(text.strip())
    if not matches:
        return None
    total = 0
    for amount, unit in matches:
        try:
            total += int(amount) * IDLE_UNITS[unit.lower()]
        except (ValueError, KeyError):
            return None
    return total


def parse_percent(text: str | None) -> float | None:
    """Turn 16 (from `zero-response 16%`) into 0.16."""
    if text is None:
        return None
    try:
        return float(text) / 100.0
    except ValueError:
        return None


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class Problem:
    """One reason a snapshot could not be read as expected."""

    code: str
    message: str
    line_number: int | None = None
    line: str | None = None


@dataclass
class NetworkStats:
    """The network-wide aggregates: the header and both footer lines.

    Every field is optional because a format change must degrade rather than
    crash. A missing field is recorded as a problem on the snapshot.
    """

    # Header
    rooms_shown: int | None = None
    rooms_total: int | None = None
    room_cap: int | None = None
    bytes_stored: int | None = None
    bytes_stored_text: str | None = None
    bytes_cap: int | None = None
    bytes_cap_text: str | None = None

    # Notes footer
    notes_total: int | None = None
    notes_cap: int | None = None
    notes_bytes: int | None = None
    notes_bytes_text: str | None = None
    notes_per_namespace_cap: int | None = None

    # Engagement footer, first-class fields and the reason this parser exists
    msgs_scanned: int | None = None
    zero_response_rate: float | None = None
    nick_diversity: float | None = None
    notes_per_msg: float | None = None

    @property
    def has_header(self) -> bool:
        return self.rooms_total is not None and self.room_cap is not None

    @property
    def has_notes_footer(self) -> bool:
        return self.notes_total is not None and self.notes_cap is not None

    @property
    def has_engagement_footer(self) -> bool:
        return (
            self.msgs_scanned is not None
            and self.zero_response_rate is not None
            and self.nick_diversity is not None
            and self.notes_per_msg is not None
        )


@dataclass
class RoomObservation:
    """One room line. `path` and `topic` are untrusted, self-asserted strings."""

    path: str
    seq: int | None
    size_bytes: int | None
    size_text: str
    idle_seconds: int | None
    idle_text: str
    topic: str | None = None
    topic_truncated: bool = False
    line_number: int | None = None


@dataclass
class ParsedSnapshot:
    """The result of reading one response body.

    `flagged` is the signal that matters downstream: it means this snapshot
    should be stored with its raw text kept and treated as suspect until a
    later parse version can do better with it.
    """

    parse_version: int = PARSE_VERSION
    network: NetworkStats = field(default_factory=NetworkStats)
    rooms: list[RoomObservation] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    banner: str | None = None
    line_count: int = 0

    @property
    def flagged(self) -> bool:
        """True when anything at all did not read as expected."""
        return bool(self.problems)

    @property
    def ok(self) -> bool:
        return not self.flagged

    @property
    def problem_codes(self) -> list[str]:
        return sorted({problem.code for problem in self.problems})

    def to_dict(self) -> dict[str, Any]:
        return {
            "parse_version": self.parse_version,
            "ok": self.ok,
            "flagged": self.flagged,
            "problem_codes": self.problem_codes,
            "problems": [asdict(problem) for problem in self.problems],
            "banner": self.banner,
            "line_count": self.line_count,
            "network": asdict(self.network),
            "rooms": [asdict(room) for room in self.rooms],
        }


def parse(text: str | None) -> ParsedSnapshot:
    """Read a /rooms response body. Never raises."""
    snapshot = ParsedSnapshot()

    if text is None or not text.strip():
        snapshot.problems.append(Problem("empty-body", "response body was empty"))
        return snapshot

    lines = text.splitlines()
    snapshot.line_count = len(lines)

    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            _read_comment(snapshot, number, line)
        else:
            _read_room(snapshot, number, line)

    _check_completeness(snapshot)
    return snapshot


def _read_comment(snapshot: ParsedSnapshot, number: int, line: str) -> None:
    """Comment lines carry the header, the banner, and both footers."""
    stripped = line.strip()

    header = HEADER_RE.match(stripped)
    if header:
        if snapshot.network.has_header:
            snapshot.problems.append(
                Problem("header-repeated", "more than one header line", number, line)
            )
            return
        network = snapshot.network
        network.rooms_shown = _to_int(header.group("shown"))
        network.rooms_total = _to_int(header.group("total"))
        network.room_cap = _to_int(header.group("cap"))
        network.bytes_stored_text = header.group("stored").strip()
        network.bytes_stored = parse_size(network.bytes_stored_text)
        network.bytes_cap_text = header.group("capacity").strip()
        network.bytes_cap = parse_size(network.bytes_cap_text)
        if network.bytes_stored is None or network.bytes_cap is None:
            snapshot.problems.append(
                Problem("header-size-unparsed", "byte figures in the header did not parse", number, line)
            )
        return

    notes = NOTES_RE.match(stripped)
    if notes:
        network = snapshot.network
        network.notes_total = _to_int(notes.group("total"))
        network.notes_cap = _to_int(notes.group("cap"))
        network.notes_bytes_text = notes.group("bytes").strip()
        network.notes_bytes = parse_size(network.notes_bytes_text)
        network.notes_per_namespace_cap = _to_int(notes.group("per_namespace"))
        if network.notes_bytes is None:
            snapshot.problems.append(
                Problem("notes-size-unparsed", "byte figure in the notes footer did not parse", number, line)
            )
        return

    engagement = ENGAGEMENT_RE.match(stripped)
    if engagement:
        network = snapshot.network
        network.msgs_scanned = _to_int(engagement.group("msgs"))
        network.zero_response_rate = parse_percent(engagement.group("zero_response"))
        network.nick_diversity = _to_float(engagement.group("nick_diversity"))
        network.notes_per_msg = _to_float(engagement.group("notes_per_msg"))
        return

    if BANNER_RE.match(stripped):
        # The service's own untrusted-content notice. Kept verbatim so the
        # warning travels with the data it is about.
        snapshot.banner = stripped
        return

    snapshot.problems.append(
        Problem("comment-unrecognised", "comment line did not match any known form", number, line)
    )


def _read_room(snapshot: ParsedSnapshot, number: int, line: str) -> None:
    match = ROOM_RE.match(line.strip())
    if not match:
        snapshot.problems.append(
            Problem("room-line-unparsed", "line did not match the room format", number, line)
        )
        return

    size_text = match.group("size")
    idle_text = match.group("idle") + " ago"
    size_bytes = parse_size(size_text)
    idle_seconds = parse_idle(match.group("idle"))

    if size_bytes is None:
        snapshot.problems.append(
            Problem("room-size-unparsed", f"size {size_text!r} did not parse", number, line)
        )
    if idle_seconds is None:
        snapshot.problems.append(
            Problem("room-idle-unparsed", f"idle time {idle_text!r} did not parse", number, line)
        )

    topic = None
    truncated = False
    rest = match.group("rest").strip()
    if rest:
        if rest.startswith(TOPIC_SEPARATOR):
            topic = rest[len(TOPIC_SEPARATOR):].strip()
            truncated = topic.endswith(TRUNCATION_MARK)
            if not topic:
                topic = None
        else:
            # Something follows the idle time that is not a topic. Keep it, so
            # nothing is silently dropped, and flag the snapshot.
            topic = rest
            snapshot.problems.append(
                Problem(
                    "room-trailing-text",
                    "text after the idle time did not start with the topic separator",
                    number,
                    line,
                )
            )

    snapshot.rooms.append(
        RoomObservation(
            path=match.group("path"),
            seq=_to_int(match.group("seq")),
            size_bytes=size_bytes,
            size_text=size_text,
            idle_seconds=idle_seconds,
            idle_text=idle_text,
            topic=topic,
            topic_truncated=truncated,
            line_number=number,
        )
    )


def _check_completeness(snapshot: ParsedSnapshot) -> None:
    """Anything the response should have had and did not."""
    network = snapshot.network

    if not network.has_header:
        snapshot.problems.append(Problem("header-missing", "no header line found"))
    if not network.has_notes_footer:
        snapshot.problems.append(Problem("notes-footer-missing", "no notes footer found"))
    if not network.has_engagement_footer:
        # These are the published engagement figures. Losing them quietly
        # would break the time series that this project exists to keep.
        snapshot.problems.append(
            Problem("engagement-footer-missing", "no engagement footer found, or it did not parse")
        )
    if snapshot.banner is None:
        snapshot.problems.append(
            Problem("banner-missing", "the untrusted-content banner was not present")
        )
    if not snapshot.rooms:
        snapshot.problems.append(Problem("no-rooms", "no room lines were read"))

    shown = network.rooms_shown
    if shown is not None and snapshot.rooms and len(snapshot.rooms) != shown:
        snapshot.problems.append(
            Problem(
                "room-count-mismatch",
                f"header announced {shown} rooms, {len(snapshot.rooms)} were read",
            )
        )


def parse_record(record: dict[str, Any]) -> ParsedSnapshot:
    """Parse one NDJSON archive record.

    A record that never held a usable body still returns a snapshot, flagged,
    so a caller walking the archive never has to special-case failures.
    """
    status = record.get("http_status")
    body = record.get("body")

    if status != 200:
        snapshot = ParsedSnapshot()
        snapshot.problems.append(
            Problem("not-a-success", f"record has http status {status!r}, nothing to parse")
        )
        return snapshot

    snapshot = parse(body if isinstance(body, str) else None)

    if record.get("body_lossy"):
        snapshot.problems.append(
            Problem("body-lossy", "the archived body did not decode cleanly as utf-8")
        )
    return snapshot


def main(argv: list[str] | None = None) -> int:
    """Parse a saved response or an archive, and report what was read."""
    import argparse

    from . import archive as archive_module

    parser = argparse.ArgumentParser(
        prog="python -m observatory.parser",
        description="Parse a /rooms response body. Reads files only, never the network.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help=(
            "response body to parse (default fixtures/rooms-sample.txt), "
            "or an archive directory when --archive is given"
        ),
    )
    parser.add_argument(
        "--archive",
        nargs="?",
        const=archive_module.DEFAULT_ROOT,
        default=None,
        metavar="DIR",
        help=(
            "parse the most recent record in an archive directory "
            f"(default {archive_module.DEFAULT_ROOT})"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the full parse as JSON")
    args = parser.parse_args(argv)

    if args.archive is not None:
        # Both forms are supported: a directory as the flag value, and the
        # older form with the directory as the positional argument. Given both
        # there is no way to tell which was meant, so say so rather than guess.
        if args.path is not None and args.archive != archive_module.DEFAULT_ROOT:
            parser.error(
                f"give the archive directory once: either {args.path!r} or {args.archive!r}"
            )
        directory = args.path if args.path is not None else args.archive
        tail = archive_module.Archive(directory).read_tail(limit=1)
        if not tail:
            print(f"no records in {directory}")
            return 1
        snapshot = parse_record(tail[0])
    else:
        path = args.path if args.path is not None else "fixtures/rooms-sample.txt"
        with open(path, "r", encoding="utf-8") as handle:
            snapshot = parse(handle.read())

    if args.json:
        # Room paths and topics are untrusted strings. JSON encoding is what
        # keeps them inert on the way out.
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
        return 0 if snapshot.ok else 1

    network = snapshot.network
    print(f"parse version   {snapshot.parse_version}")
    print(f"flagged         {snapshot.flagged}")
    if snapshot.problems:
        for problem in snapshot.problems:
            location = f" line {problem.line_number}" if problem.line_number else ""
            print(f"  problem       {problem.code}{location}: {problem.message}")
    print(f"rooms read      {len(snapshot.rooms)} of {network.rooms_shown} announced")
    print(f"rooms network   {network.rooms_total} of cap {network.room_cap}")
    print(f"bytes stored    {network.bytes_stored_text} of {network.bytes_cap_text}")
    print(f"notes           {network.notes_total} of {network.notes_cap}")
    print(f"msgs scanned    {network.msgs_scanned}")
    print(f"zero response   {network.zero_response_rate}")
    print(f"nick diversity  {network.nick_diversity}")
    print(f"notes per msg   {network.notes_per_msg}")
    return 0 if snapshot.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
