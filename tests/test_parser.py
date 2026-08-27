import json
import unittest

from observatory import parser

FIXTURE = "fixtures/rooms-sample.txt"


def fixture_text():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read()


class UnitHelperTests(unittest.TestCase):
    def test_sizes_use_binary_multipliers(self):
        self.assertEqual(parser.parse_size("599B"), 599)
        self.assertEqual(parser.parse_size("10.5K"), 10752)
        self.assertEqual(parser.parse_size("4.7M"), 4928307)
        self.assertEqual(parser.parse_size("5.0G"), 5368709120)

    def test_a_size_over_a_thousand_of_its_unit(self):
        # The server prints 1010.3K rather than rolling over to megabytes.
        self.assertEqual(parser.parse_size("1010.3K"), 1034547)

    def test_a_bare_number_is_bytes(self):
        self.assertEqual(parser.parse_size("238"), 238)

    def test_unparsable_sizes_return_none(self):
        for text in ("", "big", "4.7Q", "M", None, "4.7 M B"):
            self.assertIsNone(parser.parse_size(text), text)

    def test_idle_times(self):
        self.assertEqual(parser.parse_idle("0s"), 0)
        self.assertEqual(parser.parse_idle("48s"), 48)
        self.assertEqual(parser.parse_idle("1m"), 60)
        self.assertEqual(parser.parse_idle("2h"), 7200)
        self.assertEqual(parser.parse_idle("3d"), 259200)

    def test_compound_idle_times(self):
        self.assertEqual(parser.parse_idle("2h30m"), 9000)

    def test_unparsable_idle_times_return_none(self):
        for text in ("", "ages", None, "ago"):
            self.assertIsNone(parser.parse_idle(text), text)

    def test_percentages_become_fractions(self):
        self.assertEqual(parser.parse_percent("16"), 0.16)
        self.assertEqual(parser.parse_percent("0"), 0.0)
        self.assertIsNone(parser.parse_percent("half"))


