import builtins
import json
import os
import tempfile
import unittest

from observatory import archive
from observatory.archive import Archive

FIXTURE = "fixtures/rooms-sample.txt"


def record(body="body", fetched_at=None, ok=True):
    return archive.make_record(
        url="https://technocore.chat/rooms",
        source="fixture",
        ok=ok,
        http_status=200 if ok else 503,
        headers={},
        raw_body=body.encode("utf-8"),
        elapsed_ms=1,
        error=None if ok else "http 503",
        backoff_seconds=None if ok else 60.0,
        fetched_at=fetched_at,
    )


class RecordTests(unittest.TestCase):
    def make(self, raw_body, **overrides):
        fields = dict(
            url="https://technocore.chat/rooms",
            source="fixture",
            ok=True,
            http_status=200,
            headers={},
            raw_body=raw_body,
            elapsed_ms=5,
            error=None,
            backoff_seconds=None,
        )
        fields.update(overrides)
        return archive.make_record(**fields)

    def test_body_is_stored_verbatim(self):
        with open(FIXTURE, "rb") as handle:
            raw = handle.read()
        stored = self.make(raw)
        self.assertEqual(stored["body_bytes"], len(raw))
        self.assertEqual(stored["body"].encode("utf-8"), raw)
        self.assertEqual(stored["body_sha256"], archive.body_digest(raw))
        self.assertEqual(stored["body_encoding"], "utf-8")
        self.assertFalse(stored["body_lossy"])
        self.assertIsNone(stored["parse_version"])

    def test_every_record_carries_the_same_keys(self):
        # Body storage is uniform: nothing about the response changes which
        # fields exist, only their values.
        with open(FIXTURE, "rb") as handle:
            raw = handle.read()
        success = self.make(raw)
        undecodable = self.make(b"\xff\xfe not utf-8")
        empty = self.make(b"")
        no_response = self.make(
            None, ok=False, http_status=None, error="network error: timeout",
            backoff_seconds=60.0,
        )
        keys = set(success)
        for stored in (undecodable, empty, no_response):
            self.assertEqual(set(stored), keys)

    def test_an_undecodable_body_is_stored_and_flagged_not_dropped(self):
        stored = self.make(b"\xff\xfe not utf-8")
        self.assertEqual(stored["body_encoding"], "utf-8-replace")
        self.assertTrue(stored["body_lossy"])
        self.assertIn("not utf-8", stored["body"])
        # The digest still identifies the bytes as they came off the wire, so
        # a lossy record can be told apart from its original.
        self.assertEqual(stored["body_sha256"], archive.body_digest(b"\xff\xfe not utf-8"))
        self.assertNotEqual(
            stored["body_sha256"], archive.body_digest(stored["body"].encode("utf-8"))
        )

    def test_an_empty_body_is_not_confused_with_no_body(self):
        empty = self.make(b"")
        self.assertEqual(empty["body"], "")
        self.assertEqual(empty["body_bytes"], 0)
        self.assertFalse(empty["body_lossy"])

    def test_missing_body_is_none_throughout(self):
        stored = self.make(
            None, ok=False, http_status=None, error="network error: timeout",
            backoff_seconds=60.0,
        )
        self.assertIsNone(stored["body"])
        self.assertIsNone(stored["body_bytes"])
        self.assertIsNone(stored["body_sha256"])
        self.assertIsNone(stored["body_encoding"])
        self.assertFalse(stored["body_lossy"])
        self.assertEqual(stored["backoff_seconds"], 60.0)

    def test_decode_body_reports_how_it_decoded(self):
        self.assertEqual(archive.decode_body(b"plain"), ("plain", "utf-8", False))
        text, encoding, lossy = archive.decode_body(b"\xff")
        self.assertEqual(encoding, "utf-8-replace")
        self.assertTrue(lossy)
        self.assertEqual(len(text), 1)


