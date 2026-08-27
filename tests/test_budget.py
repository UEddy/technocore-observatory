import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from observatory import archive, budget


def stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "rooms.ndjson")
        self.now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    def _record_at(self, when):
        archive.append(
            self.path,
            archive.make_record(
                url="u", source="fixture", ok=True, http_status=200, headers={},
                raw_body=b"x", elapsed_ms=1, error=None, backoff_seconds=None,
                fetched_at=stamp(when),
            ),
        )

    def test_empty_archive_has_the_full_budget(self):
        window = budget.Budget(self.path)
        self.assertEqual(window.used(self.now), 0)
        self.assertEqual(window.remaining(self.now), 30)
        self.assertEqual(window.next_allowed_at(self.now), self.now)

    def test_counts_only_the_last_hour(self):
        self._record_at(self.now - timedelta(minutes=90))
        self._record_at(self.now - timedelta(minutes=30))
        self._record_at(self.now - timedelta(minutes=5))
        window = budget.Budget(self.path)
        self.assertEqual(window.used(self.now), 2)

    def test_failed_attempts_count_against_the_budget(self):
        archive.append(
            self.path,
            archive.make_record(
                url="u", source="http", ok=False, http_status=503, headers={},
                raw_body=b"unavailable", elapsed_ms=1, error="http 503",
                backoff_seconds=60.0, fetched_at=stamp(self.now - timedelta(minutes=1)),
            ),
        )
        self.assertEqual(budget.Budget(self.path).used(self.now), 1)

    def test_ceiling_blocks_further_requests(self):
        for index in range(30):
            self._record_at(self.now - timedelta(minutes=50 - index))
        window = budget.Budget(self.path)
        self.assertEqual(window.remaining(self.now), 0)
        self.assertGreater(window.next_allowed_at(self.now), self.now)

    def test_next_allowed_is_when_the_oldest_attempt_ages_out(self):
        oldest = self.now - timedelta(minutes=50)
        self._record_at(oldest)
        for index in range(29):
            self._record_at(self.now - timedelta(minutes=40 - index))
        window = budget.Budget(self.path)
        self.assertEqual(window.next_allowed_at(self.now), oldest + timedelta(hours=1))

    def test_configured_limit_can_lower_but_never_raise_the_ceiling(self):
        self.assertEqual(budget.Budget(self.path, limit_per_hour=12).limit, 12)
        self.assertEqual(budget.Budget(self.path, limit_per_hour=500).limit, 30)

    def test_lower_limit_is_enforced(self):
        for index in range(12):
            self._record_at(self.now - timedelta(minutes=30 - index))
        self.assertEqual(budget.Budget(self.path, limit_per_hour=12).remaining(self.now), 0)
        self.assertEqual(budget.Budget(self.path).remaining(self.now), 18)


class WorkerLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "sub", ".sampler.lock")

    def test_second_worker_is_refused(self):
        with budget.WorkerLock(self.path):
            with self.assertRaises(budget.LockHeld):
                budget.WorkerLock(self.path).acquire()

    def test_lock_is_released_on_exit(self):
        with budget.WorkerLock(self.path):
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_lock_is_released_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with budget.WorkerLock(self.path):
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(self.path))

    def test_a_stale_lock_is_taken_over(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("pid 1 from a crashed run\n")
        old = 1_000_000.0
        os.utime(self.path, (old, old))
        with budget.WorkerLock(self.path, stale_after=60.0):
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
