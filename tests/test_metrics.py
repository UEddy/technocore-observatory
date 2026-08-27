import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from observatory import metrics, store
from observatory.archive import Archive, make_record

FIXTURE = "fixtures/rooms-sample.txt"
START = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc)


def template_lines():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read().splitlines()


def body(
    rooms=17740,
    notes=426362,
    stored_mib=148.7,
    msgs=8072,
    zero=16,
    nick=0.23,
    notes_per_msg=52.82,
    room_shift=0,
):
    """One synthetic /rooms response with the given aggregates."""
    lines = template_lines()
    lines[0] = (
        f"# 50 of {rooms} rooms (cap 20480, {stored_mib:.1f}M of 5.0G stored), newest first"
    )
    window = lines[2:52]
    if room_shift:
        # Replace the tail of the window with paths never seen before, which is
        # what a shifting window looks like.
        replacements = [
            f"/r/synthetic-{room_shift}-{index}  seq {index + 1}  1.0K  {index}s ago"
            for index in range(room_shift)
        ]
        window = window[room_shift:] + replacements
    lines[2:52] = window
    lines[52] = (
        f"# notes {notes} of 655360 (35.4M total, 50960 per namespace, namespaces not listed)"
    )
    lines[53] = (
        f"# engagement over {msgs} msgs scanned: zero-response {zero:.0f}%, "
        f"nick diversity {nick:.2f}, notes/msg {notes_per_msg:.2f}"
    )
    return "\n".join(lines) + "\n"


class MetricsTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.store = Archive(os.path.join(self.directory, "archive"))
        self.db_path = os.path.join(self.directory, "observatory.db")

    def add(self, at, text=None, ok=True, http_status=200):
        self.store.append(
            make_record(
                url="https://technocore.chat/rooms",
                source="http",
                ok=ok,
                http_status=http_status,
                headers={},
                raw_body=(text if text is not None else body()).encode("utf-8"),
                elapsed_ms=100,
                error=None if ok else f"http {http_status}",
                backoff_seconds=None if ok else 60.0,
                fetched_at=at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )

    def linear_history(self, samples=96, minutes=15, rooms_step=3, notes_step=100, **kwargs):
        for index in range(samples):
            self.add(
                START + timedelta(minutes=minutes * index),
                body(
                    rooms=17740 + rooms_step * index,
                    notes=426362 + notes_step * index,
                    **kwargs,
                ),
            )

    def report(self):
        store.build(self.db_path, self.store)
        connection = store.connect(self.db_path)
        try:
            return metrics.build_report(connection, now=START + timedelta(days=30))
        finally:
            connection.close()


def series_from(values, start=START, minutes=60):
    return metrics.Series(
        key="test",
        label="test",
        points=[
            metrics.Point(at=start + timedelta(minutes=minutes * index), value=value)
            for index, value in enumerate(values)
        ],
    )


class RateTests(unittest.TestCase):
    def test_a_clean_slope_is_recovered_exactly(self):
        rate = metrics.fit_rate(series_from([0, 10, 20, 30, 40]))
        self.assertAlmostEqual(rate.per_hour, 10.0)
        self.assertAlmostEqual(rate.per_day, 240.0)
        self.assertEqual(rate.samples, 5)
        self.assertAlmostEqual(rate.span_hours, 4.0)

    def test_a_perfect_fit_has_no_scatter(self):
        rate = metrics.fit_rate(series_from([0, 10, 20, 30]))
        self.assertAlmostEqual(rate.standard_error, 0.0)
        self.assertAlmostEqual(rate.low_per_hour, 10.0)
        self.assertAlmostEqual(rate.high_per_hour, 10.0)

    def test_scatter_widens_the_band(self):
        rate = metrics.fit_rate(series_from([0, 30, 10, 40, 20, 60]))
        self.assertIsNotNone(rate.standard_error)
        self.assertGreater(rate.standard_error, 0)
        self.assertLess(rate.low_per_hour, rate.per_hour)
        self.assertGreater(rate.high_per_hour, rate.per_hour)

    def test_two_points_give_a_slope_but_no_error(self):
        rate = metrics.fit_rate(series_from([100, 200]))
        self.assertAlmostEqual(rate.per_hour, 100.0)
        self.assertIsNone(rate.standard_error)
        self.assertIsNone(rate.low_per_hour)

    def test_one_point_gives_no_slope(self):
        rate = metrics.fit_rate(series_from([100]))
        self.assertIsNone(rate.per_hour)
        self.assertEqual(rate.samples, 1)

    def test_no_points_at_all(self):
        rate = metrics.fit_rate(metrics.Series(key="k", label="l"))
        self.assertIsNone(rate.per_hour)
        self.assertEqual(rate.samples, 0)

    def test_samples_all_at_one_instant_say_nothing_about_a_rate(self):
        series = metrics.Series(
            key="k",
            label="l",
            points=[metrics.Point(at=START, value=value) for value in (1, 2, 3)],
        )
        self.assertIsNone(metrics.fit_rate(series).per_hour)

    def test_a_falling_series_has_a_negative_rate(self):
        self.assertLess(metrics.fit_rate(series_from([100, 90, 80])).per_hour, 0)


class ProjectionMathTests(unittest.TestCase):
    def test_a_straightforward_projection(self):
        at, hours = metrics.project_to_cap(100, 200, 10, START)
        self.assertEqual(hours, 10.0)
        self.assertEqual(at, START + timedelta(hours=10))

    def test_a_flat_rate_never_reaches_the_cap(self):
        self.assertEqual(metrics.project_to_cap(100, 200, 0, START), (None, None))

    def test_a_falling_rate_never_reaches_the_cap(self):
        self.assertEqual(metrics.project_to_cap(100, 200, -5, START), (None, None))

    def test_already_at_the_cap(self):
        at, hours = metrics.project_to_cap(200, 200, 10, START)
        self.assertEqual(at, START)
        self.assertEqual(hours, 0.0)

    def test_missing_inputs_give_no_projection(self):
        self.assertEqual(metrics.project_to_cap(None, 200, 10, START), (None, None))
        self.assertEqual(metrics.project_to_cap(100, None, 10, START), (None, None))
        self.assertEqual(metrics.project_to_cap(100, 200, None, START), (None, None))
        self.assertEqual(metrics.project_to_cap(100, 200, 10, None), (None, None))

    def test_an_absurd_horizon_gives_a_number_but_no_date(self):
        at, hours = metrics.project_to_cap(0, 10**12, 0.001, START)
        self.assertIsNone(at)
        self.assertGreater(hours, 0)


class ExhaustionTests(MetricsTestCase):
    def test_the_rate_matches_the_data_that_produced_it(self):
        # Three rooms every fifteen minutes is twelve an hour.
        self.linear_history(samples=96, minutes=15, rooms_step=3)
        report = self.report()
        rooms = next(p for p in report.projections if p.key == "rooms")
        self.assertAlmostEqual(rooms.rate.per_hour, 12.0, places=6)
        self.assertAlmostEqual(rooms.rate.per_day, 288.0, places=4)

    def test_the_projection_follows_from_the_rate(self):
        self.linear_history(samples=96, minutes=15, rooms_step=3)
        report = self.report()
        rooms = next(p for p in report.projections if p.key == "rooms")
        expected_hours = (rooms.cap - rooms.current) / rooms.rate.per_hour
        self.assertAlmostEqual(rooms.hours_left, expected_hours, places=4)
        self.assertIsNotNone(rooms.exhausts_at)

    def test_the_current_figure_and_share_come_from_the_newest_snapshot(self):
        self.linear_history(samples=10, rooms_step=3)
        report = self.report()
        rooms = next(p for p in report.projections if p.key == "rooms")
        self.assertEqual(rooms.current, 17740 + 3 * 9)
        self.assertEqual(rooms.cap, 20480)
        self.assertAlmostEqual(rooms.share, (17740 + 27) / 20480)
        self.assertEqual(rooms.headroom, 20480 - (17740 + 27))

    def test_every_projection_carries_a_caveat(self):
        self.linear_history(samples=96)
        report = self.report()
        for projection in report.projections:
            self.assertTrue(projection.caveats, projection.key)

    def test_the_first_caveat_says_it_is_not_a_forecast(self):
        self.linear_history(samples=96)
        report = self.report()
        rooms = next(p for p in report.projections if p.key == "rooms")
        self.assertIn("not a forecast", rooms.caveats[0])

    def test_a_flat_resource_gets_no_date_and_says_why(self):
        for index in range(20):
            self.add(START + timedelta(minutes=15 * index), body(rooms=17740))
        report = self.report()
        rooms = next(p for p in report.projections if p.key == "rooms")
        self.assertFalse(rooms.has_projection)
        self.assertIn("flat or negative", " ".join(rooms.caveats))

    def test_a_thin_record_says_so_before_anything_else(self):
        self.add(START, body(rooms=17740))
        self.add(START + timedelta(minutes=15), body(rooms=17800))
        report = self.report()
        rooms = next(p for p in report.projections if p.key == "rooms")
        self.assertIn("not yet enough data", rooms.caveats[0])

    def test_a_short_record_flags_the_trailing_rate_as_the_same_fit(self):
        self.linear_history(samples=96, minutes=15)  # 24 hours
        report = self.report()
        rooms = next(p for p in report.projections if p.key == "rooms")
        self.assertTrue(rooms.trailing_is_same_data)
        self.assertEqual(rooms.trailing_rate.samples, rooms.rate.samples)

    def test_a_long_record_gives_a_genuinely_trailing_rate(self):
        # Twelve days of hourly samples, so the trailing window is a subset.
        for index in range(12 * 24):
            self.add(
                START + timedelta(hours=index),
                body(rooms=17740 + index, notes=426362 + 10 * index),
            )
        report = self.report()
        rooms = next(p for p in report.projections if p.key == "rooms")
        self.assertFalse(rooms.trailing_is_same_data)
        self.assertLess(rooms.trailing_rate.samples, rooms.rate.samples)
        self.assertGreater(rooms.trailing_rate.samples, 0)

    def test_a_rate_that_changes_is_called_out(self):
        # Slow for eight days, then five times faster for four.
        rooms = 17740
        at = START
        for _ in range(8 * 24):
            self.add(at, body(rooms=rooms))
            rooms += 1
            at += timedelta(hours=1)
        for _ in range(4 * 24):
            self.add(at, body(rooms=rooms))
            rooms += 5
            at += timedelta(hours=1)
        report = self.report()
        projection = next(p for p in report.projections if p.key == "rooms")
        self.assertIn("not steady", " ".join(projection.caveats))

    def test_the_byte_figures_are_read_as_bytes(self):
        self.linear_history(samples=8, stored_mib=200.0)
        report = self.report()
        stored = next(p for p in report.projections if p.key == "bytes")
        self.assertAlmostEqual(stored.current, 200.0 * 1024 * 1024, delta=1)
        self.assertEqual(stored.cap, 5 * 1024**3)

    def test_all_three_capped_resources_are_reported(self):
        self.linear_history(samples=8)
        report = self.report()
        self.assertEqual([p.key for p in report.projections], ["rooms", "notes", "bytes"])

    def test_an_empty_database_projects_nothing_and_does_not_crash(self):
        report = self.report()
        self.assertFalse(report.has_data)
        for projection in report.projections:
            self.assertFalse(projection.has_projection)
            self.assertIsNone(projection.current)
            self.assertTrue(projection.caveats)


class EngagementTests(MetricsTestCase):
    def test_all_four_published_figures_are_tracked(self):
        self.linear_history(samples=5)
        report = self.report()
        self.assertEqual(
            [series.key for series in report.engagement],
            ["zero_response_rate", "nick_diversity", "notes_per_msg", "msgs_scanned"],
        )

    def test_the_values_are_the_ones_the_server_published(self):
        self.add(START, body(zero=16, nick=0.23, notes_per_msg=52.82, msgs=8072))
        self.add(
            START + timedelta(minutes=15),
            body(zero=18, nick=0.25, notes_per_msg=61.5, msgs=9000),
        )
        report = self.report()
        by_key = {series.key: series for series in report.engagement}
        self.assertEqual([p.value for p in by_key["zero_response_rate"].points], [0.16, 0.18])
        self.assertEqual([p.value for p in by_key["nick_diversity"].points], [0.23, 0.25])
        self.assertEqual([p.value for p in by_key["notes_per_msg"].points], [52.82, 61.5])
        self.assertEqual([p.value for p in by_key["msgs_scanned"].points], [8072.0, 9000.0])

    def test_the_series_is_ordered_oldest_first(self):
        self.add(START + timedelta(hours=2), body(msgs=3))
        self.add(START, body(msgs=1))
        self.add(START + timedelta(hours=1), body(msgs=2))
        report = self.report()
        msgs = next(s for s in report.engagement if s.key == "msgs_scanned")
        self.assertEqual([p.value for p in msgs.points], [1.0, 2.0, 3.0])
        self.assertEqual(msgs.span_hours, 2.0)

    def test_a_failed_request_contributes_no_point(self):
        self.add(START, body(msgs=8072))
        self.add(START + timedelta(minutes=15), "service unavailable", ok=False, http_status=503)
        report = self.report()
        msgs = next(s for s in report.engagement if s.key == "msgs_scanned")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(report.failed_requests, 1)

    def test_a_snapshot_missing_the_footer_contributes_no_point(self):
        broken = body().replace("# engagement over", "# engagement across")
        self.add(START, body(msgs=8072))
        self.add(START + timedelta(minutes=15), broken)
        report = self.report()
        msgs = next(s for s in report.engagement if s.key == "msgs_scanned")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(report.flagged_snapshots, 1)
        # The header on that snapshot still parsed, so rooms keeps both points.
        rooms = next(p for p in report.projections if p.key == "rooms")
        self.assertEqual(len(rooms.series), 2)


class CoverageTests(MetricsTestCase):
    def test_coverage_counts_distinct_paths_and_observations(self):
        self.add(START, body())
        self.add(START + timedelta(minutes=15), body())
        report = self.report()
        self.assertEqual(report.coverage.rooms_seen, 50)
        self.assertEqual(report.coverage.observations, 100)

    def test_a_shifting_window_accumulates_coverage(self):
        self.add(START, body())
        self.add(START + timedelta(minutes=15), body(room_shift=10))
        report = self.report()
        self.assertEqual(report.coverage.rooms_seen, 60)

    def test_the_share_of_the_reported_total_is_a_floor(self):
        self.add(START, body(rooms=20000))
        report = self.report()
        self.assertEqual(report.coverage.rooms_reported_by_server, 20000)
        self.assertAlmostEqual(report.coverage.share_of_reported, 50 / 20000)

    def test_window_turnover_counts_rooms_that_left(self):
        self.add(START, body())
        self.add(START + timedelta(minutes=15), body(room_shift=10))
        report = self.report()
        self.assertEqual(report.coverage.window_size, 50)
        self.assertEqual(report.coverage.median_replaced_per_sample, 10.0)
        self.assertEqual(report.coverage.pairs_compared, 1)
        self.assertAlmostEqual(report.coverage.replaced_share, 0.2)

    def test_the_sample_gap_is_reported_alongside_turnover(self):
        self.add(START, body())
        self.add(START + timedelta(minutes=15), body(room_shift=5))
        self.add(START + timedelta(minutes=30), body(room_shift=5))
        report = self.report()
        self.assertEqual(report.coverage.sample_gap_minutes, 15.0)
        self.assertEqual(report.coverage.pairs_compared, 2)

    def test_a_single_snapshot_supports_no_turnover_figure(self):
        self.add(START, body())
        report = self.report()
        self.assertEqual(report.coverage.pairs_compared, 0)
        self.assertIsNone(report.coverage.median_replaced_per_sample)
        self.assertIsNone(report.coverage.replaced_share)

    def test_an_empty_database_has_empty_coverage(self):
        report = self.report()
        self.assertEqual(report.coverage.rooms_seen, 0)
        self.assertIsNone(report.coverage.share_of_reported)


class ReportTests(MetricsTestCase):
    def test_counts_and_stamps(self):
        self.add(START, body())
        self.add(START + timedelta(minutes=15), body())
        self.add(START + timedelta(minutes=30), "gone", ok=False, http_status=503)
        report = self.report()

        self.assertEqual(report.snapshot_count, 3)
        self.assertEqual(report.successful_snapshots, 2)
        self.assertEqual(report.failed_requests, 1)
        self.assertEqual(report.first_snapshot_at, START)
        self.assertEqual(report.last_snapshot_at, START + timedelta(minutes=30))
        self.assertAlmostEqual(report.span_hours, 0.5)
        self.assertTrue(report.has_data)

    def test_the_parse_version_travels_with_the_report(self):
        self.add(START, body())
        self.assertIsNotNone(self.report().parse_version)

    def test_flagged_and_lossy_snapshots_are_counted(self):
        self.add(START, body().replace("# engagement over", "# engagement across"))
        report = self.report()
        self.assertEqual(report.flagged_snapshots, 1)

    def test_an_empty_database_reports_nothing_without_failing(self):
        report = self.report()
        self.assertEqual(report.snapshot_count, 0)
        self.assertFalse(report.has_data)
        self.assertEqual(report.span_hours, 0.0)
        self.assertEqual(len(report.engagement), 4)


if __name__ == "__main__":
    unittest.main()
