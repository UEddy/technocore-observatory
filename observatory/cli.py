"""Command line entry point for the sampler.

Defaults are deliberately safe: with no arguments the tool replays the saved
fixture and writes to the archive. Talking to the live service takes two
explicit flags, so no development run can hit it by accident.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import archive, budget
from .guard import Guard
from .fetcher import DEFAULT_INTERVAL_SECONDS, DEFAULT_URL, MIN_INTERVAL_SECONDS, Fetcher, Outcome
from .transport import FixtureTransport, HttpTransport

DEFAULT_ARCHIVE = archive.DEFAULT_ROOT
DEFAULT_FIXTURE = "fixtures/rooms-sample.txt"
DEFAULT_LOCK = "data/.sampler.lock"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m observatory",
        description=(
            "Sample the technocore.chat rooms endpoint and archive raw responses "
            "as NDJSON. Fetching only. Parsing is a separate step."
        ),
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"endpoint to sample (default {DEFAULT_URL})")
    parser.add_argument(
        "--archive",
        default=DEFAULT_ARCHIVE,
        help=f"archive directory, one NDJSON file per month (default {DEFAULT_ARCHIVE})",
    )
    parser.add_argument(
        "--source",
        choices=("fixture", "http"),
        default="fixture",
        help="where responses come from (default fixture, which makes no network calls)",
    )
    parser.add_argument("--fixture", default=DEFAULT_FIXTURE, help=f"fixture file for --source fixture (default {DEFAULT_FIXTURE})")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="required alongside --source http, so live requests are always deliberate",
    )
    parser.add_argument(
        "--replay-status",
        type=int,
        default=200,
        help="status the fixture transport should report, for exercising backoff offline",
    )
    parser.add_argument(
        "--replay-header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="header the fixture transport should report, repeatable",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"seconds between samples (default {int(DEFAULT_INTERVAL_SECONDS)}, floor {int(MIN_INTERVAL_SECONDS)})",
    )
    parser.add_argument("--once", action="store_true", help="make a single attempt and exit (the default)")
    parser.add_argument("--loop", action="store_true", help="keep sampling on the interval until interrupted")
    parser.add_argument("--cycles", type=int, default=None, help="stop after this many cycles in loop mode")
    parser.add_argument(
        "--limit-per-hour",
        type=int,
        default=budget.HARD_CEILING_PER_HOUR,
        help=f"request ceiling per hour (default and hard maximum {budget.HARD_CEILING_PER_HOUR})",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would happen without requesting or writing")
    parser.add_argument("--status", action="store_true", help="print budget and backoff state, then exit")
    parser.add_argument("--lock", default=DEFAULT_LOCK, help=f"worker lock path (default {DEFAULT_LOCK})")
    parser.add_argument(
        "--guard",
        default=None,
        metavar="PATH",
        help=(
            "keep the backoff floor in this file as well as in the archive, for "
            "runs whose archive write might not survive, such as a scheduled job "
            "that has to push its results somewhere"
        ),
    )
    return parser


def parse_replay_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition(":")
        if not separator:
            raise ValueError(f"bad --replay-header value: {item!r}, expected NAME:VALUE")
        headers[name.strip().lower()] = value.strip()
    return headers


def describe_last_attempt(store: archive.Archive) -> str:
    """What the archive says actually happened, not what these flags would do.

    Status is a diagnostic. Reporting the transport this invocation would build
    describes a default rather than the last run, which is worse than saying
    nothing.
    """
    tail = store.read_tail(limit=1)
    if not tail:
        return "none recorded"

    record = tail[0]
    status = record.get("http_status")
    outcome = f"status {status}" if status is not None else "no response"
    if not record.get("ok"):
        outcome += f" ({record.get('error') or 'failed'})"
    if record.get("body_lossy"):
        outcome += ", body did not decode cleanly"
    return (
        f"{record.get('fetched_at') or 'unknown time'} "
        f"from {record.get('source') or 'unknown source'}, {outcome}"
    )


def describe(outcome: Outcome) -> str:
    if outcome.action == "fetched":
        record = outcome.record or {}
        parts = [
            f"fetched status={outcome.status}",
            f"bytes={record.get('body_bytes')}",
            f"sha256={str(record.get('body_sha256'))[:12]}",
            f"ms={record.get('elapsed_ms')}",
        ]
        if outcome.lossy:
            # As loud as a failed parse. The snapshot is kept, but it is
            # broken and the exit status says so.
            parts.append(f"LOSSY BODY ({outcome.reason})")
        if outcome.wait_seconds:
            parts.append(f"backoff={int(outcome.wait_seconds)}s ({outcome.reason})")
        return " ".join(parts)
    return f"skipped: {outcome.reason} (retry in {int(outcome.wait_seconds)}s)"


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.source == "http" and not args.allow_network:
        parser.error("--source http requires --allow-network. Development runs use --source fixture.")
    if args.limit_per_hour > budget.HARD_CEILING_PER_HOUR:
        parser.error(f"--limit-per-hour cannot exceed the hard ceiling of {budget.HARD_CEILING_PER_HOUR}")

    try:
        replay_headers = parse_replay_headers(args.replay_header)
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    if args.source == "http":
        transport = HttpTransport()
    else:
        transport = FixtureTransport(args.fixture, status=args.replay_status, headers=replay_headers)

    store = archive.Archive(args.archive)
    guard = Guard(args.guard)
    fetcher = Fetcher(
        transport,
        store,
        url=args.url,
        limit_per_hour=args.limit_per_hour,
        guard=guard,
    )

    if args.status:
        now = datetime.now(timezone.utc)
        state = fetcher.state()
        print(f"archive        {args.archive} ({len(store.files())} month file(s))")
        print(f"current file   {store.path_for()}")
        print(f"last attempt   {describe_last_attempt(store)}")
        print(f"budget         {fetcher.budget.used(now)}/{fetcher.budget.limit} used in the last hour")
        print(f"next allowed   in {int(max(0.0, (fetcher.budget.next_allowed_at(now) - now).total_seconds()))}s")
        print(f"failures       {state.consecutive_failures} consecutive")
        print(f"backoff wait   {int(state.wait_seconds(now))}s")
        print(f"guard          {guard.describe(now)}")
        # Last, and labelled, so it cannot be read as a report of what ran.
        print(f"if run now     would fetch from {getattr(transport, 'source', transport.name)}")
        return 0

    lock = budget.WorkerLock(args.lock)
    try:
        lock.acquire()
    except budget.LockHeld as exc:
        print(f"not starting: {exc}", file=sys.stderr)
        return 3

    if lock.broke_stale_lock:
        print(f"cleared a stale lock: {lock.broke_stale_lock}", file=sys.stderr)

    try:
        if args.loop:
            fetcher.run(
                interval=args.interval,
                max_cycles=args.cycles,
                dry_run=args.dry_run,
                on_outcome=lambda outcome: print(describe(outcome), flush=True),
                heartbeat=lock.heartbeat,
            )
        else:
            outcome = fetcher.attempt(dry_run=args.dry_run)
            print(describe(outcome))
            if outcome.action == "fetched" and not outcome.usable:
                return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        lock.release()

    return 0
