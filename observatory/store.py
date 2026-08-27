"""SQLite loader.

Build step 3. Reads the NDJSON archive, parses each record, and writes the
result into `data/observatory.db`.

The database is disposable. Every table here can be rebuilt from the archive
alone, and rebuilding is the normal way to pick up a new parse version. Nothing
is stored that cannot be recomputed: the `rooms` table is an aggregate over
`room_observations` and is recomputed on every load rather than maintained
incrementally, so it can never drift from the observations it summarises.

Every string that came from the service is written through a bound parameter.
Room paths and topics are anonymous third-party input; they are stored, and
they are never concatenated into SQL.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import archive as archive_module
from . import parser as parser_module

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = "data/observatory.db"

SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per request attempt, holding the raw response text. This is the
-- table everything else can be rebuilt from if the archive is ever lost.
CREATE TABLE snapshots (
    id             INTEGER PRIMARY KEY,
    dedupe_key     TEXT    NOT NULL UNIQUE,
    fetched_at     TEXT    NOT NULL,
    url            TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    http_status    INTEGER,
    ok             INTEGER NOT NULL,
    error          TEXT,
    elapsed_ms     INTEGER,
    body_bytes     INTEGER,
    body_sha256    TEXT,
    body_encoding  TEXT,
    body_lossy     INTEGER NOT NULL DEFAULT 0,
    raw_body       TEXT,
    parse_version  INTEGER,
    parse_flagged  INTEGER NOT NULL DEFAULT 0,
    parse_problems TEXT
);

CREATE INDEX snapshots_fetched_at ON snapshots (fetched_at);
CREATE INDEX snapshots_flagged ON snapshots (parse_flagged);

-- The network-wide aggregates: the header and both footer lines. The four
-- engagement figures are the time series this project exists to keep.
CREATE TABLE network_stats (
    snapshot_id             INTEGER PRIMARY KEY
                            REFERENCES snapshots (id) ON DELETE CASCADE,
    rooms_shown             INTEGER,
    rooms_total             INTEGER,
    room_cap                INTEGER,
    bytes_stored            INTEGER,
    bytes_cap               INTEGER,
    notes_total             INTEGER,
    notes_cap               INTEGER,
    notes_bytes             INTEGER,
    notes_per_namespace_cap INTEGER,
    msgs_scanned            INTEGER,
    zero_response_rate      REAL,
    nick_diversity          REAL,
    notes_per_msg           REAL
);

-- One row per room line per snapshot. Paths and topics are self-asserted,
-- unverified strings.
CREATE TABLE room_observations (
    id              INTEGER PRIMARY KEY,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots (id) ON DELETE CASCADE,
    room_path       TEXT    NOT NULL,
    seq             INTEGER,
    size_bytes      INTEGER,
    idle_seconds    INTEGER,
    topic           TEXT,
    topic_truncated INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX room_observations_path ON room_observations (room_path);
CREATE INDEX room_observations_snapshot ON room_observations (snapshot_id);

-- Derived entirely from room_observations. Recomputed on every load, never
-- maintained by hand, so it cannot drift.
CREATE TABLE rooms (
    room_path         TEXT PRIMARY KEY,
    first_seen        TEXT    NOT NULL,
    last_seen         TEXT    NOT NULL,
    first_seq         INTEGER,
    last_seq          INTEGER,
    observation_count INTEGER NOT NULL
);
"""


@dataclass
class LoadReport:
    """What one load did. Counts that should be looked at, not just logged."""

    db_path: str = ""
    files: list[str] = field(default_factory=list)
    records_read: int = 0
    snapshots_loaded: int = 0
    duplicates_skipped: int = 0
    failed_requests: int = 0
    flagged: int = 0
    lossy: int = 0
    room_observations: int = 0
    rooms: int = 0
    problem_codes: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [
            f"database        {self.db_path}",
            f"archive files   {len(self.files)}",
            f"records read    {self.records_read}",
            f"snapshots       {self.snapshots_loaded} loaded, "
            f"{self.duplicates_skipped} already present",
            f"failed requests {self.failed_requests}",
            f"observations    {self.room_observations}",
            f"rooms known     {self.rooms}",
        ]
        if self.lossy:
            # A body that did not decode cleanly is a broken snapshot. Say so
            # at the same volume as a parse failure, never as a footnote.
            out.append(f"LOSSY BODIES    {self.lossy} snapshot(s) did not decode cleanly as utf-8")
        if self.flagged:
            out.append(f"FLAGGED         {self.flagged} snapshot(s) did not parse cleanly")
            for code, count in sorted(self.problem_codes.items(), key=lambda kv: (-kv[1], kv[0])):
                out.append(f"                {count:>5}  {code}")
        return out


def connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        ("parse_version", str(parser_module.PARSE_VERSION)),
    )
    connection.commit()


def schema_version(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", ("schema_version",)
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row["value"]) if row else None


def dedupe_key(record: dict[str, Any]) -> str:
    """Identity of one request attempt.

    Timestamp, source, status, body digest and error, all of them. Only a
    record identical in every one of those is the same attempt loaded twice,
    which is what makes a reload safe.

    Status has to be in the key and not merely a fallback for a missing
    digest: a 200 and a 503 in the same second carrying the same body are two
    different attempts, and treating them as one loses the failure.
    """
    parts = [
        str(record.get("fetched_at")),
        str(record.get("source")),
        str(record.get("http_status")),
        str(record.get("body_sha256")),
        str(record.get("error")),
    ]
    return "|".join(parts)


def load_record(connection: sqlite3.Connection, record: dict[str, Any]) -> str:
    """Load one archive record. Returns what happened.

    One of "loaded", "duplicate". Never raises on bad content: a record that
    cannot be parsed is stored flagged, with its raw text intact, so a later
    parse version can do better with it.
    """
    key = dedupe_key(record)
    existing = connection.execute(
        "SELECT id FROM snapshots WHERE dedupe_key = ?", (key,)
    ).fetchone()
    if existing:
        return "duplicate"

    snapshot = parser_module.parse_record(record)
    problems = snapshot.problem_codes
    parsed_anything = bool(snapshot.rooms) or snapshot.network.has_header

    cursor = connection.execute(
        """
        INSERT INTO snapshots (
            dedupe_key, fetched_at, url, source, http_status, ok, error,
            elapsed_ms, body_bytes, body_sha256, body_encoding, body_lossy,
            raw_body, parse_version, parse_flagged, parse_problems
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            str(record.get("fetched_at") or ""),
            str(record.get("url") or ""),
            str(record.get("source") or ""),
            record.get("http_status"),
            1 if record.get("ok") else 0,
            record.get("error"),
            record.get("elapsed_ms"),
            record.get("body_bytes"),
            record.get("body_sha256"),
            record.get("body_encoding"),
            1 if record.get("body_lossy") else 0,
            record.get("body"),
            snapshot.parse_version if parsed_anything else None,
            1 if snapshot.flagged else 0,
            json.dumps(problems) if problems else None,
        ),
    )
    snapshot_id = cursor.lastrowid

    network = snapshot.network
    if parsed_anything:
        connection.execute(
            """
            INSERT INTO network_stats (
                snapshot_id, rooms_shown, rooms_total, room_cap, bytes_stored,
                bytes_cap, notes_total, notes_cap, notes_bytes,
                notes_per_namespace_cap, msgs_scanned, zero_response_rate,
                nick_diversity, notes_per_msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                network.rooms_shown,
                network.rooms_total,
                network.room_cap,
                network.bytes_stored,
                network.bytes_cap,
                network.notes_total,
                network.notes_cap,
                network.notes_bytes,
                network.notes_per_namespace_cap,
                network.msgs_scanned,
                network.zero_response_rate,
                network.nick_diversity,
                network.notes_per_msg,
            ),
        )

    if snapshot.rooms:
        connection.executemany(
            """
            INSERT INTO room_observations (
                snapshot_id, room_path, seq, size_bytes, idle_seconds, topic,
                topic_truncated
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_id,
                    room.path,
                    room.seq,
                    room.size_bytes,
                    room.idle_seconds,
                    room.topic,
                    1 if room.topic_truncated else 0,
                )
                for room in snapshot.rooms
            ],
        )

    return "loaded"


def refresh_rooms(connection: sqlite3.Connection) -> int:
    """Rebuild the rooms table from the observations. Returns the row count.

    first_seq and last_seq follow the snapshot timestamps rather than the seq
    values themselves, because a room path can be reused after the server's
    ring buffer drops it and a seq can therefore go backwards.
    """
    connection.execute("DELETE FROM rooms")
    connection.execute(
        """
        INSERT INTO rooms (
            room_path, first_seen, last_seen, first_seq, last_seq,
            observation_count
        )
        SELECT
            observation.room_path,
            MIN(snapshot.fetched_at),
            MAX(snapshot.fetched_at),
            (
                SELECT earliest.seq
                FROM room_observations AS earliest
                JOIN snapshots AS earliest_snapshot
                    ON earliest_snapshot.id = earliest.snapshot_id
                WHERE earliest.room_path = observation.room_path
                ORDER BY earliest_snapshot.fetched_at ASC, earliest.id ASC
                LIMIT 1
            ),
            (
                SELECT latest.seq
                FROM room_observations AS latest
                JOIN snapshots AS latest_snapshot
                    ON latest_snapshot.id = latest.snapshot_id
                WHERE latest.room_path = observation.room_path
                ORDER BY latest_snapshot.fetched_at DESC, latest.id DESC
                LIMIT 1
            ),
            COUNT(*)
        FROM room_observations AS observation
        JOIN snapshots AS snapshot ON snapshot.id = observation.snapshot_id
        GROUP BY observation.room_path
        """
    )
    row = connection.execute("SELECT COUNT(*) AS count FROM rooms").fetchone()
    return int(row["count"])


def _summarise(connection: sqlite3.Connection, report: LoadReport) -> LoadReport:
    counts = connection.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed,
            SUM(parse_flagged) AS flagged,
            SUM(body_lossy) AS lossy
        FROM snapshots
        """
    ).fetchone()
    report.failed_requests = int(counts["failed"] or 0)
    report.flagged = int(counts["flagged"] or 0)
    report.lossy = int(counts["lossy"] or 0)

    observations = connection.execute(
        "SELECT COUNT(*) AS count FROM room_observations"
    ).fetchone()
    report.room_observations = int(observations["count"])

    for row in connection.execute(
        "SELECT parse_problems FROM snapshots WHERE parse_problems IS NOT NULL"
    ):
        try:
            codes = json.loads(row["parse_problems"])
        except (TypeError, json.JSONDecodeError):
            continue
        for code in codes:
            report.problem_codes[code] = report.problem_codes.get(code, 0) + 1
    return report


