import json
import os
import sqlite3
import tempfile
import unittest

from observatory import archive, parser, store
from observatory.archive import Archive

FIXTURE = "fixtures/rooms-sample.txt"


def fixture_text():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read()


def snapshot_record(body=None, fetched_at="2026-08-27T12:00:00Z", **overrides):
    raw = (body if body is not None else fixture_text()).encode("utf-8")
    fields = dict(
        url="https://technocore.chat/rooms",
        source="fixture:" + FIXTURE,
        ok=True,
        http_status=200,
        headers={},
        raw_body=raw,
        elapsed_ms=42,
        error=None,
        backoff_seconds=None,
        fetched_at=fetched_at,
    )
    fields.update(overrides)
    return archive.make_record(**fields)


def failure_record(fetched_at="2026-08-27T12:05:00Z"):
    return archive.make_record(
        url="https://technocore.chat/rooms",
        source="http",
        ok=False,
        http_status=503,
        headers={},
        raw_body=b"service unavailable",
        elapsed_ms=12,
        error="http 503",
        backoff_seconds=60.0,
        fetched_at=fetched_at,
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.store = Archive(os.path.join(self.directory, "archive"))
        self.db_path = os.path.join(self.directory, "observatory.db")

    def build(self, **kwargs):
        return store.build(self.db_path, self.store, **kwargs)

    def query(self, sql, params=()):
        connection = store.connect(self.db_path)
        try:
            return [dict(row) for row in connection.execute(sql, params)]
        finally:
            connection.close()

    def scalar(self, sql, params=()):
        rows = self.query(sql, params)
        return list(rows[0].values())[0] if rows else None


class BuildTests(StoreTestCase):
    def test_building_from_one_snapshot(self):
        self.store.append(snapshot_record())
        report = self.build()

        self.assertEqual(report.records_read, 1)
        self.assertEqual(report.snapshots_loaded, 1)
        self.assertEqual(report.room_observations, 50)
        self.assertEqual(report.rooms, 50)
        self.assertEqual(report.flagged, 0)
        self.assertEqual(report.lossy, 0)
        self.assertTrue(os.path.exists(self.db_path))

    def test_the_schema_and_versions_are_recorded(self):
        self.store.append(snapshot_record())
        self.build()
        meta = {row["key"]: row["value"] for row in self.query("SELECT key, value FROM meta")}
        self.assertEqual(meta["schema_version"], str(store.SCHEMA_VERSION))
        self.assertEqual(meta["parse_version"], str(parser.PARSE_VERSION))
        self.assertIn("built_at", meta)

    def test_the_raw_text_is_kept_byte_for_byte(self):
        self.store.append(snapshot_record())
        self.build()
        raw = self.scalar("SELECT raw_body FROM snapshots")
        self.assertEqual(raw, fixture_text())

    def test_the_parse_version_is_stamped_on_the_snapshot(self):
        self.store.append(snapshot_record())
        self.build()
        self.assertEqual(self.scalar("SELECT parse_version FROM snapshots"), parser.PARSE_VERSION)

    def test_network_stats_carry_the_engagement_figures(self):
        self.store.append(snapshot_record())
        self.build()
        row = self.query("SELECT * FROM network_stats")[0]
        self.assertEqual(row["rooms_total"], 17740)
        self.assertEqual(row["room_cap"], 20480)
        self.assertEqual(row["notes_total"], 426362)
        self.assertEqual(row["notes_cap"], 655360)
        self.assertEqual(row["msgs_scanned"], 8072)
        self.assertEqual(row["zero_response_rate"], 0.16)
        self.assertEqual(row["nick_diversity"], 0.23)
        self.assertEqual(row["notes_per_msg"], 52.82)

    def test_room_observations_carry_the_room_lines(self):
        self.store.append(snapshot_record())
        self.build()
        row = self.query(
            "SELECT * FROM room_observations WHERE room_path = ?", ("/r/lobby",)
        )[0]
        self.assertEqual(row["seq"], 3837919)
        self.assertEqual(row["size_bytes"], 4928307)
        self.assertEqual(row["idle_seconds"], 0)
        self.assertEqual(row["topic"], "Technocore &  Ecosystem Room")
        self.assertEqual(row["topic_truncated"], 0)

    def test_a_truncated_topic_is_marked_in_the_database(self):
        self.store.append(snapshot_record())
        self.build()
        self.assertEqual(
            self.scalar(
                "SELECT topic_truncated FROM room_observations WHERE room_path = ?",
                ("/r/kibble",),
            ),
            1,
        )

    def test_a_room_without_a_topic_stores_null_not_empty_string(self):
        self.store.append(snapshot_record())
        self.build()
        self.assertIsNone(
            self.scalar(
                "SELECT topic FROM room_observations WHERE room_path = ?", ("/r/meta",)
            )
        )

    def test_building_across_several_month_files(self):
        self.store.append(snapshot_record(fetched_at="2026-07-31T23:50:00Z"))
        self.store.append(snapshot_record(fetched_at="2026-08-01T00:10:00Z"))
        report = self.build()
        self.assertEqual(len(report.files), 2)
        self.assertEqual(report.snapshots_loaded, 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshots"), 2)


class FailureAndFlagTests(StoreTestCase):
    def test_a_failed_request_is_stored_without_stats_or_observations(self):
        self.store.append(failure_record())
        report = self.build()

        self.assertEqual(report.snapshots_loaded, 1)
        self.assertEqual(report.failed_requests, 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM network_stats"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM room_observations"), 0)
        row = self.query("SELECT * FROM snapshots")[0]
        self.assertEqual(row["ok"], 0)
        self.assertEqual(row["http_status"], 503)
        self.assertEqual(row["error"], "http 503")
        # A failed request has nothing to parse, so it carries no parse version.
        self.assertIsNone(row["parse_version"])
        self.assertEqual(row["raw_body"], "service unavailable")

    def test_a_flagged_snapshot_is_kept_with_its_raw_text(self):
        broken = fixture_text().replace("# engagement over", "# engagement across")
        self.store.append(snapshot_record(body=broken))
        report = self.build()

        self.assertEqual(report.snapshots_loaded, 1)
        self.assertEqual(report.flagged, 1)
        self.assertIn("engagement-footer-missing", report.problem_codes)

        row = self.query("SELECT * FROM snapshots")[0]
        self.assertEqual(row["parse_flagged"], 1)
        self.assertEqual(row["raw_body"], broken)
        codes = json.loads(row["parse_problems"])
        self.assertIn("engagement-footer-missing", codes)
        # What did parse is still loaded, so the room series is not lost.
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM room_observations"), 50)

    def test_an_unreadable_body_loads_flagged_rather_than_crashing(self):
        self.store.append(snapshot_record(body="<html>503</html>"))
        report = self.build()
        self.assertEqual(report.snapshots_loaded, 1)
        self.assertEqual(report.flagged, 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM room_observations"), 0)

    def test_a_lossy_body_is_counted_and_flagged_as_loudly_as_a_parse_failure(self):
        record = snapshot_record()
        record["body_lossy"] = True
        record["body_encoding"] = "utf-8-replace"
        self.store.append(record)
        report = self.build()

        self.assertEqual(report.lossy, 1)
        self.assertEqual(report.flagged, 1)
        self.assertIn("body-lossy", report.problem_codes)
        row = self.query("SELECT * FROM snapshots")[0]
        self.assertEqual(row["body_lossy"], 1)
        self.assertEqual(row["parse_flagged"], 1)
        self.assertIn("LOSSY BODIES", "\n".join(report.lines()))

    def test_the_report_lists_flag_counts_by_code(self):
        self.store.append(snapshot_record(body="not a rooms page", fetched_at="2026-08-27T12:00:00Z"))
        self.store.append(snapshot_record(body="also not one", fetched_at="2026-08-27T12:15:00Z"))
        report = self.build()
        self.assertEqual(report.flagged, 2)
        self.assertEqual(report.problem_codes["header-missing"], 2)


class RoomsAggregateTests(StoreTestCase):
    def two_snapshots(self):
        first = fixture_text()
        second = first.replace("/r/lobby                    seq 3837919", "/r/lobby                    seq 3900000")
        self.store.append(snapshot_record(body=first, fetched_at="2026-08-27T12:00:00Z"))
        self.store.append(snapshot_record(body=second, fetched_at="2026-08-27T12:15:00Z"))

    def test_first_and_last_seen_span_the_snapshots(self):
        self.two_snapshots()
        self.build()
        row = self.query("SELECT * FROM rooms WHERE room_path = ?", ("/r/lobby",))[0]
        self.assertEqual(row["first_seen"], "2026-08-27T12:00:00Z")
        self.assertEqual(row["last_seen"], "2026-08-27T12:15:00Z")
        self.assertEqual(row["observation_count"], 2)

    def test_first_and_last_seq_follow_the_timestamps(self):
        self.two_snapshots()
        self.build()
        row = self.query("SELECT * FROM rooms WHERE room_path = ?", ("/r/lobby",))[0]
        self.assertEqual(row["first_seq"], 3837919)
        self.assertEqual(row["last_seq"], 3900000)

    def test_a_seq_that_goes_backwards_is_reported_as_seen_not_as_a_maximum(self):
        # A room path can be reused after the ring buffer drops it, so seq is
        # not monotonic and the aggregate must not assume it is.
        first = fixture_text()
        second = first.replace("/r/lobby                    seq 3837919", "/r/lobby                    seq 12")
        self.store.append(snapshot_record(body=first, fetched_at="2026-08-27T12:00:00Z"))
        self.store.append(snapshot_record(body=second, fetched_at="2026-08-27T12:15:00Z"))
        self.build()
        row = self.query("SELECT * FROM rooms WHERE room_path = ?", ("/r/lobby",))[0]
        self.assertEqual(row["first_seq"], 3837919)
        self.assertEqual(row["last_seq"], 12)

    def test_rooms_is_derived_and_can_be_recomputed_at_any_time(self):
        self.two_snapshots()
        self.build()
        before = self.query("SELECT * FROM rooms ORDER BY room_path")

        connection = store.connect(self.db_path)
        try:
            connection.execute("DELETE FROM rooms")
            connection.commit()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 0
            )
            store.refresh_rooms(connection)
            connection.commit()
            after = [
                dict(row)
                for row in connection.execute("SELECT * FROM rooms ORDER BY room_path")
            ]
        finally:
            connection.close()

        self.assertEqual(before, after)

    def test_every_observed_room_appears_once(self):
        self.two_snapshots()
        self.build()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM rooms"), 50)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM room_observations"), 100)