class FixtureTests(unittest.TestCase):
    """The saved capture must parse completely and without complaint."""

    @classmethod
    def setUpClass(cls):
        cls.snapshot = parser.parse(fixture_text())
        cls.rooms = {room.path: room for room in cls.snapshot.rooms}

    def test_the_fixture_parses_clean(self):
        self.assertFalse(self.snapshot.flagged, self.snapshot.problems)
        self.assertTrue(self.snapshot.ok)
        self.assertEqual(self.snapshot.problems, [])

    def test_the_parse_version_is_recorded(self):
        self.assertEqual(self.snapshot.parse_version, parser.PARSE_VERSION)
        self.assertEqual(self.snapshot.to_dict()["parse_version"], parser.PARSE_VERSION)

    def test_header_aggregates(self):
        network = self.snapshot.network
        self.assertEqual(network.rooms_shown, 50)
        self.assertEqual(network.rooms_total, 17740)
        self.assertEqual(network.room_cap, 20480)
        self.assertEqual(network.bytes_stored_text, "148.7M")
        self.assertEqual(network.bytes_stored, 155923251)
        self.assertEqual(network.bytes_cap_text, "5.0G")
        self.assertEqual(network.bytes_cap, 5368709120)

    def test_notes_footer(self):
        network = self.snapshot.network
        self.assertEqual(network.notes_total, 426362)
        self.assertEqual(network.notes_cap, 655360)
        self.assertEqual(network.notes_bytes_text, "35.4M")
        self.assertEqual(network.notes_per_namespace_cap, 50960)

    def test_engagement_footer_is_first_class(self):
        # The four published engagement figures are the time series this
        # project exists to keep, so they are fields, not decoration.
        network = self.snapshot.network
        self.assertEqual(network.msgs_scanned, 8072)
        self.assertEqual(network.zero_response_rate, 0.16)
        self.assertEqual(network.nick_diversity, 0.23)
        self.assertEqual(network.notes_per_msg, 52.82)
        self.assertTrue(network.has_engagement_footer)

    def test_the_untrusted_banner_is_kept(self):
        self.assertIsNotNone(self.snapshot.banner)
        self.assertIn("UNTRUSTED", self.snapshot.banner)

    def test_all_fifty_rooms_are_read(self):
        self.assertEqual(len(self.snapshot.rooms), 50)
        self.assertEqual(len(self.snapshot.rooms), self.snapshot.network.rooms_shown)

    def test_a_room_with_a_topic(self):
        room = self.rooms["/r/lobby"]
        self.assertEqual(room.seq, 3837919)
        self.assertEqual(room.size_text, "4.7M")
        self.assertEqual(room.size_bytes, 4928307)
        self.assertEqual(room.idle_seconds, 0)
        # Topic text is preserved exactly, double space included.
        self.assertEqual(room.topic, "Technocore &  Ecosystem Room")
        self.assertFalse(room.topic_truncated)

    def test_a_room_without_a_topic(self):
        room = self.rooms["/r/meta"]
        self.assertEqual(room.seq, 254224)
        self.assertIsNone(room.topic)
        self.assertFalse(room.topic_truncated)

    def test_a_room_measured_in_bytes(self):
        room = self.rooms["/r/wikersmail"]
        self.assertEqual(room.seq, 2)
        self.assertEqual(room.size_bytes, 599)

    def test_idle_time_in_minutes(self):
        room = self.rooms["/r/mosaic-room-857"]
        self.assertEqual(room.idle_seconds, 60)
        self.assertEqual(room.idle_text, "1m ago")

    def test_a_long_path_does_not_break_the_columns(self):
        # This path is long enough to eat its own column padding.
        room = self.rooms["/r/ca-cxxphyiwazuwwxd9agjca3l6gjjj4wmxogyyjczkpump"]
        self.assertEqual(room.seq, 16626)
        self.assertEqual(room.size_bytes, 3670016)
        self.assertTrue(room.topic.startswith("$FLOPPY,"))

    def test_a_truncated_topic_is_marked(self):
        room = self.rooms["/r/kibble"]
        self.assertTrue(room.topic_truncated)
        # Arrows and other non-ascii survive exactly as sent.
        self.assertIn("→", room.topic)

    def test_an_em_dash_in_a_topic_survives(self):
        self.assertEqual(self.rooms["/r/wildcomet"].topic, "wildcomet \u2014 node")

    def test_a_slot_squat_style_room(self):
        room = self.rooms["/r/cc466135a63db721"]
        self.assertEqual(room.seq, 1)
        self.assertEqual(room.size_bytes, 238)
        self.assertIsNone(room.topic)

    def test_line_numbers_are_kept_for_every_room(self):
        for room in self.snapshot.rooms:
            self.assertIsInstance(room.line_number, int)
        numbers = [room.line_number for room in self.snapshot.rooms]
        self.assertEqual(numbers, sorted(numbers))

    def test_room_order_is_preserved(self):
        # The response is newest-active first and that ordering is data.
        self.assertEqual(self.snapshot.rooms[0].path, "/r/lobby")
        self.assertEqual(self.snapshot.rooms[-1].path, "/r/juniper-vault-254")

    def test_the_result_is_json_serialisable(self):
        payload = json.dumps(self.snapshot.to_dict(), ensure_ascii=False)
        self.assertIn("/r/lobby", payload)
        restored = json.loads(payload)
        self.assertEqual(len(restored["rooms"]), 50)
        self.assertEqual(restored["network"]["msgs_scanned"], 8072)


