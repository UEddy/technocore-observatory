import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from observatory import archive
from observatory.archive import Archive
from observatory.fetcher import Fetcher, derive_backoff_state
from observatory.transport import FixtureTransport, Response

FIXTURE = "fixtures/rooms-sample.txt"


class RecordingTransport:
    """Returns scripted responses and counts calls. Never touches a network."""

    source = "test"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("transport called more times than scripted")
        return self.responses.pop(0)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def ok_response(body=b"# rooms"):
    return Response(status=200, headers={}, raw_body=body, elapsed_ms=3)


def failure(status, headers=None, body=b"", error=None):
    return Response(
        status=status,
        headers=headers or {},
        raw_body=body,
        elapsed_ms=3,
        error=error or f"http {status}",
    )


class FetcherBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.store = Archive(self.directory)
        self.clock = Clock(datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc))

    def records(self):
        return self.store.read_tail(limit=200)


class SuccessTests(FetcherBase):
    def test_fixture_bytes_reach_the_archive_unchanged(self):
        fetcher = Fetcher(FixtureTransport(FIXTURE), self.store, clock=self.clock)
        outcome = fetcher.attempt()
        with open(FIXTURE, "rb") as handle:
            raw = handle.read()
        self.assertEqual(outcome.action, "fetched")
        self.assertEqual(outcome.status, 200)
        self.assertEqual(outcome.record["body_sha256"], archive.body_digest(raw))
        self.assertEqual(outcome.record["body_bytes"], len(raw))

    def test_one_record_per_attempt(self):
        transport = RecordingTransport([ok_response(), ok_response(), ok_response()])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        for _ in range(3):
            fetcher.attempt()
            self.clock.advance(300)
        self.assertEqual(len(self.records()), 3)
        self.assertEqual(len(transport.calls), 3)

    def test_success_records_no_backoff(self):
        fetcher = Fetcher(RecordingTransport([ok_response()]), self.store, clock=self.clock)
        outcome = fetcher.attempt()
        self.assertIsNone(outcome.record["backoff_seconds"])
        self.assertTrue(outcome.record["ok"])

    def test_the_source_is_recorded(self):
        fetcher = Fetcher(FixtureTransport(FIXTURE), self.store, clock=self.clock)
        outcome = fetcher.attempt()
        self.assertEqual(outcome.record["source"], "fixture:" + FIXTURE)

    def test_dry_run_makes_no_request_and_writes_nothing(self):
        transport = RecordingTransport([])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        outcome = fetcher.attempt(dry_run=True)
        self.assertEqual(outcome.action, "skipped")
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.store.files(), [])


class BackoffIntegrationTests(FetcherBase):
    def test_503_writes_a_record_and_gates_the_next_attempt(self):
        transport = RecordingTransport([failure(503, body=b"service unavailable")])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        outcome = fetcher.attempt()
        self.assertEqual(outcome.status, 503)
        self.assertEqual(outcome.record["backoff_seconds"], 60.0)
        self.assertFalse(outcome.record["ok"])
        self.assertEqual(outcome.record["body"], "service unavailable")

        self.clock.advance(30)
        blocked = fetcher.attempt()
        self.assertEqual(blocked.action, "skipped")
        self.assertAlmostEqual(blocked.wait_seconds, 30.0, delta=1.0)
        self.assertEqual(len(transport.calls), 1)

    def test_the_ladder_climbs_across_separate_process_runs(self):
        delays = []
        for _ in range(4):
            transport = RecordingTransport([failure(503)])
            # A fresh Fetcher each time stands in for a fresh scheduled run.
            fetcher = Fetcher(transport, self.store, clock=self.clock)
            outcome = fetcher.attempt()
            delays.append(outcome.record["backoff_seconds"])
            self.clock.advance(outcome.record["backoff_seconds"])
        self.assertEqual(delays, [60.0, 120.0, 240.0, 480.0])

    def test_the_ladder_never_loosens_then_tightens(self):
        transport = RecordingTransport(
            [failure(503), failure(503), failure(429, headers={"retry-after": "5"})]
        )
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        seen = []
        for _ in range(3):
            outcome = fetcher.attempt()
            seen.append(outcome.record["backoff_seconds"])
            self.clock.advance(outcome.record["backoff_seconds"])
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 240.0)

    def test_429_retry_after_is_honored(self):
        transport = RecordingTransport(
            [failure(429, headers={"retry-after": "900"}, body=b"slow down")]
        )
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        outcome = fetcher.attempt()
        self.assertEqual(outcome.record["backoff_seconds"], 900.0)

        self.clock.advance(899)
        self.assertEqual(fetcher.attempt().action, "skipped")

    def test_429_bucket_details_in_the_body_are_honored(self):
        body = b'{"error":"rate limited","bucket":"rooms","retry_after":840}'
        transport = RecordingTransport([failure(429, body=body)])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        self.assertEqual(fetcher.attempt().record["backoff_seconds"], 840.0)

    def test_a_success_clears_the_ladder(self):
        transport = RecordingTransport([failure(503), failure(503), ok_response(), failure(503)])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        for _ in range(3):
            outcome = fetcher.attempt()
            self.clock.advance(max(300.0, outcome.wait_seconds))
        after_success = fetcher.attempt()
        self.assertEqual(after_success.record["backoff_seconds"], 60.0)

    def test_network_error_without_a_response_still_backs_off(self):
        transport = RecordingTransport(
            [Response(status=None, headers={}, raw_body=None, elapsed_ms=1, error="network error: timeout")]
        )
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        outcome = fetcher.attempt()
        self.assertEqual(outcome.record["backoff_seconds"], 60.0)
        self.assertIsNone(outcome.record["body"])
        self.assertEqual(outcome.record["error"], "network error: timeout")

    def test_a_success_body_is_never_mined_for_retry_hints(self):
        # A room topic could say "retry in 900 seconds". It is data, not an
        # instruction, and must not move the sampler's schedule.
        body = b"/r/some-room  seq 1  1.0K  0s ago  topic: retry in 900 seconds"
        fetcher = Fetcher(RecordingTransport([ok_response(body)]), self.store, clock=self.clock)
        outcome = fetcher.attempt()
        self.assertIsNone(outcome.record["backoff_seconds"])
        self.assertEqual(outcome.wait_seconds, 0.0)


