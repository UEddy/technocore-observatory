import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from observatory.archive import Archive
from observatory.fetcher import Fetcher
from observatory.guard import Guard, GuardState
from observatory.transport import Response

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


class ScriptedTransport:
    source = "test"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, _url):
        self.calls += 1
        if not self.responses:
            raise AssertionError("transport called more times than scripted")
        return self.responses.pop(0)


def failure(status=503):
    return Response(
        status=status, headers={}, raw_body=b"unavailable", elapsed_ms=1,
        error=f"http {status}",
    )


def success():
    return Response(status=200, headers={}, raw_body=b"# rooms", elapsed_ms=1)


class GuardFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "sub", "guard.json")
        self.guard = Guard(self.path)

    def test_no_path_means_no_guard(self):
        empty = Guard(None)
        self.assertFalse(empty)
        self.assertIsNone(empty.not_before(NOW))
        self.assertEqual(empty.describe(NOW), "not in use")
        empty.record(now=NOW, ok=False, delay_seconds=60, consecutive_failures=1)

    def test_a_missing_file_is_no_floor(self):
        self.assertIsNone(self.guard.not_before(NOW))
        self.assertEqual(self.guard.read(), GuardState())

    def test_a_failure_writes_a_floor(self):
        self.guard.record(
            now=NOW, ok=False, delay_seconds=240, consecutive_failures=3, http_status=503
        )
        state = self.guard.read()
        self.assertEqual(state.next_attempt_at, NOW + timedelta(seconds=240))
        self.assertEqual(state.consecutive_failures, 3)
        self.assertEqual(state.last_status, 503)
        self.assertEqual(self.guard.not_before(NOW), NOW + timedelta(seconds=240))

    def test_a_success_clears_the_floor(self):
        self.guard.record(now=NOW, ok=False, delay_seconds=240, consecutive_failures=3)
        self.guard.record(now=NOW, ok=True, delay_seconds=None, consecutive_failures=0, http_status=200)
        self.assertIsNone(self.guard.not_before(NOW))
        self.assertEqual(self.guard.read().consecutive_failures, 0)

    def test_an_expired_floor_is_no_floor(self):
        self.guard.record(now=NOW, ok=False, delay_seconds=60, consecutive_failures=1)
        self.assertIsNone(self.guard.not_before(NOW + timedelta(seconds=61)))

    def test_the_file_is_readable_json(self):
        self.guard.record(now=NOW, ok=False, delay_seconds=60, consecutive_failures=1)
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["consecutive_failures"], 1)
        self.assertEqual(payload["next_attempt_at"], "2026-08-27T12:01:00Z")

    def test_a_corrupt_file_fails_open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("not json at all")
        self.assertIsNone(self.guard.not_before(NOW))

    def test_a_file_with_unexpected_contents_fails_open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for content in ("[]", '"text"', '{"next_attempt_at": 12}', '{"next_attempt_at": "soon"}'):
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(content)
            self.assertIsNone(self.guard.not_before(NOW), content)

    def test_writing_leaves_no_temporary_file(self):
        self.guard.record(now=NOW, ok=False, delay_seconds=60, consecutive_failures=1)
        self.assertFalse(os.path.exists(self.path + ".writing"))

    def test_describe_reports_the_hold(self):
        self.assertIn("not written yet", self.guard.describe(NOW))
        self.guard.record(now=NOW, ok=False, delay_seconds=120, consecutive_failures=2)
        self.assertIn("holding for 120s", self.guard.describe(NOW))
        self.assertIn("2 consecutive failures", self.guard.describe(NOW))
        self.guard.record(now=NOW, ok=True, delay_seconds=None, consecutive_failures=0)
        self.assertIn("no floor", self.guard.describe(NOW))