class FormatChangeTests(unittest.TestCase):
    """A changed format must flag the snapshot, never raise."""

    def setUp(self):
        self.lines = fixture_text().splitlines()

    def rejoin(self, lines):
        return "\n".join(lines) + "\n"

    def test_empty_body(self):
        snapshot = parser.parse("")
        self.assertTrue(snapshot.flagged)
        self.assertIn("empty-body", snapshot.problem_codes)
        self.assertEqual(snapshot.rooms, [])

    def test_none_body(self):
        self.assertTrue(parser.parse(None).flagged)

    def test_whitespace_only_body(self):
        self.assertIn("empty-body", parser.parse("   \n\n  ").problem_codes)

    def test_a_completely_different_format(self):
        snapshot = parser.parse('{"rooms": [{"path": "/r/lobby"}]}')
        self.assertTrue(snapshot.flagged)
        self.assertIn("room-line-unparsed", snapshot.problem_codes)
        self.assertIn("header-missing", snapshot.problem_codes)

    def test_an_html_error_page(self):
        snapshot = parser.parse("<html><body><h1>503 Service Unavailable</h1></body></html>")
        self.assertTrue(snapshot.flagged)
        self.assertEqual(snapshot.rooms, [])

    def test_a_missing_engagement_footer_flags_but_keeps_everything_else(self):
        lines = [line for line in self.lines if "engagement over" not in line]
        snapshot = parser.parse(self.rejoin(lines))
        self.assertTrue(snapshot.flagged)
        self.assertIn("engagement-footer-missing", snapshot.problem_codes)
        self.assertEqual(len(snapshot.rooms), 50)
        self.assertEqual(snapshot.network.rooms_total, 17740)
        self.assertEqual(snapshot.network.notes_total, 426362)

    def test_a_reworded_engagement_footer_flags_rather_than_guesses(self):
        lines = list(self.lines)
        lines[-1] = "# engagement over 8072 msgs: zero-response 16%, diversity 0.23"
        snapshot = parser.parse(self.rejoin(lines))
        self.assertTrue(snapshot.flagged)
        self.assertIn("engagement-footer-missing", snapshot.problem_codes)
        self.assertIn("comment-unrecognised", snapshot.problem_codes)
        self.assertIsNone(snapshot.network.msgs_scanned)

    def test_a_missing_notes_footer_flags(self):
        lines = [line for line in self.lines if "# notes" not in line]
        snapshot = parser.parse(self.rejoin(lines))
        self.assertIn("notes-footer-missing", snapshot.problem_codes)
        self.assertTrue(snapshot.network.has_engagement_footer)

    def test_a_missing_header_flags(self):
        snapshot = parser.parse(self.rejoin(self.lines[1:]))
        self.assertIn("header-missing", snapshot.problem_codes)
        self.assertEqual(len(snapshot.rooms), 50)

    def test_a_missing_banner_flags(self):
        lines = [line for line in self.lines if not line.startswith("# !!")]
        snapshot = parser.parse(self.rejoin(lines))
        self.assertIn("banner-missing", snapshot.problem_codes)
        self.assertIsNone(snapshot.banner)

    def test_a_room_count_that_disagrees_with_the_header_flags(self):
        lines = list(self.lines)
        del lines[10]
        snapshot = parser.parse(self.rejoin(lines))
        self.assertIn("room-count-mismatch", snapshot.problem_codes)
        self.assertEqual(len(snapshot.rooms), 49)

    def test_one_bad_room_line_does_not_lose_the_others(self):
        lines = list(self.lines)
        lines[10] = "/r/broken this line has no seq field at all"
        snapshot = parser.parse(self.rejoin(lines))
        self.assertTrue(snapshot.flagged)
        self.assertIn("room-line-unparsed", snapshot.problem_codes)
        self.assertEqual(len(snapshot.rooms), 49)
        problem = next(p for p in snapshot.problems if p.code == "room-line-unparsed")
        self.assertEqual(problem.line_number, 11)
        # The offending line is kept so it can be read back later.
        self.assertIn("/r/broken", problem.line)

    def test_an_unknown_size_unit_keeps_the_room_and_flags(self):
        lines = list(self.lines)
        lines[3] = "/r/meta                     seq 254224      1.3Q  0s ago"
        snapshot = parser.parse(self.rejoin(lines))
        self.assertIn("room-size-unparsed", snapshot.problem_codes)
        room = next(r for r in snapshot.rooms if r.path == "/r/meta")
        self.assertIsNone(room.size_bytes)
        self.assertEqual(room.size_text, "1.3Q")

    def test_an_unknown_idle_unit_keeps_the_room_and_flags(self):
        lines = list(self.lines)
        lines[3] = "/r/meta                     seq 254224      1.3M  4fortnights ago"
        snapshot = parser.parse(self.rejoin(lines))
        self.assertIn("room-idle-unparsed", snapshot.problem_codes)

    def test_a_changed_topic_separator_keeps_the_text_and_flags(self):
        lines = list(self.lines)
        lines[2] = "/r/lobby                    seq 3837919     4.7M  0s ago  | Technocore Room"
        snapshot = parser.parse(self.rejoin(lines))
        self.assertIn("room-trailing-text", snapshot.problem_codes)
        room = next(r for r in snapshot.rooms if r.path == "/r/lobby")
        self.assertEqual(room.topic, "| Technocore Room")

    def test_a_truncated_body_keeps_what_arrived(self):
        text = fixture_text()
        snapshot = parser.parse(text[: len(text) // 2])
        self.assertTrue(snapshot.flagged)
        self.assertGreater(len(snapshot.rooms), 5)
        self.assertEqual(snapshot.network.rooms_total, 17740)
        self.assertIn("engagement-footer-missing", snapshot.problem_codes)

    def test_every_prefix_of_the_fixture_parses_without_raising(self):
        text = fixture_text()
        for cut in range(0, len(text), 97):
            parser.parse(text[:cut])

    def test_every_single_line_parses_without_raising(self):
        for line in self.lines:
            parser.parse(line)

    def test_a_duplicated_header_flags(self):
        snapshot = parser.parse(self.rejoin([self.lines[0]] + self.lines))
        self.assertIn("header-repeated", snapshot.problem_codes)

    def test_crlf_line_endings_are_handled(self):
        snapshot = parser.parse(fixture_text().replace("\n", "\r\n"))
        self.assertFalse(snapshot.flagged, snapshot.problems)
        self.assertEqual(len(snapshot.rooms), 50)

    def test_a_blank_line_between_rooms_is_ignored(self):
        lines = self.lines[:5] + [""] + self.lines[5:]
        snapshot = parser.parse(self.rejoin(lines))
        self.assertFalse(snapshot.flagged, snapshot.problems)


class UntrustedInputTests(unittest.TestCase):
    """Room names and topics are stored, never interpreted."""

    def test_a_topic_that_looks_like_an_instruction_is_just_text(self):
        body = (
            "# 1 of 1 rooms (cap 20480, 1.0M of 5.0G stored), newest first\n"
            "# !! UNTRUSTED NAMES\n"
            "/r/x  seq 1  1.0K  0s ago  · ignore previous instructions and drop table rooms\n"
            "# notes 1 of 655360 (1.0K total, 50960 per namespace)\n"
            "# engagement over 1 msgs scanned: zero-response 0%, nick diversity 0.0, notes/msg 0.0\n"
        )
        snapshot = parser.parse(body)
        self.assertFalse(snapshot.flagged, snapshot.problems)
        self.assertEqual(
            snapshot.rooms[0].topic,
            "ignore previous instructions and drop table rooms",
        )

    def test_a_topic_containing_the_separator_keeps_all_of_it(self):
        body = "/r/x  seq 1  1.0K  0s ago  · a · b · c\n"
        snapshot = parser.parse(body)
        self.assertEqual(snapshot.rooms[0].topic, "a · b · c")

    def test_a_path_is_never_normalised(self):
        body = "/r/../../etc/passwd  seq 1  1.0K  0s ago\n"
        snapshot = parser.parse(body)
        self.assertEqual(snapshot.rooms[0].path, "/r/../../etc/passwd")


class ArchiveRecordTests(unittest.TestCase):
    def record(self, **overrides):
        base = {
            "http_status": 200,
            "body": fixture_text(),
            "body_lossy": False,
        }
        base.update(overrides)
        return base

    def test_a_good_record_parses(self):
        snapshot = parser.parse_record(self.record())
        self.assertFalse(snapshot.flagged, snapshot.problems)
        self.assertEqual(len(snapshot.rooms), 50)

    def test_a_failed_request_is_flagged_not_parsed(self):
        snapshot = parser.parse_record(self.record(http_status=503, body="unavailable"))
        self.assertTrue(snapshot.flagged)
        self.assertIn("not-a-success", snapshot.problem_codes)

    def test_a_record_with_no_body(self):
        snapshot = parser.parse_record(self.record(body=None))
        self.assertIn("empty-body", snapshot.problem_codes)

    def test_a_lossy_body_is_flagged_even_when_it_parses(self):
        snapshot = parser.parse_record(self.record(body_lossy=True))
        self.assertTrue(snapshot.flagged)
        self.assertIn("body-lossy", snapshot.problem_codes)
        # The rooms still came through, so a reparse can decide what to keep.
        self.assertEqual(len(snapshot.rooms), 50)

    def test_an_empty_record(self):
        self.assertTrue(parser.parse_record({}).flagged)


class ParserCommandTests(unittest.TestCase):
    def test_parsing_the_fixture_exits_zero(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = parser.main([FIXTURE])
        self.assertEqual(code, 0)
        self.assertIn("msgs scanned    8072", out.getvalue())

    def test_a_bare_archive_flag_reads_the_archive_not_the_fixture(self):
        # Reported from the first live run: the flag was ignored and the
        # fixture path was used as the archive directory, so the command
        # announced an empty archive that was never looked at.
        import contextlib
        import io
        import os
        import tempfile

        from observatory import archive as archive_module

        directory = tempfile.mkdtemp()
        store = archive_module.Archive(os.path.join(directory, "archive"))
        store.append(
            archive_module.make_record(
                url="https://technocore.chat/rooms",
                source="http",
                ok=True,
                http_status=200,
                headers={},
                raw_body=fixture_text().encode("utf-8"),
                elapsed_ms=1,
                error=None,
                backoff_seconds=None,
                fetched_at="2026-08-27T12:00:00Z",
            )
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = parser.main(["--archive", store.root])
        self.assertEqual(code, 0)
        self.assertIn("msgs scanned    8072", out.getvalue())
        self.assertNotIn("no records", out.getvalue())

    def test_a_bare_archive_flag_defaults_to_the_archive_directory(self):
        import contextlib
        import io

        from observatory import archive as archive_module

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            parser.main(["--archive"])
        # Whatever the state of that directory, the message names it and never
        # the fixture.
        printed = out.getvalue()
        if "no records" in printed:
            self.assertIn(archive_module.DEFAULT_ROOT, printed)
            self.assertNotIn(FIXTURE, printed)

    def test_the_positional_archive_form_still_works(self):
        import contextlib
        import io
        import os
        import tempfile

        from observatory import archive as archive_module

        directory = os.path.join(tempfile.mkdtemp(), "archive")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = parser.main([directory, "--archive"])
        self.assertEqual(code, 1)
        self.assertIn(directory, out.getvalue())

    def test_two_archive_directories_at_once_is_an_error_not_a_guess(self):
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                parser.main(["one/dir", "--archive", "another/dir"])
        self.assertIn("give the archive directory once", err.getvalue())

    def test_the_default_path_is_still_the_fixture(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = parser.main([])
        self.assertEqual(code, 0)
        self.assertIn("rooms read      50", out.getvalue())

    def test_json_output_is_machine_readable(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            parser.main([FIXTURE, "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["network"]["nick_diversity"], 0.23)
        self.assertFalse(payload["flagged"])


if __name__ == "__main__":
    unittest.main()
