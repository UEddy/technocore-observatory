import json
import os
import tempfile
import unittest

from observatory import archive


class RecordTests(unittest.TestCase):
    def test_body_is_stored_verbatim(self):
        raw = b"# 50 of 17735 rooms\n/r/lobby  seq 1  1.9M  0s ago\n"
        record = archive.make_record(
            url="https://technocore.chat/rooms",
            source="fixture",
            ok=True,
            http_status=200,
            headers={},
            raw_body=raw,
            elapsed_ms=5,
            error=None,
            backoff_seconds=None,
        )
        self.assertEqual(record["body_bytes"], len(raw))
        self.assertEqual(record["body"].encode("utf-8"), raw)
        self.assertEqual(record["body_sha256"], archive.body_digest(raw))
        self.assertNotIn("body_base64", record)
        self.assertIsNone(record["parse_version"])

    def test_the_saved_fixture_survives_a_round_trip(self):
        # The saved fixture is not valid UTF-8, which is exactly the case the
        # base64 copy exists for. The archived record must still reproduce the
        # response byte for byte.
        import base64

        with open("fixtures/rooms-sample.txt", "rb") as handle:
            raw = handle.read()
        record = archive.make_record(
            url="https://technocore.chat/rooms",
            source="fixture",
            ok=True,
            http_status=200,
            headers={},
            raw_body=raw,
            elapsed_ms=5,
            error=None,
            backoff_seconds=None,
        )
        self.assertEqual(record["body_bytes"], len(raw))
        self.assertEqual(record["body_sha256"], archive.body_digest(raw))
        self.assertEqual(base64.b64decode(record["body_base64"]), raw)

    def test_non_utf8_body_keeps_a_base64_copy(self):
        raw = b"\xff\xfe not utf-8"
        record = archive.make_record(
            url="u", source="fixture", ok=True, http_status=200, headers={},
            raw_body=raw, elapsed_ms=1, error=None, backoff_seconds=None,
        )
        import base64

        self.assertEqual(base64.b64decode(record["body_base64"]), raw)
        self.assertEqual(record["body_sha256"], archive.body_digest(raw))

    def test_missing_body_is_none_not_empty(self):
        record = archive.make_record(
            url="u", source="http", ok=False, http_status=None, headers={},
            raw_body=None, elapsed_ms=1, error="network error: timeout",
            backoff_seconds=60.0,
        )
        self.assertIsNone(record["body"])
        self.assertIsNone(record["body_bytes"])
        self.assertEqual(record["backoff_seconds"], 60.0)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "sub", "rooms.ndjson")

    def _write(self, count, ok=True):
        for index in range(count):
            archive.append(
                self.path,
                archive.make_record(
                    url="u", source="fixture", ok=ok, http_status=200 if ok else 503,
                    headers={}, raw_body=f"body {index}".encode(), elapsed_ms=1,
                    error=None if ok else "http 503",
                    backoff_seconds=None if ok else 60.0,
                ),
            )

    def test_append_creates_directories_and_one_line_per_record(self):
        self._write(3)
        with open(self.path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["body"], "body 0")

    def test_multiline_body_stays_on_one_line(self):
        archive.append(
            self.path,
            archive.make_record(
                url="u", source="fixture", ok=True, http_status=200, headers={},
                raw_body=b"line one\nline two\nline three", elapsed_ms=1,
                error=None, backoff_seconds=None,
            ),
        )
        with open(self.path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["body"], "line one\nline two\nline three")

    def test_read_tail_returns_the_most_recent_oldest_first(self):
        self._write(10)
        tail = archive.read_tail(self.path, limit=3)
        self.assertEqual([record["body"] for record in tail], ["body 7", "body 8", "body 9"])

    def test_read_tail_across_chunk_boundaries(self):
        self._write(400)
        tail = archive.read_tail(self.path, limit=250)
        self.assertEqual(len(tail), 250)
        self.assertEqual(tail[-1]["body"], "body 399")
        self.assertEqual(tail[0]["body"], "body 150")

    def test_read_tail_larger_than_the_file(self):
        self._write(2)
        self.assertEqual(len(archive.read_tail(self.path, limit=100)), 2)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(archive.read_tail(os.path.join(self.directory, "nope.ndjson")), [])
        self.assertEqual(list(archive.iter_records(os.path.join(self.directory, "nope.ndjson"))), [])

    def test_a_corrupt_line_does_not_stop_the_reader(self):
        self._write(2)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write('{"half written"\n')
        archive.append(
            self.path,
            archive.make_record(
                url="u", source="fixture", ok=True, http_status=200, headers={},
                raw_body=b"after", elapsed_ms=1, error=None, backoff_seconds=None,
            ),
        )
        tail = archive.read_tail(self.path, limit=10)
        self.assertEqual([record["body"] for record in tail], ["body 0", "body 1", "after"])
        self.assertEqual(len(list(archive.iter_records(self.path))), 3)

    def test_timestamps_round_trip(self):
        stamp = archive.utc_now_iso()
        self.assertTrue(stamp.endswith("Z"))
        self.assertEqual(archive.parse_iso(stamp).tzinfo.utcoffset(None).total_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
