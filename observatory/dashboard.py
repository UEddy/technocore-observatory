"""Static dashboard generator.

Build step 4. Reads the database, writes one self-contained HTML file. No
framework, no build step, nothing fetched at page load, and no JavaScript at
all: the core numbers are plain HTML and the charts are SVG generated here, so
the page works with scripting disabled because there is nothing to disable.

Traffic classification is step 5 and is deliberately absent. What ships here is
resource exhaustion, the engagement time series, and the caveats that make both
readable honestly.

Everything that reaches the page goes through `escape`. No string from the
service is printed in this step, and the escaping stays anyway so that the
habit is already in place when step 5 starts printing room names.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone

from . import metrics as metrics_module
from . import store as store_module

DEFAULT_OUTPUT = "site/index.html"

REPO_URL = "https://github.com/technocore-observatory"
ENDPOINT = "https://technocore.chat/rooms"


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def format_int(value: float | None) -> str:
    if value is None:
        return "no data"
    return f"{int(round(value)):,}"


def format_bytes(value: float | None) -> str:
    """Binary units, the way the server writes them."""
    if value is None:
        return "no data"
    step = 1024.0
    units = ("B", "K", "M", "G", "T")
    size = float(value)
    for unit in units:
        if abs(size) < step or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{unit}"
        size /= step
    return f"{size:.1f}T"


def format_share(share: float | None) -> str:
    if share is None:
        return "no data"
    return f"{share * 100:.1f}%"


def format_stamp(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_date(moment: datetime | None) -> str:
    if moment is None:
        return "no date"
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def format_span(hours: float) -> str:
    if hours <= 0:
        return "no span yet"
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def format_rate(rate: metrics_module.Rate | None, formatter=format_int) -> str:
    """The underlying rate, always printed next to any date derived from it."""
    if rate is None or rate.per_hour is None:
        return "not measurable yet"
    per_hour = rate.per_hour
    per_day = rate.per_day or 0.0
    if abs(per_hour) >= 1:
        return f"{formatter(per_hour)} per hour ({formatter(per_day)} per day)"
    return f"{per_hour:.3g} per hour ({formatter(per_day)} per day)"


def line_chart(
    series: metrics_module.Series,
    width: int = 640,
    height: int = 160,
    padding: int = 28,
) -> str:
    """An inline SVG line chart, generated here so the page needs no script."""
    points = series.points
    title = escape(f"{series.label} over time")

    if not points:
        return (
            f'<figure class="chart"><figcaption>{escape(series.label)}</figcaption>'
            f'<p class="empty">No samples yet.</p></figure>'
        )

    low = series.minimum or 0.0
    high = series.maximum or 0.0
    if high == low:
        # A flat series still deserves a line, drawn down the middle.
        low, high = low - 1.0, high + 1.0

    first_at = points[0].at
    last_at = points[-1].at
    span = (last_at - first_at).total_seconds() or 1.0

    def x_of(moment: datetime) -> float:
        fraction = (moment - first_at).total_seconds() / span
        return padding + fraction * (width - 2 * padding)

    def y_of(value: float) -> float:
        fraction = (value - low) / (high - low)
        return height - padding - fraction * (height - 2 * padding)

    coordinates = [(x_of(p.at), y_of(p.value)) for p in points]
    path = " ".join(
        ("M" if index == 0 else "L") + f"{x:.1f},{y:.1f}"
        for index, (x, y) in enumerate(coordinates)
    )

    dots = ""
    if len(coordinates) == 1:
        x, y = coordinates[0]
        dots = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" class="dot" />'

    baseline = height - padding
    latest = points[-1].value
    readable_high = f"{high:,.4g}"
    readable_low = f"{low:,.4g}"

    return f"""<figure class="chart">
  <figcaption>{escape(series.label)} <span class="unit">{escape(series.unit)}</span></figcaption>
  <svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}" preserveAspectRatio="none">
    <title>{title}</title>
    <line class="axis" x1="{padding}" y1="{baseline}" x2="{width - padding}" y2="{baseline}" />
    <line class="axis" x1="{padding}" y1="{padding}" x2="{padding}" y2="{baseline}" />
    <path class="line" d="{path}" fill="none" />
    {dots}
    <text class="tick" x="{padding - 4}" y="{padding + 4}" text-anchor="end">{escape(readable_high)}</text>
    <text class="tick" x="{padding - 4}" y="{baseline}" text-anchor="end">{escape(readable_low)}</text>
    <text class="tick" x="{padding}" y="{height - 6}">{escape(format_date(first_at))}</text>
    <text class="tick" x="{width - padding}" y="{height - 6}" text-anchor="end">{escape(format_date(last_at))}</text>
  </svg>
  <p class="reading">Latest {escape(f"{latest:,.4g}")}, from {escape(len(points))} samples over {escape(format_span(series.span_hours))}.</p>