class RebuildTests(StoreTestCase):
    def test_a_rebuild_is_idempotent(self):
        self.store.append(snapshot_record())
        self.store.append(snapshot_record(fetched_at="2026-08-27T12:15:00Z"))
        first = self.build()
        first_rows = self.query("SELECT * FROM snapshots ORDER BY id")

        second = self.build()
        second_rows = self.query("SELECT * FROM snapshots ORDER BY id")

        self.assertEqual(first.snapshots_loaded, second.snapshots_loaded)
        self.assertEqual(first_rows, second_rows)

    def test_a_rebuild_picks_up_new_records(self):
        self.store.append(snapshot_record())
        self.build()
        self.store.append(snapshot_record(fetched_at="2026-08-27T12:15:00Z"))
        report = self.build()
        self.assertEqual(report.snapshots_loaded, 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshots"), 2)

    def test_an_update_skips_records_already_loaded(self):
        self.store.append(snapshot_record())
        self.build()
        self.store.append(snapshot_record(fetched_at="2026-08-27T12:15:00Z"))

        report = self.build(rebuild=False)
        self.assertEqual(report.records_read, 2)
        self.assertEqual(report.snapshots_loaded, 1)
        self.assertEqual(report.duplicates_skipped, 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshots"), 2)

    def test_an_update_against_a_missing_database_builds_one(self):
        self.store.append(snapshot_record())
        report = self.build(rebuild=False)
        self.assertEqual(report.snapshots_loaded, 1)

    def test_a_failure_is_not_swallowed_by_an_identical_success(self):
        self.store.append(snapshot_record(fetched_at="2026-08-27T12:00:00Z"))
        self.store.append(
            snapshot_record(
                fetched_at="2026-08-27T12:00:00Z", ok=False, http_status=503,
                error="http 503",
            )
        )
        report = self.build()
        self.assertEqual(report.snapshots_loaded, 2)
        self.assertEqual(report.duplicates_skipped, 0)
        self.assertEqual(report.failed_requests, 1)

    def test_an_update_refreshes_the_derived_rooms_table(self):
        self.store.append(snapshot_record())
        self.build()
        self.store.append(snapshot_record(fetched_at="2026-08-27T12:15:00Z"))
        self.build(rebuild=False)
        self.assertEqual(
            self.scalar("SELECT observation_count FROM rooms WHERE room_path = ?", ("/r/lobby",)),
            2,
        )

    def test_the_database_is_rebuildable_from_the_archive_alone(self):
        self.store.append(snapshot_record())
        self.store.append(failure_record())
        self.build()
        original = self.query("SELECT * FROM snapshots ORDER BY id")

        # Throw the database away entirely, keep only the NDJSON.
        os.unlink(self.db_path)
        self.assertFalse(os.path.exists(self.db_path))

        self.build()
        self.assertEqual(self.query("SELECT * FROM snapshots ORDER BY id"), original)

    def test_an_interrupted_rebuild_leaves_no_half_written_database(self):
        self.store.append(snapshot_record())
        self.build()
        good = self.query("SELECT * FROM snapshots ORDER BY id")

        broken = Archive(os.path.join(self.directory, "archive"))

        def explode():
            raise RuntimeError("interrupted mid-load")
            yield  # pragma: no cover

        broken.iter_records = explode
        with self.assertRaises(RuntimeError):
            store.build(self.db_path, broken)

        # The previous database is untouched and no temporary file is left.
        self.assertEqual(self.query("SELECT * FROM snapshots ORDER BY id"), good)
        self.assertFalse(os.path.exists(self.db_path + ".building"))

    def test_a_database_from_an_older_schema_is_rebuilt_rather_than_topped_up(self):
        self.store.append(snapshot_record())
        self.build()
        connection = store.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = ?", ("0", "schema_version")
            )
            connection.commit()
        finally:
            connection.close()

        report = self.build(rebuild=False)
        self.assertEqual(report.snapshots_loaded, 1)
        self.assertEqual(
            self.scalar("SELECT value FROM meta WHERE key = ?", ("schema_version",)),
            str(store.SCHEMA_VERSION),
        )


class UntrustedInputTests(StoreTestCase):
    def test_a_topic_that_looks_like_sql_is_stored_as_text(self):
        body = (
            "# 1 of 1 rooms (cap 20480, 1.0M of 5.0G stored), newest first\n"
            "# !! UNTRUSTED NAMES\n"
            "/r/x  seq 1  1.0K  0s ago  · '); DROP TABLE rooms; --\n"
            "# notes 1 of 655360 (1.0K total, 50960 per namespace)\n"
            "# engagement over 1 msgs scanned: zero-response 0%, nick diversity 0.0, notes/msg 0.0\n"
        )
        self.store.append(snapshot_record(body=body))
        self.build()

        self.assertEqual(
            self.scalar("SELECT topic FROM room_observations"), "'); DROP TABLE rooms; --"
        )
        # The table it names is still there, which is what bound parameters buy.
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM rooms"), 1)

    def test_a_room_path_with_quotes_round_trips(self):
        body = "/r/odd\"name'here  seq 1  1.0K  0s ago\n"
        self.store.append(snapshot_record(body=body))
        self.build()
        self.assertEqual(self.scalar("SELECT room_path FROM room_observations"), "/r/odd\"name'here")


class ForeignKeyTests(StoreTestCase):
    def test_deleting_a_snapshot_takes_its_children_with_it(self):
        self.store.append(snapshot_record())
        self.build()
        connection = store.connect(self.db_path)
        try:
            connection.execute("DELETE FROM snapshots")
            connection.commit()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM room_observations").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM network_stats").fetchone()[0], 0
            )
        finally:
            connection.close()

    def test_an_orphan_observation_is_rejected(self):
        self.store.append(snapshot_record())
        self.build()
        connection = store.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO room_observations (snapshot_id, room_path) VALUES (?, ?)",
                    (9999, "/r/nowhere"),
                )
        finally:
            connection.close()


