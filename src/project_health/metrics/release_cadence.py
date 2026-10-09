"""Release cadence metrics (issue #135, METRICS.md §6).

Four metrics, all `definition_version "1.0"`, computed from the `release`
raw table (`collectors/release.py`, `schema/tables.py` RELEASE) -- every
row's authoritative `release_date` is a git tag's own `creatordate` (never
`archive_date`, which is a cross-check-only, non-authoritative field -- see
`collectors/release.py`'s module docstring). Published as plain,
individually-rendered metrics (DECISIONS.md D29: composite/dimension
scoring is not published; `scoring/registry.py`'s pre-existing "key"/
"supporting" classification of these metric_ids stays a true taxonomic
fact about METRICS.md §6, just no longer something a composite is built
from):

- `release_frequency` -- METRICS.md §6: count of GA releases in the
  trailing-24-calendar-month window ending at each completed month. A raw
  count is meaningful at any value, including 0 (a 24-month window with
  zero releases is exactly the stagnation signal this metric exists to
  surface), so it carries no sample-size floor, the same reasoning
  `metrics/engine.py`'s `HEADCOUNT_METRICS` constant documents for its own
  three count metrics (passing `floor=0` to `_make_row` achieves the
  identical "always ok, value=n" behavior without repurposing that
  headcount-specific constant for a non-headcount concept). CHAOSS
  Knowledge Base "Release Frequency" is an exact-name match (verified live
  2026-10-09, `site/metrics_meta.py`).

- `release_regularity` -- METRICS.md §6: coefficient of variation (CoV =
  stdev / mean) of inter-release gaps (days) within the same trailing-24m
  window, floor of 3 releases (2 intervals) per window, direction `none`
  (an intentional long freeze, e.g. Cassandra's 4.0 stabilization period,
  spikes this without the project being less healthy -- SCORING.md §6).
  `details_json` also carries `median_days_between_releases` and
  `mean_days_between_releases` for the same audit trail `days_between_
  releases` (below) publishes as its own metric.

- `days_between_releases` -- issue #135's own "a companion
  days_between_releases (median gap, trailing 12 months)": the median of
  the same inter-release gaps `release_regularity` computes, published as
  its own metric_id rather than folded only into that metric's
  `details_json` (D29 follow-up: once publication stopped being gated by a
  dimension's 1-3 key-metric cap, there was no longer a reason to keep it
  detail-only). Same window/floor as `release_regularity`.

- `time_since_last_release` -- METRICS.md §6: days between the most recent
  GA release and `as_of`, direction `none`, snapshot (one row per run, not
  a monthly series) -- emits no row at all until at least one GA release
  is known (nothing to report yet, same as every other dense-month metric
  returning `[]` for "no data at all yet").

Reuses `metrics.engine`'s private helpers (`_dense_months`, `_make_row`)
and `metrics.windows.trailing_24m_window`/`month_end`, the same "new
metrics live in their own module, `compute_all` gets one import plus one
`rows.extend(...)`" pattern `metrics/dev_metrics.py` established for issue
#54.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime

import duckdb

from project_health.metrics.engine import _dense_months, _make_row
from project_health.metrics.windows import month_end, trailing_24m_window

# METRICS.md §6 `release_regularity`: "floor of 3 releases (2 intervals) in
# the window; below that, insufficient data."
RELEASE_REGULARITY_MIN_RELEASES = 3


def _all_release_dates(con: duckdb.DuckDBPyConnection) -> list[date]:
    rows = con.execute("SELECT release_date FROM release ORDER BY release_date").fetchall()
    return [row[0] for row in rows]


def _gaps_in_window(dates: list[date], start: date, end: date) -> tuple[int, list[int]]:
    """`(n_releases, gap_days)` for every GA release in `[start, end]` --
    `n_releases` is the window's release count, `gap_days` is the list of
    day-counts between each consecutive pair (one shorter than
    `n_releases`). Shared by `_release_regularity` and
    `_days_between_releases`, which both window the same way but report a
    different statistic over the same gaps."""
    in_window = sorted(d for d in dates if start <= d <= end)
    gaps = [(in_window[i] - in_window[i - 1]).days for i in range(1, len(in_window))]
    return len(in_window), gaps


def _release_frequency(
    dates: list[date],
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> list[dict]:
    if not dates:
        return []
    first_month = dates[0].replace(day=1)
    rows: list[dict] = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        start, end = trailing_24m_window(window_end)
        n = sum(1 for d in dates if start <= d <= end)
        rows.append(
            _make_row(
                metric_id="release_frequency",
                window_start=start,
                window_end=end,
                raw_value=float(n),
                n=n,
                # No sample-size floor -- see module docstring.
                floor=0,
                run_id=run_id,
                computed_at=computed_at,
                details={"window_months": 24},
            )
        )
    return rows


def _release_regularity(
    dates: list[date],
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> list[dict]:
    if not dates:
        return []
    first_month = dates[0].replace(day=1)
    rows: list[dict] = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        start, end = trailing_24m_window(window_end)
        n, gaps_days = _gaps_in_window(dates, start, end)
        raw_value: float | None = None
        mean_days: float | None = None
        median_days: float | None = None
        if n >= RELEASE_REGULARITY_MIN_RELEASES and gaps_days:
            mean_days = statistics.mean(gaps_days)
            median_days = statistics.median(gaps_days)
            if mean_days > 0 and len(gaps_days) >= 2:
                stdev_days = statistics.stdev(gaps_days)
                raw_value = stdev_days / mean_days
        rows.append(
            _make_row(
                metric_id="release_regularity",
                window_start=start,
                window_end=end,
                raw_value=raw_value,
                n=n,
                floor=RELEASE_REGULARITY_MIN_RELEASES,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "window_months": 24,
                    "n_intervals": len(gaps_days),
                    "mean_days_between_releases": mean_days,
                    "median_days_between_releases": median_days,
                },
            )
        )
    return rows


def _days_between_releases(
    dates: list[date],
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> list[dict]:
    """Issue #135's own "companion days_between_releases (median gap)" --
    the median of the same inter-release gaps `_release_regularity` already
    computes, published as its own metric (D29 follow-up: not gated by a
    dimension's key-metric cap, so no longer detail-only)."""
    if not dates:
        return []
    first_month = dates[0].replace(day=1)
    rows: list[dict] = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        start, end = trailing_24m_window(window_end)
        n, gaps_days = _gaps_in_window(dates, start, end)
        raw_value: float | None = None
        mean_days: float | None = None
        if n >= RELEASE_REGULARITY_MIN_RELEASES and gaps_days:
            raw_value = statistics.median(gaps_days)
            mean_days = statistics.mean(gaps_days)
        rows.append(
            _make_row(
                metric_id="days_between_releases",
                window_start=start,
                window_end=end,
                raw_value=raw_value,
                n=n,
                floor=RELEASE_REGULARITY_MIN_RELEASES,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "window_months": 24,
                    "n_intervals": len(gaps_days),
                    "mean_days_between_releases": mean_days,
                },
            )
        )
    return rows


def _time_since_last_release(
    dates: list[date],
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> list[dict]:
    if not dates:
        return []
    last_release = max(dates)
    days_since = (as_of - last_release).days
    return [
        _make_row(
            metric_id="time_since_last_release",
            window_start=as_of,
            window_end=as_of,
            raw_value=float(days_since),
            n=1,
            # No population floor (METRICS.md §6: "Population & exclusions:
            # none.") -- this is a single fact, not a rate/ratio/latency
            # statistic over a sample.
            floor=0,
            run_id=run_id,
            computed_at=computed_at,
            details={"last_release_date": last_release.isoformat()},
        )
    ]


def compute_release_cadence(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> list[dict]:
    """All four issue #135 release-cadence metrics' `metric_value` row
    dicts, in one call -- `metrics.engine.compute_all`'s single integration
    point for this module."""
    dates = _all_release_dates(con)
    rows: list[dict] = []
    rows.extend(_release_frequency(dates, as_of, run_id, computed_at))
    rows.extend(_release_regularity(dates, as_of, run_id, computed_at))
    rows.extend(_days_between_releases(dates, as_of, run_id, computed_at))
    rows.extend(_time_since_last_release(dates, as_of, run_id, computed_at))
    return rows