</figure>"""


def meter(share: float | None) -> str:
    if share is None:
        return ""
    percent = max(0.0, min(1.0, share)) * 100
    return (
        f'<div class="meter" role="img" aria-label="{escape(format_share(share))} of cap consumed">'
        f'<span style="width: {percent:.1f}%"></span></div>'
    )


def projection_card(projection: metrics_module.Projection, is_bytes: bool = False) -> str:
    formatter = format_bytes if is_bytes else format_int
    rate = projection.rate
    trailing = projection.trailing_rate

    if projection.has_projection:
        headline = format_date(projection.exhausts_at)
        earliest = format_date(projection.earliest)
        latest = format_date(projection.latest)
        if projection.earliest and projection.latest and earliest != latest:
            band = (
                f"<p class=\"band\">Range {escape(earliest)} to {escape(latest)}, "
                f"from two standard errors on the fitted rate. That range covers the "
                f"scatter in the samples only, not the chance that the rate changes.</p>"
            )
        elif projection.earliest and projection.latest:
            band = (
                '<p class="band">The fit is tight enough that its error range lands on the '
                "same day. That measures how straight the samples are, not how likely the "
                "rate is to hold, and it is the second of those that decides the date.</p>"
            )
        else:
            band = '<p class="band">No range: too few samples to estimate one.</p>'
    else:
        headline = "none"
        band = ""

    trailing_line = ""
    if trailing and trailing.per_hour is not None:
        if projection.trailing_is_same_data:
            detail = (
                f"the record is shorter than {int(metrics_module.TRAILING_WINDOW_DAYS)} days, "
                "so this is the same fit over the same samples, not a second opinion"
            )
        else:
            trailing_date = (
                format_date(projection.trailing_exhausts_at)
                if projection.trailing_exhausts_at
                else "none"
            )
            detail = (
                f"{trailing.samples} samples from the last "
                f"{int(metrics_module.TRAILING_WINDOW_DAYS)} days, giving {trailing_date}"
            )
        trailing_line = (
            f"<dt>Trailing rate</dt><dd>{escape(format_rate(trailing, formatter))}"
            f" <span class=\"muted\">({escape(detail)})</span></dd>"
        )

    caveats = "".join(f"<li>{escape(text)}</li>" for text in projection.caveats)

    return f"""<article class="card">
  <h3>{escape(projection.label.title())}</h3>
  <p class="figure">{escape(formatter(projection.current))} <span class="of">of {escape(formatter(projection.cap))}</span></p>
  <p class="share">{escape(format_share(projection.share))} of cap consumed, {escape(formatter(projection.headroom))} left</p>
  {meter(projection.share)}
  <dl>
    <dt>Rate</dt><dd>{escape(format_rate(rate, formatter))}
      <span class="muted">({escape(rate.samples if rate else 0)} samples over {escape(format_span(rate.span_hours if rate else 0))})</span></dd>
    {trailing_line}
    <dt>Cap reached</dt><dd class="projected">{escape(headline)}</dd>
  </dl>
  {band}
  <ul class="caveats">{caveats}</ul>
