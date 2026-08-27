import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from observatory import budget


class PidLivenessTests(unittest.TestCase):
    def test_this_process_is_running(self):
        self.assertTrue(budget.pid_is_running(os.getpid()))

    def test_a_dead_process_is_not_running(self):
        # Start a child, wait for it to exit, then ask about its pid.
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(budget.pid_is_running(child.pid))

    def test_a_live_child_is_running(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
        )
        try:
            self.assertTrue(budget.pid_is_running(child.pid))
        finally:
            child.stdin.close()
            child.wait()

    def test_nonsense_pids(self):
        self.assertFalse(budget.pid_is_running(None))
        self.assertFalse(budget.pid_is_running(0))
        self.assertFalse(budget.pid_is_running(-1))


class StaleLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "sub", ".sampler.lock")

    def write_lock(self, pid, started="2026-08-27T12:00:00Z", host=None, age=0.0):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {"pid": pid, "started": started}
        if host is not None:
            payload["host"] = host
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        if age:
            when = time.time() - age
            os.utime(self.path, (when, when))

    def test_the_lock_records_a_pid(self):
        with budget.WorkerLock(self.path) as lock:
            info = lock.read_info()
        self.assertEqual(info.pid, os.getpid())
        self.assertIsNotNone(info.started)
        self.assertIsNotNone(info.host)

    def test_a_killed_process_does_not_stop_collection(self):
        # The exact scenario: a worker was killed, its lock file survives, and
        # the pid in it belongs to nothing.
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.write_lock(child.pid)

        lock = budget.WorkerLock(self.path)
        lock.acquire()
        try:
            self.assertIsNotNone(lock.broke_stale_lock)
            self.assertIn(str(child.pid), lock.broke_stale_lock)
            self.assertIn("not running", lock.broke_stale_lock)
            self.assertEqual(lock.read_info().pid, os.getpid())
        finally:
            lock.release()

    def test_a_live_holder_is_never_evicted(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
        )
        try:
            self.write_lock(child.pid)
            with self.assertRaises(budget.LockHeld) as raised:
                budget.WorkerLock(self.path).acquire()
            self.assertIn(str(child.pid), str(raised.exception))
        finally:
            child.stdin.close()
            child.wait()

    def test_this_process_holding_the_lock_twice_is_a_conflict(self):
        # Our own pid is alive, so a second worker in this process must wait
        # rather than treat the lock as a leftover.
        with budget.WorkerLock(self.path):
            with self.assertRaises(budget.LockHeld):
                budget.WorkerLock(self.path).acquire()

    def test_the_timeout_breaks_a_lock_whose_pid_cannot_be_trusted(self):
        # A pid that is alive but belongs to something else entirely, which is
        # what pid reuse looks like. Age is the backstop.
        self.write_lock(os.getpid(), age=1200.0)
        lock = budget.WorkerLock(self.path, stale_after=900.0)
        lock.acquire()
        try:
            self.assertIn("untouched", lock.broke_stale_lock)
        finally:
            lock.release()

    def test_a_lock_from_another_host_falls_back_to_the_timeout(self):
        # A pid written on a different machine says nothing about this one, so
        # it must not be probed, and only age can break the lock.
        self.write_lock(999_999, host="some-other-runner")
        with self.assertRaises(budget.LockHeld):
            budget.WorkerLock(self.path, stale_after=900.0).acquire()

        self.write_lock(999_999, host="some-other-runner", age=1200.0)
        lock = budget.WorkerLock(self.path, stale_after=900.0)
        lock.acquire()
        try:
            self.assertIn("untouched", lock.broke_stale_lock)
        finally:
            lock.release()

    def test_a_heartbeat_protects_a_long_run_from_the_timeout(self):
        lock = budget.WorkerLock(self.path, stale_after=900.0)
        lock.acquire()
        try:
            old = time.time() - 1200.0
            os.utime(self.path, (old, old))
            self.assertGreater(lock.read_info().age_seconds, 900.0)

            lock.heartbeat()
            self.assertLess(lock.read_info().age_seconds, 5.0)

            # With the heartbeat fresh, a second worker still has to wait.
            with self.assertRaises(budget.LockHeld):
                budget.WorkerLock(self.path, stale_after=900.0).acquire()
        finally:
            lock.release()

    def test_an_empty_lock_file_is_stale(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("")
        lock = budget.WorkerLock(self.path)
        lock.acquire()
        try:
            self.assertIn("empty", lock.broke_stale_lock)
        finally:
            lock.release()

    def test_an_unparsable_lock_file_falls_back_to_the_timeout(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("pid 4242 written by an older version\n")

        with self.assertRaises(budget.LockHeld):
            budget.WorkerLock(self.path, stale_after=900.0).acquire()

        old = time.time() - 1200.0
        os.utime(self.path, (old, old))
        lock = budget.WorkerLock(self.path, stale_after=900.0)
        lock.acquire()
        try:
            self.assertIn("unknown pid", lock.broke_stale_lock)
        finally:
            lock.release()

    def test_a_clean_acquire_reports_no_takeover(self):
        lock = budget.WorkerLock(self.path)
        lock.acquire()
        try:
            self.assertIsNone(lock.broke_stale_lock)
        finally:
            lock.release()

    def test_release_is_idempotent_and_leaves_no_file(self):
        lock = budget.WorkerLock(self.path)
        lock.acquire()
        lock.release()
        lock.release()
        self.assertFalse(os.path.exists(self.path))

    def test_the_lock_is_released_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with budget.WorkerLock(self.path):
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(self.path))

    def test_reading_a_lock_that_is_not_there(self):
        self.assertIsNone(budget.WorkerLock(self.path).read_info())


class HeartbeatWiringTests(unittest.TestCase):
    def test_the_loop_heartbeats_every_cycle(self):
        from datetime import datetime, timedelta, timezone

        from observatory.archive import Archive
        from observatory.fetcher import Fetcher
        from observatory.transport import Response

        class Transport:
            source = "test"

            def get(self, _url):
                return Response(status=200, headers={}, raw_body=b"# rooms", elapsed_ms=1)

        directory = tempfile.mkdtemp()
        now = [datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)]
        beats = []

        fetcher = Fetcher(
            Transport(),
            Archive(directory),
            clock=lambda: now[0],
        )

        def sleep(seconds):
            now[0] = now[0] + timedelta(seconds=seconds)

        fetcher.run(
            interval=300,
            max_cycles=3,
            sleep=sleep,
            heartbeat=lambda: beats.append(1),
        )
        self.assertEqual(len(beats), 3)


if __name__ == "__main__":
    unittest.main()
