import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from observatory import dashboard, metrics, store
from observatory.archive import Archive, make_record

from tests.test_metrics import START, body


def strip_tags(html_text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text)).strip()


class TagBalance(HTMLParser):
    """Enough of a check to catch an unclosed block, without a dependency."""

    VOID = {"meta", "br", "hr", "img", "input", "link", "circle", "line", "path", "use"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.problems = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack:
            self.problems.append(f"closing {tag} with nothing open")
        elif self.stack[-1] != tag:
            self.problems.append(f"closing {tag} while {self.stack[-1]} is open")
        else:
            self.stack.pop()


class DashboardTestCase(unittest.TestCase):
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

    def history(self, samples=96, rooms_step=3, room_shift=0):
        for index in range(samples):
            self.add(
                START + timedelta(minutes=15 * index),
                body(
                    rooms=17740 + rooms_step * index,
                    notes=426362 + 100 * index,
                    room_shift=room_shift if index else 0,
                ),
            )

    def page(self):
        store.build(self.db_path, self.store)
        connection = store.connect(self.db_path)
        try:
            report = metrics.build_report(connection, now=START + timedelta(days=1))
        finally:
            connection.close()
        return dashboard.render(report)


class ProminenceTests(DashboardTestCase):
    def test_the_snapshot_count_and_last_updated_come_before_anything_else(self):
        self.history(samples=10)
        page = self.page()
        stamp = page.index('class="stamp"')
        self.assertLess(stamp, page.index("Resource consumption"))
        self.assertLess(stamp, page.index('class="method"'))

    def test_the_stamp_carries_the_count_the_time_and_the_span(self):
        self.history(samples=10)
        page = self.page()
        block = page.split('class="stamp"')[1].split("</section>")[0]
        self.assertIn("Last updated", block)
        self.assertIn("Snapshots", block)
        self.assertIn("Observed over", block)
        self.assertIn("10", block)
        self.assertIn("2026-08-24", block)

    def test_the_footer_repeats_the_count_and_the_generation_time(self):
        self.history(samples=4)
        page = self.page()
        footer = page.split("<footer>")[1]
        self.assertIn("4", footer)
        self.assertIn("archived request attempts", footer)


class ProjectionCopyTests(DashboardTestCase):
    def test_the_rate_appears_next_to_the_date_it_produced(self):
        self.history(samples=96, rooms_step=3)
        page = self.page()
        card = page.split('<article class="card">')[1].split("</article>")[0]
        text = strip_tags(card)
        self.assertIn("per hour", text)
        self.assertIn("per day", text)
        # Twelve rooms an hour, 288 a day, from three every fifteen minutes.
        self.assertIn("12 per hour", text)
        self.assertIn("288 per day", text)
        self.assertLess(text.index("per hour"), text.index("Cap reached"))

    def test_the_sample_count_and_span_back_the_rate_up(self):
        self.history(samples=96)
        card = strip_tags(self.page().split('<article class="card">')[1])
        self.assertIn("96 samples over", card)

    def test_the_uncertainty_caveat_is_explicit(self):
        self.history(samples=96)
        page = self.page()
        self.assertIn("not a forecast", page)
        self.assertIn("assumes the current rate holds, which it will not", page)

    def test_a_short_record_says_the_projection_is_barely_worth_printing(self):
        self.add(START, body(rooms=17740))
        self.add(START + timedelta(minutes=15), body(rooms=17800))
        page = self.page()
        self.assertIn("far too little to extrapolate from", page)

    def test_a_flat_resource_prints_no_date(self):
        for index in range(20):
            self.add(START + timedelta(minutes=15 * index), body(rooms=17740))
        page = self.page()
        card = page.split('<article class="card">')[1].split("</article>")[0]
        self.assertIn("flat or negative", card)
        self.assertIn("none", strip_tags(card))

    def test_the_error_range_is_labelled_as_covering_scatter_only(self):
        # A far off projection with scatter in the fit, so the two standard
        # error band lands on different days at each end.
        projection = metrics.Projection(
            key="rooms",
            label="room slots",
            current=1000.0,
            cap=20480.0,
            rate=metrics.Rate(method="m", per_hour=2.0, standard_error=0.5, samples=40, span_hours=10.0),
            trailing_rate=metrics.Rate(method="t"),
            exhausts_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            earliest=datetime(2026, 11, 1, tzinfo=timezone.utc),
            latest=datetime(2027, 4, 1, tzinfo=timezone.utc),
        )
        card = dashboard.projection_card(projection)
        self.assertIn("two standard errors", card)
        self.assertIn("not the chance that the rate changes", card)
        self.assertIn("2026-11-01", card)
        self.assertIn("2027-04-01", card)

    def test_a_band_that_lands_on_one_day_says_what_it_does_not_measure(self):
        self.history(samples=96, rooms_step=3)
        page = self.page()
        self.assertIn("error range lands on the", page)
        self.assertIn("not how likely the rate is to hold", page)

    def test_all_three_capped_resources_get_a_card(self):
        self.history(samples=10)
        page = self.page()
        self.assertEqual(page.count('<article class="card">'), 3)
        for heading in ("Room Slots", "Note Namespace", "Bytes Stored"):
            self.assertIn(heading, page)

    def test_the_trailing_rate_admits_when_it_is_the_same_fit(self):
        self.history(samples=96)
        page = self.page()
        self.assertIn("not a second opinion", page)


class CoverageCopyTests(DashboardTestCase):
    def test_the_lower_bound_caveat_is_on_the_page(self):
        self.history(samples=10, room_shift=5)
        page = self.page()
        self.assertIn("lower bound on the active set, not a census", page)

    def test_the_reason_the_bound_is_loose_is_given(self):
        self.history(samples=10, room_shift=5)
        text = strip_tags(self.page())
        self.assertIn("turns over roughly once a minute", text)
        self.assertIn("never recorded at all", text)

    def test_the_turnover_figure_is_stated_as_a_floor(self):
        self.history(samples=10, room_shift=5)
        text = strip_tags(self.page())
        self.assertIn("at least", text)
        self.assertIn("rooms in the window were replaced", text)

    def test_no_claim_is_made_about_the_total_active_set(self):
        self.history(samples=10, room_shift=5)
        text = strip_tags(self.page())
        self.assertIn("Nothing here can support a claim about how many rooms are active in total", text)

    def test_the_share_of_the_server_total_is_qualified(self):
        self.history(samples=10)
        text = strip_tags(self.page())
        self.assertIn("That is at least", text)
        self.assertIn("the true share of rooms that existed is higher", text)


class MethodologyTests(DashboardTestCase):
    def test_the_sampling_method_is_stated(self):
        self.history(samples=4)
        text = strip_tags(self.page())
        self.assertIn("every 15 minutes", text)
        self.assertIn("single sequential worker", text)

    def test_the_request_budget_is_stated(self):
        self.history(samples=4)
        text = strip_tags(self.page())
        self.assertIn("30 requests per hour", text)
        self.assertIn("Retry-After", text)
        self.assertIn("never retries tighter", text)

    def test_names_and_topics_are_marked_unverified(self):
        self.history(samples=4)
        text = strip_tags(self.page())
        self.assertIn("Room names and topics are unverified", text)
        self.assertIn("any caller can set on any room", text)

    def test_the_server_figures_are_marked_as_the_servers_claims(self):
        self.history(samples=4)
        self.assertIn("the server's claims, not measurements of ours", strip_tags(self.page()))

    def test_classification_is_absent_and_said_to_be_absent(self):
        self.history(samples=4)
        page = self.page()
        self.assertIn("classification is not part of this release", page)
        for word in ("template cluster", "slot squat", "farm"):
            self.assertNotIn(word, page.lower())

    def test_failed_and_flagged_snapshots_are_owned_up_to(self):
        self.history(samples=4)
        self.add(START + timedelta(hours=2), "gone", ok=False, http_status=503)
        text = strip_tags(self.page())
        self.assertIn("failed outright", text)
        self.assertIn("rather than dropped", text)


class RenderingTests(DashboardTestCase):
    def test_the_page_carries_no_script_at_all(self):
        self.history(samples=10)
        page = self.page().lower()
        self.assertNotIn("<script", page)
        self.assertNotIn("javascript:", page)
        self.assertNotIn("onclick", page)

    def test_nothing_is_fetched_at_page_load(self):
        self.history(samples=10)
        page = self.page()
        self.assertEqual(re.findall(r'src="[^"]+"', page), [])
        self.assertEqual(re.findall(r'<link[^>]+>', page), [])
        # The only external reference is a link the reader can choose to follow.
        external = re.findall(r'href="(https?://[^"]+)"', page)
        self.assertEqual(external, [dashboard.REPO_URL])

    def test_the_charts_are_inline_svg(self):
        self.history(samples=10)
        page = self.page()
        self.assertEqual(page.count("<svg"), 4)
        self.assertIn('<path class="line"', page)
        self.assertIn("<title>", page)

    def test_tags_are_balanced(self):
        self.history(samples=10)
        checker = TagBalance()
        checker.feed(self.page())
        self.assertEqual(checker.problems, [])

    def test_the_page_declares_its_charset_and_viewport(self):
        self.history(samples=4)
        page = self.page()
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn("width=device-width", page)

    def test_an_empty_database_still_renders_the_methodology(self):
        page = self.page()
        self.assertIn("No successful snapshots yet", page)
        self.assertIn("30 requests per hour", page)
        self.assertIn("lower bound on the active set", page)
        self.assertIn("No samples yet", page)


class EscapingTests(unittest.TestCase):
    def test_text_from_a_series_label_cannot_break_out(self):
        report = metrics.Report(generated_at=datetime(2026, 8, 27, tzinfo=timezone.utc))
        report.engagement = [
            metrics.Series(
                key="k",
                label="<script>alert(1)</script>",
                unit='" onload="alert(2)',
                points=[metrics.Point(at=START, value=1.0)],
            )
        ]
        page = dashboard.render(report)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('onload="alert', page)

    def test_the_escape_helper_covers_quotes(self):
        self.assertEqual(dashboard.escape('a"b'), "a&quot;b")
        self.assertEqual(dashboard.escape("<b>"), "&lt;b&gt;")


class ChartTests(unittest.TestCase):
    def series(self, values):
        return metrics.Series(
            key="k",
            label="Test series",
            unit="things",
            points=[
                metrics.Point(at=START + timedelta(hours=index), value=value)
                for index, value in enumerate(values)
            ],
        )

    def test_a_normal_series_draws_a_path(self):
        svg = dashboard.line_chart(self.series([1, 5, 3, 9]))
        self.assertIn('<path class="line"', svg)
        commands = re.search(r'<path class="line" d="([^"]+)"', svg).group(1)
        self.assertEqual(commands.count("M"), 1)
        self.assertEqual(commands.count("L"), 3)
        self.assertIn("Test series", svg)
        self.assertIn("things", svg)

    def test_an_empty_series_says_so_rather_than_drawing_nothing(self):
        svg = dashboard.line_chart(metrics.Series(key="k", label="Empty"))
        self.assertIn("No samples yet", svg)
        self.assertNotIn("<svg", svg)

    def test_a_single_point_gets_a_dot(self):
        svg = dashboard.line_chart(self.series([7]))
        self.assertIn("<circle", svg)

    def test_a_flat_series_still_draws(self):
        svg = dashboard.line_chart(self.series([3, 3, 3]))
        self.assertIn('<path class="line"', svg)

    def test_the_reading_line_reports_samples_and_span(self):
        svg = dashboard.line_chart(self.series([1, 2, 3]))
        self.assertIn("3 samples", svg)
        self.assertIn("2.0 hours", svg)


class FormattingTests(unittest.TestCase):
    def test_integers_get_thousands_separators(self):
        self.assertEqual(dashboard.format_int(17740), "17,740")
        self.assertEqual(dashboard.format_int(None), "no data")

    def test_bytes_use_the_units_the_server_uses(self):
        self.assertEqual(dashboard.format_bytes(599), "599B")
        self.assertEqual(dashboard.format_bytes(5 * 1024**3), "5.0G")
        self.assertEqual(dashboard.format_bytes(None), "no data")

    def test_shares_are_percentages(self):
        self.assertEqual(dashboard.format_share(0.911), "91.1%")
        self.assertEqual(dashboard.format_share(None), "no data")

    def test_spans_switch_from_hours_to_days(self):
        self.assertEqual(dashboard.format_span(0), "no span yet")
        self.assertEqual(dashboard.format_span(6), "6.0 hours")
        self.assertEqual(dashboard.format_span(72), "3.0 days")

    def test_a_missing_rate_says_so(self):
        self.assertEqual(dashboard.format_rate(None), "not measurable yet")
        self.assertEqual(
            dashboard.format_rate(metrics.Rate(method="m")), "not measurable yet"
        )

    def test_a_small_rate_keeps_its_precision(self):
        rate = metrics.Rate(method="m", per_hour=0.0125)
        self.assertIn("0.0125 per hour", dashboard.format_rate(rate))


class WriteTests(DashboardTestCase):
    def test_writing_creates_the_directory_and_the_file(self):
        self.history(samples=4)
        store.build(self.db_path, self.store)
        out = os.path.join(self.directory, "site", "index.html")
        path = dashboard.write(self.db_path, out)
        self.assertEqual(path, out)
        self.assertTrue(os.path.exists(out))
        with open(out, "r", encoding="utf-8") as handle:
            self.assertIn("Technocore Observatory", handle.read())

    def test_the_command_reports_where_it_wrote(self):
        import contextlib
        import io

        self.history(samples=4)
        store.build(self.db_path, self.store)
        out = os.path.join(self.directory, "site", "index.html")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = dashboard.main(["--db", self.db_path, "--out", out])
        self.assertEqual(code, 0)
        self.assertIn("wrote", buffer.getvalue())

    def test_the_command_says_what_to_do_when_there_is_no_database(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = dashboard.main(
                ["--db", os.path.join(self.directory, "nope.db"), "--out", "unused.html"]
            )
        self.assertEqual(code, 1)
        self.assertIn("python -m observatory.store", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