def load(
    connection: sqlite3.Connection,
    records: Iterable[dict[str, Any]],
    report: LoadReport | None = None,
) -> LoadReport:
    """Load records into an open database and refresh the derived table."""
    report = report or LoadReport()
    for record in records:
        report.records_read += 1
        if load_record(connection, record) == "loaded":
            report.snapshots_loaded += 1
        else:
            report.duplicates_skipped += 1
    report.rooms = refresh_rooms(connection)
    connection.commit()
    return _summarise(connection, report)


def build(
    db_path: str = DEFAULT_DB_PATH,
    store: archive_module.Archive | None = None,
    *,
    rebuild: bool = True,
) -> LoadReport:
    """Build or update the database from the NDJSON archive.

    With `rebuild` (the default) the database is built fresh into a temporary
    file and moved into place, so an interrupted rebuild cannot leave a
    half-written database behind and the old one stays readable until the new
    one is complete.

    With `rebuild=False` the existing database is topped up: records already
    present are skipped by their dedupe key. Use it for the common case of
    adding the last few snapshots; use a rebuild after a parse version change.
    """
    store = store or archive_module.Archive()
    report = LoadReport(db_path=db_path, files=store.files())

    if not rebuild and os.path.exists(db_path):
        connection = connect(db_path)
        try:
            if schema_version(connection) != SCHEMA_VERSION:
                # An older database cannot be topped up safely, and it is
                # disposable by design, so rebuild it instead.
                connection.close()
                return build(db_path, store, rebuild=True)
            return load(connection, store.iter_records(), report)
        finally:
            connection.close()

    temporary = db_path + ".building"
    if os.path.exists(temporary):
        os.unlink(temporary)

    connection = connect(temporary)
    try:
        create_schema(connection)
        load(connection, store.iter_records(), report)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("built_at", archive_module.utc_now_iso()),
        )
        connection.commit()
    except BaseException:
        # The database in place is still the last good one. Take the partial
        # build away with us rather than leaving it to be puzzled over.
        connection.close()
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    finally:
        connection.close()

    os.replace(temporary, db_path)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m observatory.store",
        description=(
            "Build data/observatory.db from the NDJSON archive. Reads files "
            "only, never the network. The database is disposable and can "
            "always be rebuilt from the archive."
        ),
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"database path (default {DEFAULT_DB_PATH})")
    parser.add_argument(
        "--archive",
        default=archive_module.DEFAULT_ROOT,
        help=f"archive directory (default {archive_module.DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="add new records to an existing database instead of rebuilding it",
    )
    args = parser.parse_args(argv)

    report = build(args.db, archive_module.Archive(args.archive), rebuild=not args.update)
    for line in report.lines():
        print(line)
    # A flagged or lossy snapshot is not a failure of the load. It is loaded,
    # kept, and counted, and the exit status says it needs looking at.
    return 1 if (report.flagged or report.lossy) else 0


if __name__ == "__main__":
    raise SystemExit(main())