</article>"""


STYLE = """
:root {
  color-scheme: light dark;
  --ink: #16181d;
  --muted: #5b6270;
  --bg: #ffffff;
  --panel: #f5f6f8;
  --line: #d8dbe2;
  --accent: #1f6feb;
  --warn: #8a5a00;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e7e9ee;
    --muted: #a2a9b8;
    --bg: #101216;
    --panel: #181b21;
    --line: #2a2f39;
    --accent: #5b9dff;
    --warn: #e0b062;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  padding: 1.5rem 1rem 4rem;
  max-width: 62rem;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.55 system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
}
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2.5rem 0 .75rem; }
h3 { font-size: 1rem; margin: 0 0 .5rem; }
a { color: var(--accent); }
.lede { color: var(--muted); margin: 0 0 1.25rem; max-width: 46rem; }
.stamp {
  display: flex; flex-wrap: wrap; gap: 1rem 2rem;
  padding: 1rem 1.15rem; margin: 0 0 1.5rem;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
}
.stamp div { min-width: 8rem; }
.stamp .value { font-size: 1.5rem; font-weight: 650; display: block; line-height: 1.2; }
.stamp .key { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
.method { border-left: 3px solid var(--accent); padding: .1rem 0 .1rem 1rem; margin: 0 0 1rem; }
.method p { margin: .5rem 0; }
.cards { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr)); }
.card { padding: 1rem 1.15rem; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
.figure { font-size: 1.5rem; font-weight: 650; margin: .1rem 0; }
.figure .of { font-size: .95rem; font-weight: 400; color: var(--muted); }
.share { margin: .1rem 0 .6rem; color: var(--muted); font-size: .9rem; }
.meter { height: 8px; background: var(--line); border-radius: 99px; overflow: hidden; margin: 0 0 .8rem; }
.meter span { display: block; height: 100%; background: var(--accent); }
dl { margin: 0; display: grid; grid-template-columns: 1fr; gap: .1rem; }
dt { font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin-top: .5rem; }
dd { margin: 0; }
dd.projected { font-size: 1.15rem; font-weight: 650; }
.band { font-size: .85rem; color: var(--muted); margin: .5rem 0 0; }
.muted { color: var(--muted); font-size: .85rem; }
.caveats { margin: .75rem 0 0; padding-left: 1.1rem; font-size: .85rem; color: var(--warn); }
.caveats li { margin: .3rem 0; }
.charts { display: grid; gap: 1.25rem; grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr)); }
.chart { margin: 0; padding: 1rem 1.15rem; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
.chart figcaption { font-weight: 650; margin-bottom: .4rem; }
.chart .unit { font-weight: 400; color: var(--muted); font-size: .85rem; }
.chart svg { width: 100%; height: auto; display: block; }
.chart .line { stroke: var(--accent); stroke-width: 2; }
.chart .dot { fill: var(--accent); }
.chart .axis { stroke: var(--line); stroke-width: 1; }
.chart .tick { fill: var(--muted); font-size: 11px; }
.chart .reading { font-size: .85rem; color: var(--muted); margin: .5rem 0 0; }
.chart .empty { color: var(--muted); }
.note { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 1rem 1.15rem; }
.note p:first-child { margin-top: 0; }
.note p:last-child { margin-bottom: 0; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--muted); font-size: .85rem; }
"""


def render(report: metrics_module.Report) -> str:
    """The whole page, as one string."""
    coverage = report.coverage

    by_key = {projection.key: projection for projection in report.projections}
    cards = "".join(
        projection_card(by_key[key], is_bytes=(key == "bytes"))
        for key in ("rooms", "notes", "bytes")
        if key in by_key
    )

    charts = "".join(line_chart(series) for series in report.engagement)

    turnover = "not enough samples yet"
    if coverage.median_replaced_per_sample is not None and coverage.window_size:
        gap = (
            f"{coverage.sample_gap_minutes:.0f} minutes"
            if coverage.sample_gap_minutes
            else "one interval"
        )
        turnover = (
            f"at least {format_int(coverage.median_replaced_per_sample)} of "
            f"{format_int(coverage.window_size)} rooms in the window were replaced "
            f"between consecutive samples, {gap} apart (median of "
            f"{format_int(coverage.pairs_compared)} pairs)"
        )

    share_line = ""
    if coverage.share_of_reported is not None:
        share_line = (
            f" That is at least {escape(format_share(coverage.share_of_reported))} of the "
            f"{escape(format_int(coverage.rooms_reported_by_server))} rooms the server "
            f"currently reports, and the true share of rooms that existed is higher, "
            f"because rooms the sampler never saw are not counted."
        )

    health = ""
    if report.failed_requests or report.flagged_snapshots or report.lossy_snapshots:
        health = (
            f"<p>Of those attempts, {escape(format_int(report.failed_requests))} failed outright, "
            f"{escape(format_int(report.flagged_snapshots))} did not parse cleanly and are kept "
            f"flagged with their raw text, and {escape(format_int(report.lossy_snapshots))} arrived "
            f"with a body that did not decode cleanly. Failed and flagged snapshots are counted "
            f"here rather than dropped, because leaving them out would overstate how well the "
            f"sampling went.</p>"
        )

    empty_notice = ""
    if not report.has_data:
        empty_notice = (
            '<p class="note">No successful snapshots yet, so every figure below is empty. '
            "The methodology and the request budget are stated anyway, because they are what "
            "the numbers will have to be read against.</p>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Technocore Observatory</title>
<meta name="description" content="A sampling-based record of the technocore.chat network: resource consumption against the server's published caps, and the engagement figures the server publishes about itself, tracked over time.">
<style>{STYLE}</style>
</head>
<body>

<h1>Technocore Observatory</h1>
<p class="lede">A sampling-based record of the technocore.chat network. Resource
consumption against the server's own published caps, and the engagement figures
the server publishes about itself, tracked over time.</p>

<section class="stamp" aria-label="Freshness of the data behind this page">
  <div>
    <span class="key">Last updated</span>
    <span class="value">{escape(format_stamp(report.last_snapshot_at))}</span>
  </div>
  <div>
    <span class="key">Snapshots</span>
    <span class="value">{escape(format_int(report.successful_snapshots))}</span>
  </div>
  <div>
    <span class="key">Observed over</span>
    <span class="value">{escape(format_span(report.span_hours))}</span>
  </div>
  <div>
    <span class="key">First snapshot</span>
    <span class="value">{escape(format_stamp(report.first_snapshot_at))}</span>
  </div>
</section>

{empty_notice}

<section class="method" aria-label="Methodology and request budget">
  <p><strong>How this is sampled.</strong> One <code>GET {escape(ENDPOINT)}</code>
  every 15 minutes, from a single sequential worker, under an absolute ceiling of
  30 requests per hour across all endpoints. On 429 the sampler honors
  <code>Retry-After</code>; on 503 it backs off exponentially from 60 seconds to a
  cap of 30 minutes, and never retries tighter than the interval before. No room is
  ever fetched individually and the room list is never crawled.</p>

  <p><strong>What the numbers are.</strong> The header and footer figures are
  network-wide aggregates published by the server itself, and they are the server's
  claims, not measurements of ours. The 50 room lines in each response are a rolling
  window biased toward whatever is active at that moment.</p>

  <p><strong>Room names and topics are unverified.</strong> A room name is a string
  its creator chose, and a topic is a note that any caller can set on any room
  without ever posting to it. Neither is a claim about what a room is or who runs
  it. None are shown on this page yet; classification is not part of this release.</p>
</section>

<h2>Resource consumption</h2>
<div class="cards">
{cards}
</div>

<h2>Engagement, as the server reports it</h2>
<p class="lede">These four figures come from the response footer. They are the
numbers the operator chose to publish, which is a signal in itself about what is
being watched. Every point is one snapshot.</p>
<div class="charts">
{charts}
</div>

<h2>Coverage, and why it is a floor</h2>
<div class="note">
  <p>The sampler has seen <strong>{escape(format_int(coverage.rooms_seen))} distinct
  room paths</strong> across {escape(format_int(coverage.observations))} observations.
  <strong>That is a lower bound on the active set, not a census.</strong>{share_line}</p>

  <p>Every room in the window carries an idle time of under a minute, so the window
  turns over roughly once a minute while the sampler reads it once every fifteen. In
  the samples collected so far, {escape(turnover)}. Any room that appears and goes
  quiet between two samples is never recorded at all. Both the count above and the
  turnover figure are floors: what the data supports is that at least this many rooms
  were active and at least this much of the window changed. Nothing here can support
  a claim about how many rooms are active in total.</p>

  {health}
</div>

<footer>
  <p>Page generated {escape(format_stamp(report.generated_at))} from
  {escape(format_int(report.snapshot_count))} archived request attempts
  ({escape(format_int(report.successful_snapshots))} successful), parse version
  {escape(report.parse_version or "unknown")}.</p>
  <p>The raw responses, the code that collected them and the rules behind every
  figure here are in the repository. The database is rebuilt from the newline
  delimited archive, so every number on this page can be recomputed from the
  committed data. MIT licensed.</p>
  <p><a href="{escape(REPO_URL)}">Source and archive</a></p>
</footer>

</body>
</html>
"""


def write(db_path: str = store_module.DEFAULT_DB_PATH, out_path: str = DEFAULT_OUTPUT) -> str:
    """Build the page from the database and write it. Returns the path."""
    connection = store_module.connect(db_path)
    try:
        report = metrics_module.build_report(connection)
    finally:
        connection.close()

    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render(report))
    return out_path


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m observatory.dashboard",
        description=(
            "Generate the static dashboard from the database. Writes one "
            "self-contained HTML file with no scripts and no external assets."
        ),
    )
    parser.add_argument("--db", default=store_module.DEFAULT_DB_PATH, help="database path")
    parser.add_argument("--out", default=DEFAULT_OUTPUT, help=f"output file (default {DEFAULT_OUTPUT})")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"no database at {args.db}. Build one with: python -m observatory.store")
        return 1

    path = write(args.db, args.out)
    size = os.path.getsize(path)
    print(f"wrote {path} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