class LostPushTests(unittest.TestCase):
    """The case the guard exists for: the archive write does not survive."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.archive_path = os.path.join(self.directory, "archive")
        self.guard = Guard(os.path.join(self.directory, "guard.json"))
        self.now = NOW

    def clock(self):
        return self.now

    def fetcher(self, *responses):
        return Fetcher(
            ScriptedTransport(*responses),
            Archive(self.archive_path),
            clock=self.clock,
            guard=self.guard,
        )

    def lose_the_archive(self):
        """What a failed push amounts to: the runner is thrown away."""
        shutil.rmtree(self.archive_path, ignore_errors=True)

    def test_the_ladder_survives_a_lost_archive(self):
        first = self.fetcher(failure(503))
        self.assertEqual(first.attempt().record["backoff_seconds"], 60.0)

        self.lose_the_archive()
        self.now += timedelta(seconds=30)

        second = self.fetcher(success())
        outcome = second.attempt()
        self.assertEqual(outcome.action, "skipped")
        self.assertIn("guard is holding", outcome.reason)
        self.assertIn("not in this archive", outcome.reason)
        self.assertAlmostEqual(outcome.wait_seconds, 30.0, delta=1)
        self.assertEqual(second.transport.calls, 0)

    def test_the_floor_lifts_when_the_wait_is_over(self):
        self.fetcher(failure(503)).attempt()
        self.lose_the_archive()
        self.now += timedelta(seconds=61)
        self.assertEqual(self.fetcher(success()).attempt().action, "fetched")

    def test_a_repeatedly_lost_archive_does_not_climb_the_ladder(self):
        # Honest limit: with every record lost, each run looks like the first
        # failure, so the floor stays at the base delay instead of doubling.
        # It still holds the line at the base delay, which is the point.
        delays = []
        for _ in range(3):
            outcome = self.fetcher(failure(503)).attempt()
            delays.append(outcome.record["backoff_seconds"])
            self.lose_the_archive()
            self.now += timedelta(seconds=61)
        self.assertEqual(delays, [60.0, 60.0, 60.0])

    def test_the_ladder_still_climbs_when_the_archive_survives(self):
        delays = []
        for _ in range(3):
            outcome = self.fetcher(failure(503)).attempt()
            delays.append(outcome.record["backoff_seconds"])
            self.now += timedelta(seconds=outcome.record["backoff_seconds"] + 1)
        self.assertEqual(delays, [60.0, 120.0, 240.0])

    def test_a_lost_guard_falls_back_to_the_archive(self):
        self.fetcher(failure(503)).attempt()
        os.unlink(self.guard.path)
        self.now += timedelta(seconds=30)
        outcome = self.fetcher(success()).attempt()
        # The archive still holds the ladder, so the attempt is still gated.
        self.assertEqual(outcome.action, "skipped")
        self.assertIn("backing off", outcome.reason)

    def test_the_guard_never_brings_an_attempt_forward(self):
        # The archive says wait 240s; a stale guard saying 60s must not shorten it.
        self.fetcher(failure(503)).attempt()
        self.now += timedelta(seconds=61)
        self.fetcher(failure(503)).attempt()
        self.now += timedelta(seconds=121)
        third = self.fetcher(failure(503)).attempt()
        self.assertEqual(third.record["backoff_seconds"], 240.0)

        self.guard.record(now=self.now, ok=False, delay_seconds=1, consecutive_failures=1)
        self.now += timedelta(seconds=10)
        outcome = self.fetcher(success()).attempt()
        self.assertEqual(outcome.action, "skipped")
        self.assertIn("backing off", outcome.reason)
        self.assertGreater(outcome.wait_seconds, 200)

    def test_a_success_through_the_guard_clears_it_for_the_next_run(self):
        self.fetcher(failure(503)).attempt()
        self.now += timedelta(seconds=61)
        self.fetcher(success()).attempt()
        self.assertIsNone(self.guard.not_before(self.now))

        self.lose_the_archive()
        self.assertEqual(self.fetcher(success()).attempt().action, "fetched")

    def test_without_a_guard_a_lost_archive_resets_the_ladder(self):
        # The behaviour the guard exists to prevent, pinned so the difference
        # stays visible.
        unguarded = Fetcher(
            ScriptedTransport(failure(503)),
            Archive(self.archive_path),
            clock=self.clock,
        )
        unguarded.attempt()
        self.lose_the_archive()
        self.now += timedelta(seconds=1)

        again = Fetcher(
            ScriptedTransport(success()),
            Archive(self.archive_path),
            clock=self.clock,
        )
        self.assertEqual(again.attempt().action, "fetched")


class GuardCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.archive_path = os.path.join(self.directory, "archive")
        self.guard_path = os.path.join(self.directory, "guard.json")
        self.lock_path = os.path.join(self.directory, "lock")

    def run_cli(self, *extra):
        import contextlib
        import io

        from observatory import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run(
                [
                    "--archive", self.archive_path,
                    "--lock", self.lock_path,
                    "--guard", self.guard_path,
                    "--fixture", "fixtures/rooms-sample.txt",
                    *extra,
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def test_a_failed_fetch_writes_the_guard(self):
        code, _, _ = self.run_cli("--replay-status", "503")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.guard_path))
        self.assertIsNotNone(Guard(self.guard_path).read().next_attempt_at)

    def test_status_reports_the_guard(self):
        self.run_cli("--replay-status", "503")
        _, out, _ = self.run_cli("--status")
        self.assertIn("guard", out)
        self.assertIn("holding for", out)

    def test_the_guard_gates_the_next_run(self):
        self.run_cli("--replay-status", "503")
        shutil.rmtree(self.archive_path, ignore_errors=True)
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("guard is holding", out)

    def test_without_the_flag_no_guard_file_appears(self):
        import contextlib
        import io

        from observatory import cli

        with contextlib.redirect_stdout(io.StringIO()):
            cli.run(
                [
                    "--archive", self.archive_path,
                    "--lock", self.lock_path,
                    "--fixture", "fixtures/rooms-sample.txt",
                    "--replay-status", "503",
                ]
            )
        self.assertFalse(os.path.exists(self.guard_path))


if __name__ == "__main__":
    unittest.main()
