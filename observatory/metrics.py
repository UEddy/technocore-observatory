"""Metrics computed at report time.

Build step 4: resource exhaustion and the engagement time series. Nothing here
is stored; everything is derived from the database on demand, and the database
is itself derived from the NDJSON archive. Traffic classification is step 5 and
is deliberately absent.

Two honesty rules run through this module.

**A projection is a rate plus an assumption.** Every projection carries the
rate it was computed from, the number of samples and the span of time behind
it, and a range rather than a single date. A linear fit on a few days of launch
traffic is a weak forecast, and the caller is given what it needs to say so.

**Coverage is a floor.** The 50 room window turns over roughly once a minute
against a sampling interval of fifteen, so the rooms this project has seen are
a lower bound on the rooms that existed, never a census. Anything derived from
accumulated coverage is named and returned as a floor.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import archive as archive_module

# How much of the observed span a trailing projection covers.
TRAILING_WINDOW_DAYS = 7.0
# Below this many samples, or this many hours of span, a projection is little
# more than a straight line through noise and is labelled as such.
MIN_SAMPLES_FOR_PROJECTION = 3
WEAK_SPAN_HOURS = 48.0
WEAK_SAMPLES = 24

# The resources with a published cap, and where to read them.
RESOURCES = (
    ("rooms", "room slots", "rooms_total", "room_cap"),
    ("notes", "note namespace", "notes_total", "notes_cap"),
    ("bytes", "bytes stored", "bytes_stored", "bytes_cap"),
)


@dataclass
class Point:
    at: datetime
    value: float


@dataclass
class Series:
    """One measurement over time."""

    key: str
    label: str
    points: list[Point] = field(default_factory=list)
    unit: str = ""

    def __len__(self) -> int:
        return len(self.points)

    @property
    def first(self) -> Point | None:
        return self.points[0] if self.points else None

    @property
    def last(self) -> Point | None:
        return self.points[-1] if self.points else None

    @property
    def span_hours(self) -> float:
        if len(self.points) < 2:
            return 0.0
        return (self.points[-1].at - self.points[0].at).total_seconds() / 3600.0

    @property
    def minimum(self) -> float | None:
        return min((p.value for p in self.points), default=None)

    @property
    def maximum(self) -> float | None:
        return max((p.value for p in self.points), default=None)

    def since(self, cutoff: datetime) -> "Series":
        return Series(
            key=self.key,
            label=self.label,
            unit=self.unit,
            points=[p for p in self.points if p.at >= cutoff],
        )


@dataclass
class Rate:
    """A least squares slope, with what it was fitted on.

    `standard_error` is None when there are too few points for the residuals to
    mean anything. It is the basis of the projection range, and its absence is
    itself worth reporting.
    """

    method: str
    per_hour: float | None = None
    standard_error: float | None = None
    samples: int = 0
    span_hours: float = 0.0

    @property
    def per_day(self) -> float | None:
        return None if self.per_hour is None else self.per_hour * 24.0

    @property
    def low_per_hour(self) -> float | None:
        """Slower end of a rough two standard error band."""
        if self.per_hour is None:
            return None
        if self.standard_error is None:
            return None
        return self.per_hour - 2.0 * self.standard_error

    @property
    def high_per_hour(self) -> float | None:
        if self.per_hour is None:
            return None
        if self.standard_error is None:
            return None
        return self.per_hour + 2.0 * self.standard_error


@dataclass
class Projection:
    """When a resource reaches its cap, if the current rate holds.

    It usually will not. `caveats` says why in plain words, and the dashboard
    is expected to print them next to the date rather than beneath it.
    """

    key: str
    label: str
    current: float | None = None
    cap: float | None = None
    rate: Rate | None = None
    trailing_rate: Rate | None = None
    exhausts_at: datetime | None = None
    earliest: datetime | None = None
    latest: datetime | None = None
    trailing_exhausts_at: datetime | None = None
    trailing_is_same_data: bool = False
    hours_left: float | None = None
    caveats: list[str] = field(default_factory=list)
    series: Series | None = None

    @property
    def share(self) -> float | None:
        if self.current is None or not self.cap:
            return None
        return self.current / self.cap

    @property
    def headroom(self) -> float | None:
        if self.current is None or self.cap is None:
            return None
        return self.cap - self.current

    @property
    def has_projection(self) -> bool:
        return self.exhausts_at is not None


@dataclass
class Coverage:
    """What the sampler has seen, stated as the floor that it is."""

    rooms_seen: int = 0
    observations: int = 0
    rooms_reported_by_server: int | None = None
    window_size: int | None = None
    median_replaced_per_sample: float | None = None
    sample_gap_minutes: float | None = None
    pairs_compared: int = 0

    @property
    def share_of_reported(self) -> float | None:
        if not self.rooms_reported_by_server:
            return None
        return self.rooms_seen / self.rooms_reported_by_server

    @property
    def replaced_share(self) -> float | None:
        if not self.window_size or self.median_replaced_per_sample is None:
            return None
        return self.median_replaced_per_sample / self.window_size


@dataclass
class Report:
    """Everything the dashboard needs, computed in one pass."""

    generated_at: datetime
    snapshot_count: int = 0
    successful_snapshots: int = 0
    failed_requests: int = 0
    flagged_snapshots: int = 0
    lossy_snapshots: int = 0
    first_snapshot_at: datetime | None = None
    last_snapshot_at: datetime | None = None
    projections: list[Projection] = field(default_factory=list)
    engagement: list[Series] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    parse_version: str | None = None

    @property
    def span_hours(self) -> float:
        if not self.first_snapshot_at or not self.last_snapshot_at:
            return 0.0
        return (self.last_snapshot_at - self.first_snapshot_at).total_seconds() / 3600.0

    @property
    def has_data(self) -> bool:
        return self.successful_snapshots > 0


def _to_datetime(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return archive_module.parse_iso(value)
    except ValueError:
        return None


def fit_rate(series: Series, method: str = "least squares over all samples") -> Rate:
    """Least squares slope per hour, with a standard error where possible."""
    points = series.points
    rate = Rate(method=method, samples=len(points), span_hours=series.span_hours)
    if len(points) < 2:
        return rate

    origin = points[0].at
    xs = [(p.at - origin).total_seconds() / 3600.0 for p in points]
    ys = [p.value for p in points]

    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        # Every sample landed at the same instant, which says nothing about a rate.
        return rate

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance
    rate.per_hour = slope

    if len(points) > 2:
        intercept = mean_y - slope * mean_x
        residuals = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        rate.standard_error = math.sqrt(residuals / (len(points) - 2) / variance)

    return rate


def project_to_cap(
    current: float | None,
    cap: float | None,
    per_hour: float | None,
    last_at: datetime | None,
) -> tuple[datetime | None, float | None]:
    """When the cap is reached at this rate. None when it never is."""
    if current is None or cap is None or per_hour is None or last_at is None:
        return None, None
    if per_hour <= 0:
        return None, None
    headroom = cap - current
    if headroom <= 0:
        return last_at, 0.0
    hours = headroom / per_hour
    # Beyond a century the arithmetic is meaningless and datetime overflows.
    if hours > 24 * 365 * 100:
        return None, hours
    return last_at + timedelta(hours=hours), hours


def _projection_caveats(rate: Rate, trailing: Rate, projected: bool) -> list[str]:
    caveats: list[str] = []
    if not projected:
        caveats.append(
            "No exhaustion date: consumption over the observed window is flat or negative."
        )
        return caveats

    caveats.append(
        "This is a straight line drawn through the samples so far, not a forecast. "
        "It assumes the current rate holds, which it will not."
    )
    if rate.samples < WEAK_SAMPLES or rate.span_hours < WEAK_SPAN_HOURS:
        caveats.append(
            f"It rests on {rate.samples} samples across "
            f"{rate.span_hours:.1f} hours, which is far too little to extrapolate from. "
            "Treat the date as an order of magnitude at best."
        )
    if rate.standard_error is None:
        caveats.append(
            "Too few samples to estimate an error range, so no range is shown."
        )
    if trailing.per_hour is not None and rate.per_hour:
        ratio = trailing.per_hour / rate.per_hour if rate.per_hour else None
        if ratio is not None and (ratio > 1.5 or ratio < 0.67):
            caveats.append(
                "The trailing rate and the all-sample rate disagree by more than half, "
                "so the consumption rate is not steady and neither date is reliable."
            )
    return caveats


def resource_series(connection: sqlite3.Connection, column: str) -> Series:
    """One network-wide aggregate over time, oldest first."""
    rows = connection.execute(
        f"""
        SELECT snapshots.fetched_at AS fetched_at, network_stats.{column} AS value
        FROM network_stats
        JOIN snapshots ON snapshots.id = network_stats.snapshot_id
        WHERE network_stats.{column} IS NOT NULL
        ORDER BY snapshots.fetched_at ASC
        """
    ).fetchall()
    points = []
    for row in rows:
        at = _to_datetime(row["fetched_at"])
        if at is None:
            continue
        points.append(Point(at=at, value=float(row["value"])))
    return Series(key=column, label=column, points=points)


def exhaustion(connection: sqlite3.Connection) -> list[Projection]:
    """Consumption against each published cap, with dated projections."""
    projections: list[Projection] = []

    for key, label, value_column, cap_column in RESOURCES:
        series = resource_series(connection, value_column)
        cap_series = resource_series(connection, cap_column)

        projection = Projection(key=key, label=label, series=series)
        projection.current = series.last.value if series.last else None
        projection.cap = cap_series.last.value if cap_series.last else None

        rate = fit_rate(series)
        cutoff = None
        if series.last:
            cutoff = series.last.at - timedelta(days=TRAILING_WINDOW_DAYS)
        trailing_series = series.since(cutoff) if cutoff else series
        trailing = fit_rate(
            trailing_series, method=f"least squares over the last {int(TRAILING_WINDOW_DAYS)} days"
        )

        projection.rate = rate
        projection.trailing_rate = trailing
        # With less than a full trailing window of history, the trailing fit is
        # the same fit over the same points. Saying so stops the page from
        # presenting one number twice as if it were corroboration.
        projection.trailing_is_same_data = trailing.samples == rate.samples

        last_at = series.last.at if series.last else None
        if len(series) >= MIN_SAMPLES_FOR_PROJECTION or len(series) >= 2:
            projection.exhausts_at, projection.hours_left = project_to_cap(
                projection.current, projection.cap, rate.per_hour, last_at
            )
            # A faster rate exhausts sooner, so the high rate gives the
            # earliest date and the low rate the latest.
            projection.earliest, _ = project_to_cap(
                projection.current, projection.cap, rate.high_per_hour, last_at
            )
            projection.latest, _ = project_to_cap(
                projection.current, projection.cap, rate.low_per_hour, last_at
            )
            projection.trailing_exhausts_at, _ = project_to_cap(
                projection.current, projection.cap, trailing.per_hour, last_at
            )

        projection.caveats = _projection_caveats(rate, trailing, projection.has_projection)
        if len(series) < MIN_SAMPLES_FOR_PROJECTION:
            projection.caveats.insert(
                0,
                f"Only {len(series)} usable samples so far. "
                "There is not yet enough data for any projection worth printing.",
            )
        projections.append(projection)

    return projections


ENGAGEMENT_FIGURES = (
    ("zero_response_rate", "Zero-response rate", "share of messages"),
    ("nick_diversity", "Nick diversity", "index"),
    ("notes_per_msg", "Notes per message", "notes"),
    ("msgs_scanned", "Messages scanned", "messages"),
)


def engagement(connection: sqlite3.Connection) -> list[Series]:
    """The four figures the server publishes about itself, over time."""
    out = []
    for column, label, unit in ENGAGEMENT_FIGURES:
        series = resource_series(connection, column)
        series.label = label
        series.unit = unit
        out.append(series)
    return out


def coverage(connection: sqlite3.Connection) -> Coverage:
    """What has been seen, as a floor.

    The window turnover figure is the count of rooms present in one sample and
    gone by the next. When the window turns over faster than the sampler
    samples, this saturates at the window size, which is the honest answer: at
    least this much changed, and the real figure is unmeasurable from here.
    """
    result = Coverage()

    row = connection.execute(
        "SELECT COUNT(*) AS rooms, COALESCE(SUM(observation_count), 0) AS observations FROM rooms"
    ).fetchone()
    result.rooms_seen = int(row["rooms"])
    result.observations = int(row["observations"])

    row = connection.execute(
        """
        SELECT network_stats.rooms_total AS rooms_total
        FROM network_stats
        JOIN snapshots ON snapshots.id = network_stats.snapshot_id
        WHERE network_stats.rooms_total IS NOT NULL
        ORDER BY snapshots.fetched_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row:
        result.rooms_reported_by_server = int(row["rooms_total"])

    windows: list[tuple[datetime, set[str]]] = []
    current_id = None
    current: set[str] = set()
    current_at: datetime | None = None
    for row in connection.execute(
        """
        SELECT snapshots.id AS snapshot_id, snapshots.fetched_at AS fetched_at,
               room_observations.room_path AS room_path
        FROM room_observations
        JOIN snapshots ON snapshots.id = room_observations.snapshot_id
        ORDER BY snapshots.fetched_at ASC, snapshots.id ASC
        """
    ):
        if row["snapshot_id"] != current_id:
            if current_id is not None and current_at is not None:
                windows.append((current_at, current))
            current_id = row["snapshot_id"]
            current_at = _to_datetime(row["fetched_at"])
            current = set()
        current.add(row["room_path"])
    if current_id is not None and current_at is not None:
        windows.append((current_at, current))

    if windows:
        result.window_size = max(len(paths) for _, paths in windows)

    replaced: list[int] = []
    gaps: list[float] = []
    for (earlier_at, earlier), (later_at, later) in zip(windows, windows[1:]):
        replaced.append(len(earlier - later))
        gaps.append((later_at - earlier_at).total_seconds() / 60.0)

    result.pairs_compared = len(replaced)
    if replaced:
        ordered = sorted(replaced)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            result.median_replaced_per_sample = float(ordered[middle])
        else:
            result.median_replaced_per_sample = (ordered[middle - 1] + ordered[middle]) / 2.0
    if gaps:
        ordered_gaps = sorted(gaps)
        middle = len(ordered_gaps) // 2
        if len(ordered_gaps) % 2:
            result.sample_gap_minutes = ordered_gaps[middle]
        else:
            result.sample_gap_minutes = (ordered_gaps[middle - 1] + ordered_gaps[middle]) / 2.0

    return result


def build_report(connection: sqlite3.Connection, now: datetime | None = None) -> Report:
    """Everything the dashboard needs, in one pass over the database."""
    report = Report(generated_at=now or datetime.now(timezone.utc))

    row = connection.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_count,
            SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed,
            SUM(parse_flagged) AS flagged,
            SUM(body_lossy) AS lossy,
            MIN(fetched_at) AS first_at,
            MAX(fetched_at) AS last_at
        FROM snapshots
        """
    ).fetchone()
    if row:
        report.snapshot_count = int(row["total"] or 0)
        report.successful_snapshots = int(row["ok_count"] or 0)
        report.failed_requests = int(row["failed"] or 0)
        report.flagged_snapshots = int(row["flagged"] or 0)
        report.lossy_snapshots = int(row["lossy"] or 0)
        report.first_snapshot_at = _to_datetime(row["first_at"])
        report.last_snapshot_at = _to_datetime(row["last_at"])

    version = connection.execute(
        "SELECT value FROM meta WHERE key = ?", ("parse_version",)
    ).fetchone()
    if version:
        report.parse_version = version["value"]

    report.projections = exhaustion(connection)
    report.engagement = engagement(connection)
    report.coverage = coverage(connection)
    return report