class DedupeKeyTests(unittest.TestCase):
    def test_the_same_attempt_has_the_same_key(self):
        one = snapshot_record()
        two = snapshot_record()
        self.assertEqual(store.dedupe_key(one), store.dedupe_key(two))

    def test_a_different_time_is_a_different_attempt(self):
        self.assertNotEqual(
            store.dedupe_key(snapshot_record(fetched_at="2026-08-27T12:00:00Z")),
            store.dedupe_key(snapshot_record(fetched_at="2026-08-27T12:15:00Z")),
        )

    def test_a_different_body_at_the_same_time_is_a_different_attempt(self):
        self.assertNotEqual(
            store.dedupe_key(snapshot_record()),
            store.dedupe_key(snapshot_record(body="something else")),
        )

    def test_a_failure_and_a_success_in_the_same_second_are_different_attempts(self):
        # Replaying one fixture twice produces byte-identical bodies. The
        # status is what tells the two attempts apart, and losing the failure
        # would quietly understate how often the service was down.
        success = snapshot_record(fetched_at="2026-08-27T12:00:00Z")
        failed = snapshot_record(
            fetched_at="2026-08-27T12:00:00Z", ok=False, http_status=503, error="http 503"
        )
        self.assertNotEqual(store.dedupe_key(success), store.dedupe_key(failed))

    def test_a_failure_with_no_body_still_has_a_key(self):
        record = archive.make_record(
            url="u", source="http", ok=False, http_status=None, headers={},
            raw_body=None, elapsed_ms=1, error="network error: timeout",
            backoff_seconds=60.0, fetched_at="2026-08-27T12:00:00Z",
        )
        self.assertIn("timeout", store.dedupe_key(record))


class StoreCommandTests(StoreTestCase):
    def run_cli(self, *args):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = store.main(["--db", self.db_path, "--archive", self.store.root, *args])
        return code, out.getvalue()

    def test_a_clean_build_exits_zero(self):
        self.store.append(snapshot_record())
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("snapshots       1 loaded", out)
        self.assertIn("rooms known     50", out)

    def test_a_flagged_build_exits_one_and_says_why(self):
        self.store.append(snapshot_record(body="not a rooms page"))
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("FLAGGED", out)
        self.assertIn("header-missing", out)

    def test_an_empty_archive_builds_an_empty_database(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("records read    0", out)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshots"), 0)

    def test_update_mode_tops_up(self):
        self.store.append(snapshot_record())
        self.run_cli()
        self.store.append(snapshot_record(fetched_at="2026-08-27T12:15:00Z"))
        code, out = self.run_cli("--update")
        self.assertEqual(code, 0)
        self.assertIn("1 already present", out)


if __name__ == "__main__":
    unittest.main()