class FileTests(unittest.TestCase):
    """The single-file primitives the Archive is built out of."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "sub", "2026-08.ndjson")

    def _write(self, count):
        for index in range(count):
            archive.append_to_file(self.path, record(f"body {index}"))

    def test_append_creates_directories_and_one_line_per_record(self):
        self._write(3)
        with open(self.path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["body"], "body 0")

    def test_multiline_body_stays_on_one_line(self):
        archive.append_to_file(self.path, record("line one\nline two\nline three"))
        with open(self.path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["body"], "line one\nline two\nline three")

    def test_the_archive_stays_greppable(self):
        # Non-ASCII from the endpoint is written through, not escaped, so the
        # committed history can be searched with plain tools.
        archive.append_to_file(self.path, record("wildcomet \u2014 node"))
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertIn("wildcomet \u2014 node", handle.read())

    def test_read_file_tail_returns_the_most_recent_oldest_first(self):
        self._write(10)
        tail = archive.read_file_tail(self.path, limit=3)
        self.assertEqual([r["body"] for r in tail], ["body 7", "body 8", "body 9"])

    def test_read_file_tail_across_chunk_boundaries(self):
        self._write(400)
        tail = archive.read_file_tail(self.path, limit=250)
        self.assertEqual(len(tail), 250)
        self.assertEqual(tail[-1]["body"], "body 399")
        self.assertEqual(tail[0]["body"], "body 150")

    def test_read_file_tail_larger_than_the_file(self):
        self._write(2)
        self.assertEqual(len(archive.read_file_tail(self.path, limit=100)), 2)

    def test_missing_file_is_empty_not_an_error(self):
        missing = os.path.join(self.directory, "nope.ndjson")
        self.assertEqual(archive.read_file_tail(missing), [])
        self.assertEqual(list(archive.iter_file_records(missing)), [])

    def test_a_corrupt_line_does_not_stop_the_reader(self):
        self._write(2)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write('{"half written"\n')
        archive.append_to_file(self.path, record("after"))
        tail = archive.read_file_tail(self.path, limit=10)
        self.assertEqual([r["body"] for r in tail], ["body 0", "body 1", "after"])
        self.assertEqual(len(list(archive.iter_file_records(self.path))), 3)

    def test_timestamps_round_trip(self):
        stamp = archive.utc_now_iso()
        self.assertTrue(stamp.endswith("Z"))
        self.assertEqual(archive.parse_iso(stamp).tzinfo.utcoffset(None).total_seconds(), 0)


class RotationTests(unittest.TestCase):
    """Records are filed by month, and reads cross the boundary invisibly."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.store = Archive(self.directory)

    def test_a_record_is_filed_under_the_month_it_happened_in(self):
        self.store.append(record("july", fetched_at="2026-07-31T23:59:59Z"))
        self.store.append(record("august", fetched_at="2026-08-01T00:00:01Z"))
        names = [os.path.basename(path) for path in self.store.files()]
        self.assertEqual(names, ["2026-07.ndjson", "2026-08.ndjson"])

    def test_the_month_comes_from_the_record_not_the_clock(self):
        # A run that crosses midnight on the first of the month must not file
        # the earlier attempt under the new month.
        path = self.store.append(record("late", fetched_at="2026-07-31T23:59:59Z"))
        self.assertTrue(path.endswith("2026-07.ndjson"))

    def test_files_are_listed_oldest_first(self):
        for stamp in ("2026-09-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"):
            self.store.append(record("x", fetched_at=stamp))
        names = [os.path.basename(path) for path in self.store.files()]
        self.assertEqual(names, ["2026-07.ndjson", "2026-08.ndjson", "2026-09.ndjson"])

    def test_a_tail_that_fits_in_the_current_month_reads_one_file(self):
        for index in range(5):
            self.store.append(record(f"aug {index}", fetched_at=f"2026-08-0{index + 1}T00:00:00Z"))
        tail = self.store.read_tail(limit=3)
        self.assertEqual([r["body"] for r in tail], ["aug 2", "aug 3", "aug 4"])

    def test_a_tail_spans_a_month_boundary(self):
        for index in range(3):
            self.store.append(record(f"jul {index}", fetched_at=f"2026-07-0{index + 1}T00:00:00Z"))
        for index in range(2):
            self.store.append(record(f"aug {index}", fetched_at=f"2026-08-0{index + 1}T00:00:00Z"))

        tail = self.store.read_tail(limit=4)
        self.assertEqual([r["body"] for r in tail], ["jul 1", "jul 2", "aug 0", "aug 1"])

    def test_a_tail_spans_several_month_boundaries(self):
        months = ["2026-05", "2026-06", "2026-07", "2026-08"]
        for month in months:
            for index in range(2):
                self.store.append(
                    record(f"{month} {index}", fetched_at=f"{month}-0{index + 1}T00:00:00Z")
                )
        tail = self.store.read_tail(limit=7)
        self.assertEqual(len(tail), 7)
        self.assertEqual(tail[0]["body"], "2026-05 1")
        self.assertEqual(tail[-1]["body"], "2026-08 1")

    def test_a_tail_longer_than_the_whole_archive(self):
        self.store.append(record("only", fetched_at="2026-08-01T00:00:00Z"))
        self.assertEqual(len(self.store.read_tail(limit=500)), 1)

    def test_an_empty_or_missing_directory_reads_as_empty(self):
        self.assertEqual(self.store.read_tail(limit=10), [])
        self.assertEqual(Archive(os.path.join(self.directory, "nope")).read_tail(), [])
        self.assertEqual(Archive(os.path.join(self.directory, "nope")).files(), [])

    def test_stray_files_in_the_directory_are_ignored(self):
        self.store.append(record("real", fetched_at="2026-08-01T00:00:00Z"))
        for name in ("README.md", "notes.ndjson", "2026-8.ndjson", "2026-08.ndjson.bak"):
            with open(os.path.join(self.directory, name), "w", encoding="utf-8") as handle:
                handle.write("not an archive file\n")
        self.assertEqual(len(self.store.files()), 1)
        self.assertEqual(len(self.store.read_tail(limit=10)), 1)

    def test_iter_records_walks_every_month_oldest_first(self):
        self.store.append(record("jul", fetched_at="2026-07-01T00:00:00Z"))
        self.store.append(record("aug", fetched_at="2026-08-01T00:00:00Z"))
        self.store.append(record("sep", fetched_at="2026-09-01T00:00:00Z"))
        self.assertEqual([r["body"] for r in self.store.iter_records()], ["jul", "aug", "sep"])
        self.assertEqual(self.store.count(), 3)

    def test_a_corrupt_line_in_an_older_month_does_not_break_a_tail(self):
        self.store.append(record("jul", fetched_at="2026-07-01T00:00:00Z"))
        with open(self.store.path_for("2026-07-01T00:00:00Z"), "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        self.store.append(record("aug", fetched_at="2026-08-01T00:00:00Z"))
        self.assertEqual([r["body"] for r in self.store.read_tail(limit=5)], ["jul", "aug"])

    def test_an_unreadable_timestamp_files_under_the_current_month(self):
        import datetime as datetime_module

        path = self.store.append(record("odd", fetched_at="not a timestamp"))
        expected = datetime_module.datetime.now(datetime_module.timezone.utc).strftime("%Y-%m")
        self.assertTrue(path.endswith(f"{expected}.ndjson"))


class BoundaryStateTests(unittest.TestCase):
    """The budget and the backoff ladder must not reset at a month boundary."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.store = Archive(self.directory)

    def test_the_budget_window_counts_across_a_boundary(self):
        import datetime as datetime_module

        from observatory.budget import Budget

        # Thirty attempts straddling midnight on the first of the month, all
        # inside the same hour and therefore all inside the same window.
        base = datetime_module.datetime(
            2026, 7, 31, 23, 45, 0, tzinfo=datetime_module.timezone.utc
        )
        for index in range(30):
            stamp = (base + datetime_module.timedelta(minutes=index)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            self.store.append(record("x", fetched_at=stamp))

        self.assertEqual(len(self.store.files()), 2)
        now = base + datetime_module.timedelta(minutes=31)
        window = Budget(self.store)
        self.assertEqual(window.used(now), 30)
        self.assertEqual(window.remaining(now), 0)

    def test_the_backoff_ladder_continues_across_a_boundary(self):
        from observatory.fetcher import derive_backoff_state

        self.store.append(
            archive.make_record(
                url="u", source="t", ok=False, http_status=503, headers={},
                raw_body=b"", elapsed_ms=1, error="http 503", backoff_seconds=60.0,
                fetched_at="2026-07-31T23:58:00Z",
            )
        )
        self.store.append(
            archive.make_record(
                url="u", source="t", ok=False, http_status=503, headers={},
                raw_body=b"", elapsed_ms=1, error="http 503", backoff_seconds=120.0,
                fetched_at="2026-08-01T00:00:00Z",
            )
        )
        state = derive_backoff_state(self.store.read_tail(limit=50))
        self.assertEqual(state.consecutive_failures, 2)
        self.assertEqual(state.previous_delay, 120.0)

    def test_a_success_in_a_previous_month_still_clears_the_ladder(self):
        from observatory.fetcher import derive_backoff_state

        self.store.append(record("ok", fetched_at="2026-07-31T23:00:00Z"))
        state = derive_backoff_state(self.store.read_tail(limit=50))
        self.assertEqual(state.consecutive_failures, 0)


class CountingOpen:
    """Wraps builtins.open to count how many bytes a reader actually pulls."""

    def __init__(self, *targets):
        self.targets = {os.path.abspath(target) for target in targets}
        self.bytes_read = 0
        self._real = builtins.open

    def __enter__(self):
        outer = self

        def counting_open(file, *args, **kwargs):
            handle = outer._real(file, *args, **kwargs)
            try:
                same = os.path.abspath(file) in outer.targets
            except TypeError:
                same = False
            if not same:
                return handle
            return _CountingHandle(handle, outer)

        builtins.open = counting_open
        return self

    def __exit__(self, *_exc):
        builtins.open = self._real


class _CountingHandle:
    def __init__(self, inner, counter):
        self._inner = inner
        self._counter = counter

    def read(self, *args, **kwargs):
        data = self._inner.read(*args, **kwargs)
        self._counter.bytes_read += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __iter__(self):
        for line in self._inner:
            self._counter.bytes_read += len(line)
            yield line


class TailSeekTests(unittest.TestCase):
    """Tail reads must seek the end of the archive, never walk all of it."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.store = Archive(self.directory)
        # Two months of archive, bodies about the size of a real response,
        # so each month file alone is a couple of megabytes.
        body = "x" * 4700
        for month in ("2026-07", "2026-08"):
            for index in range(500):
                day = (index % 28) + 1
                self.store.append(record(body, fetched_at=f"{month}-{day:02d}T00:00:00Z"))
        self.files = self.store.files()
        self.size = sum(os.path.getsize(path) for path in self.files)

    def test_the_archive_is_big_enough_for_the_test_to_mean_something(self):
        self.assertEqual(len(self.files), 2)
        self.assertGreater(self.size, 4_000_000)

    def test_a_tail_reads_far_less_than_the_whole_archive(self):
        with CountingOpen(*self.files) as counter:
            tail = self.store.read_tail(limit=50)
        self.assertEqual(len(tail), 50)
        self.assertLess(counter.bytes_read, self.size // 4)

    def test_a_tail_does_not_touch_older_months_it_does_not_need(self):
        older = self.files[0]
        with CountingOpen(older) as counter:
            self.store.read_tail(limit=50)
        self.assertEqual(counter.bytes_read, 0)

    def test_the_budget_window_does_not_walk_the_archive(self):
        from observatory.budget import Budget

        with CountingOpen(*self.files) as counter:
            Budget(self.store).used()
        self.assertLess(counter.bytes_read, self.size // 4)

    def test_the_backoff_state_does_not_walk_the_archive(self):
        from observatory.fetcher import Fetcher
        from observatory.transport import FixtureTransport

        fetcher = Fetcher(FixtureTransport(FIXTURE), self.store)
        with CountingOpen(*self.files) as counter:
            fetcher.state()
        self.assertLess(counter.bytes_read, self.size // 4)

    def test_iter_records_is_the_full_walk_and_is_not_used_on_the_hot_path(self):
        # The whole-archive reader exists for rebuilds, which is fine. This
        # test documents the difference in cost.
        with CountingOpen(*self.files) as counter:
            count = sum(1 for _ in self.store.iter_records())
        self.assertEqual(count, 1000)
        self.assertGreaterEqual(counter.bytes_read, self.size // 2)


if __name__ == "__main__":
    unittest.main()
