import unittest
from datetime import datetime, timedelta, timezone

from observatory import backoff


class ExponentialLadderTests(unittest.TestCase):
    def test_starts_at_sixty_seconds(self):
        self.assertEqual(backoff.exponential_delay(1), 60.0)

    def test_doubles(self):
        self.assertEqual(backoff.exponential_delay(2), 120.0)
        self.assertEqual(backoff.exponential_delay(3), 240.0)
        self.assertEqual(backoff.exponential_delay(4), 480.0)

    def test_caps_at_thirty_minutes(self):
        for failures in range(6, 40):
            self.assertEqual(backoff.exponential_delay(failures), 1800.0)


class RetryAfterTests(unittest.TestCase):
    def test_delta_seconds(self):
        self.assertEqual(backoff.parse_retry_after("120"), 120.0)

    def test_http_date(self):
        now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
        later = now + timedelta(seconds=90)
        header = later.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertAlmostEqual(backoff.parse_retry_after(header, now=now), 90.0, delta=1.0)

    def test_past_date_is_zero_not_negative(self):
        now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
        header = (now - timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertEqual(backoff.parse_retry_after(header, now=now), 0.0)

    def test_garbage_and_empty(self):
        self.assertIsNone(backoff.parse_retry_after(None))
        self.assertIsNone(backoff.parse_retry_after(""))
        self.assertIsNone(backoff.parse_retry_after("soon"))


class BodyHintTests(unittest.TestCase):
    def test_plain_english(self):
        self.assertEqual(backoff.parse_body_hint("rate limited, retry in 42 seconds"), 42.0)

    def test_json_shape(self):
        self.assertEqual(backoff.parse_body_hint('{"bucket":"rooms","retry_after": 75}'), 75.0)

    def test_reset_in(self):
        self.assertEqual(backoff.parse_body_hint("bucket full; reset_in=610"), 610.0)

    def test_largest_hint_wins(self):
        body = "bucket rooms resets in 30s; global bucket resets in 300s"
        self.assertEqual(backoff.parse_body_hint(body), 300.0)

    def test_absurd_values_ignored(self):
        self.assertIsNone(backoff.parse_body_hint("retry after 999999999 seconds"))

    def test_no_hint(self):
        self.assertIsNone(backoff.parse_body_hint("service unavailable"))
        self.assertIsNone(backoff.parse_body_hint(None))


class NextDelayTests(unittest.TestCase):
    def test_503_uses_ladder(self):
        self.assertEqual(backoff.next_delay(http_status=503, consecutive_failures=1), 60.0)
        self.assertEqual(backoff.next_delay(http_status=503, consecutive_failures=3), 240.0)

    def test_503_capped_at_thirty_minutes(self):
        self.assertEqual(backoff.next_delay(http_status=503, consecutive_failures=12), 1800.0)

    def test_429_honors_retry_after_beyond_the_ladder(self):
        delay = backoff.next_delay(
            http_status=429,
            headers={"Retry-After": "900"},
            consecutive_failures=1,
        )
        self.assertEqual(delay, 900.0)

    def test_429_honors_body_bucket_details(self):
        delay = backoff.next_delay(
            http_status=429,
            headers={},
            body="rate limit: 30 per hour, retry in 700 seconds",
            consecutive_failures=1,
        )
        self.assertEqual(delay, 700.0)

    def test_429_never_shorter_than_the_ladder(self):
        delay = backoff.next_delay(
            http_status=429,
            headers={"retry-after": "5"},
            consecutive_failures=1,
        )
        self.assertEqual(delay, 60.0)

    def test_429_may_exceed_the_exponential_cap_when_asked(self):
        delay = backoff.next_delay(
            http_status=429,
            headers={"retry-after": "3600"},
            consecutive_failures=1,
        )
        self.assertEqual(delay, 3600.0)

    def test_never_tighter_than_the_previous_interval(self):
        delay = backoff.next_delay(
            http_status=429,
            headers={"retry-after": "10"},
            consecutive_failures=1,
            previous_delay=1200.0,
        )
        self.assertEqual(delay, 1200.0)

    def test_ladder_is_monotonic_across_an_episode(self):
        previous = 0.0
        delays = []
        for failures in range(1, 15):
            previous = backoff.next_delay(
                http_status=503,
                consecutive_failures=failures,
                previous_delay=previous,
            )
            delays.append(previous)
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(max(delays), 1800.0)

    def test_network_error_without_status_still_backs_off(self):
        self.assertEqual(backoff.next_delay(http_status=None, consecutive_failures=1), 60.0)

    def test_is_failure(self):
        self.assertFalse(backoff.is_failure(200))
        for status in (None, 429, 500, 503, 404):
            self.assertTrue(backoff.is_failure(status))


if __name__ == "__main__":
    unittest.main()