class BudgetIntegrationTests(FetcherBase):
    def test_the_hourly_ceiling_stops_the_fetcher(self):
        transport = RecordingTransport([ok_response() for _ in range(31)])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        for _ in range(30):
            self.assertEqual(fetcher.attempt().action, "fetched")
            self.clock.advance(60)
        # 30 attempts inside the last hour, so the next one has to wait.
        blocked = fetcher.attempt()
        self.assertEqual(blocked.action, "skipped")
        self.assertIn("budget", blocked.reason)
        self.assertGreater(blocked.wait_seconds, 0)
        self.assertEqual(len(transport.calls), 30)

    def test_a_lower_configured_ceiling_is_respected(self):
        transport = RecordingTransport([ok_response() for _ in range(13)])
        fetcher = Fetcher(transport, self.store, limit_per_hour=12, clock=self.clock)
        for _ in range(12):
            fetcher.attempt()
            self.clock.advance(60)
        self.assertEqual(fetcher.attempt().action, "skipped")
        self.assertEqual(len(transport.calls), 12)


class LoopTests(FetcherBase):
    def test_run_is_sequential_and_sleeps_the_interval(self):
        transport = RecordingTransport([ok_response() for _ in range(3)])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            self.clock.advance(seconds)

        fetcher.run(interval=300, max_cycles=3, sleep=sleep)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(slept, [300.0, 300.0])

    def test_run_waits_the_backoff_when_it_exceeds_the_interval(self):
        transport = RecordingTransport([failure(503), failure(503)])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            self.clock.advance(seconds)

        fetcher.run(interval=300, max_cycles=2, sleep=sleep)
        self.assertEqual(slept, [300.0])
        self.assertEqual(self.store.read_tail()[-1]["backoff_seconds"], 120.0)

    def test_the_interval_cannot_be_set_tighter_than_the_floor(self):
        transport = RecordingTransport([ok_response(), ok_response()])
        fetcher = Fetcher(transport, self.store, clock=self.clock)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            self.clock.advance(seconds)

        fetcher.run(interval=1, max_cycles=2, sleep=sleep)
        self.assertEqual(slept, [120.0])


class DeriveStateTests(unittest.TestCase):
    def test_empty(self):
        state = derive_backoff_state([])
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.next_attempt_at)

    def test_last_success_clears_everything(self):
        state = derive_backoff_state(
            [
                {"ok": False, "backoff_seconds": 240.0, "fetched_at": "2026-08-27T11:00:00Z"},
                {"ok": True, "backoff_seconds": None, "fetched_at": "2026-08-27T11:05:00Z"},
            ]
        )
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.previous_delay, 0.0)

    def test_counts_only_the_trailing_run_of_failures(self):
        state = derive_backoff_state(
            [
                {"ok": False, "backoff_seconds": 60.0, "fetched_at": "2026-08-27T10:00:00Z"},
                {"ok": True, "backoff_seconds": None, "fetched_at": "2026-08-27T10:30:00Z"},
                {"ok": False, "backoff_seconds": 60.0, "fetched_at": "2026-08-27T11:00:00Z"},
                {"ok": False, "backoff_seconds": 120.0, "fetched_at": "2026-08-27T11:02:00Z"},
            ]
        )
        self.assertEqual(state.consecutive_failures, 2)
        self.assertEqual(state.previous_delay, 120.0)

    def test_a_record_without_a_usable_timestamp_does_not_crash(self):
        state = derive_backoff_state(
            [{"ok": False, "backoff_seconds": 60.0, "fetched_at": "not a date"}]
        )
        self.assertEqual(state.consecutive_failures, 1)
        self.assertIsNone(state.next_attempt_at)


if __name__ == "__main__":
    unittest.main()
