import contextlib
import io
import os
import tempfile
import unittest

from observatory import budget, cli
from observatory.archive import Archive

FIXTURE = "fixtures/rooms-sample.txt"


class ArgumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.archive_path = os.path.join(self.directory, "archive")
        self.store = Archive(self.archive_path)
        self.lock_path = os.path.join(self.directory, ".sampler.lock")

    def base_args(self, *extra):
        return [
            "--archive", self.archive_path,
            "--lock", self.lock_path,
            "--fixture", FIXTURE,
            *extra,
        ]

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run(self.base_args(*extra))
        return code, out.getvalue(), err.getvalue()

    def test_live_requests_need_an_explicit_flag(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as raised:
                cli.run(self.base_args("--source", "http"))
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--allow-network", err.getvalue())

    def test_the_hourly_ceiling_cannot_be_raised_from_the_command_line(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                cli.run(self.base_args("--limit-per-hour", "120"))
        self.assertIn("hard ceiling", err.getvalue())

    def test_the_default_source_is_the_fixture(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("fetched status=200", out)
        records = self.store.read_tail()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "fixture:" + FIXTURE)

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(self.store.files(), [])

    def test_status_reports_without_requesting(self):
        code, out, _ = self.run_cli("--status")
        self.assertEqual(code, 0)
        self.assertIn("budget         0/30 used in the last hour", out)
        self.assertEqual(self.store.files(), [])

    def test_a_replayed_failure_reports_a_non_zero_exit(self):
        code, out, _ = self.run_cli("--replay-status", "503")
        self.assertEqual(code, 1)
        self.assertIn("backoff=60s", out)
        self.assertEqual(self.store.read_tail()[0]["http_status"], 503)

    def test_a_replayed_429_honors_the_replayed_header(self):
        code, _, _ = self.run_cli(
            "--replay-status", "429", "--replay-header", "Retry-After: 900"
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.store.read_tail()[0]["backoff_seconds"], 900.0)

    def test_a_malformed_replay_header_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                cli.run(self.base_args("--replay-header", "no-colon"))
        self.assertIn("NAME:VALUE", err.getvalue())

    def test_a_second_worker_refuses_to_start(self):
        with budget.WorkerLock(self.lock_path):
            code, _, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("another worker holds", err)
        self.assertEqual(self.store.files(), [])

    def test_the_lock_is_released_after_a_run(self):
        self.run_cli()
        self.assertFalse(os.path.exists(self.lock_path))

    def test_loop_mode_stops_after_the_requested_cycles(self):
        code, out, _ = self.run_cli("--loop", "--cycles", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.store.read_tail()), 1)


class ReplayHeaderParsingTests(unittest.TestCase):
    def test_names_are_lowercased_and_values_trimmed(self):
        self.assertEqual(
            cli.parse_replay_headers(["Retry-After: 42"]), {"retry-after": "42"}
        )

    def test_a_value_containing_a_colon_survives(self):
        parsed = cli.parse_replay_headers(["date: Thu, 27 Aug 2026 12:00:00 GMT"])
        self.assertEqual(parsed["date"], "Thu, 27 Aug 2026 12:00:00 GMT")

    def test_a_missing_colon_raises(self):
        with self.assertRaises(ValueError):
            cli.parse_replay_headers(["broken"])


if __name__ == "__main__":
    unittest.main()
