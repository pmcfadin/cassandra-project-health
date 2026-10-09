"""Golden tests for project_health.metrics.release_cadence (issue #135).

Follows `test_metrics_engine.py`'s own golden-test convention (hand-built
normalized-table rows, exact `metric_value` assertions on value/n/flag/
details_json) but lives in its own file to stay isolated from parallel
work on that shared module/its builders.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pytest

from project_health.config import load_project
from project_health.metrics.engine import compute_all
from project_health.schema import get_schema, validate

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_project(REPO_ROOT / "projects" / "cassandra.yaml")

RUN_ID = "run-test-release-1"
COMPUTED_AT = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
# Jan 2022 .. Aug 2023 are completed months; September 2023 is the current,
# excluded month.
AS_OF = date(2023, 9, 15)


def _rows_for(table: pa.Table, metric_id: str) -> list[dict]:
    rows = table.to_pylist()
    return sorted(
        (r for r in rows if r["metric_id"] == metric_id),
        key=lambda r: r["window_start"],
    )


def _row_for_window_end(table: pa.Table, metric_id: str, window_end: date) -> dict:
    matches = [r for r in _rows_for(table, metric_id) if r["window_end"] == window_end]
    assert len(matches) == 1, f"expected exactly one {metric_id} row ending {window_end}"
    return matches[0]


def _details(row: dict) -> dict:
    return json.loads(row["details_json"]) if row["details_json"] else {}


def _releases(rows: list[dict]) -> pa.Table:
    schema = get_schema("release")
    full_rows = [
        {
            "release_id": row["tag_name"],
            "tag_name": row["tag_name"],
            "version": row["version"],
            "major_minor": ".".join(row["version"].split(".")[:2]),
            "release_date": row["release_date"],
            "release_date_source": "git_tag",
            "archive_verified": True,
            "archive_date": row["release_date"],
            "repo": "apache/cassandra",
            "source_snapshot_id": "snap-1",
            "collected_at": datetime(2023, 9, 15, tzinfo=UTC),
        }
        for row in rows
    ]
    return validate("release", pa.Table.from_pylist(full_rows, schema=schema))


# Five GA releases spanning 2022-01 .. 2023-07.
RELEASE_ROWS = [
    {"tag_name": "cassandra-1.0.0", "version": "1.0.0", "release_date": date(2022, 1, 10)},
    {"tag_name": "cassandra-1.1.0", "version": "1.1.0", "release_date": date(2022, 7, 5)},
    {"tag_name": "cassandra-1.2.0", "version": "1.2.0", "release_date": date(2023, 1, 20)},
    {"tag_name": "cassandra-1.2.1", "version": "1.2.1", "release_date": date(2023, 2, 15)},
    {"tag_name": "cassandra-1.3.0", "version": "1.3.0", "release_date": date(2023, 7, 1)},
]


def _compute() -> pa.Table:
    return compute_all(
        {"release": _releases(RELEASE_ROWS)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )


class TestReleaseFrequency:
    def test_counts_releases_in_trailing_24m_window(self):
        result = _compute()
        # Window ending Jan 2023: [2021-02-01, 2023-01-31] -- includes
        # 1.0.0, 1.1.0, 1.2.0 (1.2.1/1.3.0 are later than the window end).
        row = _row_for_window_end(result, "release_frequency", date(2023, 1, 31))
        assert row["window_start"] == date(2021, 2, 1)
        assert row["value"] == 3.0
        assert row["n"] == 3
        assert row["flag"] == "ok"

    def test_last_completed_month_includes_every_release_so_far(self):
        result = _compute()
        # Window ending Aug 2023 (the last completed month): all five
        # releases fall inside [2021-09-01, 2023-08-31].
        row = _row_for_window_end(result, "release_frequency", date(2023, 8, 31))
        assert row["window_start"] == date(2021, 9, 1)
        assert row["value"] == 5.0
        assert row["n"] == 5
        assert row["flag"] == "ok"

    def test_zero_releases_in_window_is_still_ok_not_insufficient_data(self):
        result = _compute()
        # The very first dense month (Jan 2022, the month of the earliest
        # release) still has a real, computable count -- never
        # insufficient_data merely for a small/zero n (module docstring:
        # "a raw count is meaningful at any value, including 0").
        row = _row_for_window_end(result, "release_frequency", date(2022, 1, 31))
        assert row["flag"] == "ok"
        assert row["value"] == 1.0

    def test_dense_across_every_completed_month(self):
        result = _compute()
        rows = _rows_for(result, "release_frequency")
        window_ends = {r["window_end"] for r in rows}
        # Jan 2022 through Aug 2023, inclusive: 20 months, no gaps.
        assert len(window_ends) == 20
        for row in rows:
            assert row["flag"] == "ok"


class TestReleaseRegularity:
    def test_below_floor_is_insufficient_data(self):
        result = _compute()
        # Window ending Jul 2022: only 1.0.0 and 1.1.0 have happened
        # (n=2 releases, 1 interval) -- below the floor of 3.
        row = _row_for_window_end(result, "release_regularity", date(2022, 7, 31))
        assert row["n"] == 2
        assert row["flag"] == "insufficient_data"
        assert row["value"] is None

    def test_at_floor_computes_cov_of_inter_release_gaps(self):
        result = _compute()
        # Window ending Jan 2023: three releases (1.0.0, 1.1.0, 1.2.0),
        # exactly at the floor of 3.
        row = _row_for_window_end(result, "release_regularity", date(2023, 1, 31))
        gaps = [
            (date(2022, 7, 5) - date(2022, 1, 10)).days,
            (date(2023, 1, 20) - date(2022, 7, 5)).days,
        ]
        expected_mean = statistics.mean(gaps)
        expected_cov = statistics.stdev(gaps) / expected_mean
        assert row["n"] == 3
        assert row["flag"] == "ok"
        assert row["value"] == pytest.approx(expected_cov)
        details = _details(row)
        assert details["n_intervals"] == 2
        assert details["mean_days_between_releases"] == pytest.approx(expected_mean)
        assert details["median_days_between_releases"] == pytest.approx(
            statistics.median(gaps)
        )

    def test_last_completed_month_all_five_releases(self):
        result = _compute()
        row = _row_for_window_end(result, "release_regularity", date(2023, 8, 31))
        gaps = [
            (date(2022, 7, 5) - date(2022, 1, 10)).days,
            (date(2023, 1, 20) - date(2022, 7, 5)).days,
            (date(2023, 2, 15) - date(2023, 1, 20)).days,
            (date(2023, 7, 1) - date(2023, 2, 15)).days,
        ]
        expected_cov = statistics.stdev(gaps) / statistics.mean(gaps)
        assert row["n"] == 5
        assert row["flag"] == "ok"
        assert row["value"] == pytest.approx(expected_cov)
        assert _details(row)["n_intervals"] == 4

    def test_direction_of_good_none_still_computes(self):
        """`release_regularity`'s `none` direction is a scoring-layer
        concern (scoring/registry.py), not a reason for the engine to skip
        computing or emitting a real value/flag here."""
        result = _compute()
        rows = _rows_for(result, "release_regularity")
        assert len(rows) == 20


class TestDaysBetweenReleases:
    """Issue #135's own "companion days_between_releases (median gap)" --
    published as its own metric (D29 follow-up), not just
    release_regularity's details_json."""

    def test_below_floor_is_insufficient_data(self):
        result = _compute()
        row = _row_for_window_end(result, "days_between_releases", date(2022, 7, 31))
        assert row["n"] == 2
        assert row["flag"] == "insufficient_data"
        assert row["value"] is None

    def test_at_floor_reports_median_gap_in_days(self):
        result = _compute()
        row = _row_for_window_end(result, "days_between_releases", date(2023, 1, 31))
        gaps = [
            (date(2022, 7, 5) - date(2022, 1, 10)).days,
            (date(2023, 1, 20) - date(2022, 7, 5)).days,
        ]
        assert row["n"] == 3
        assert row["flag"] == "ok"
        assert row["value"] == pytest.approx(statistics.median(gaps))
        details = _details(row)
        assert details["n_intervals"] == 2
        assert details["mean_days_between_releases"] == pytest.approx(statistics.mean(gaps))

    def test_matches_release_regularitys_own_median_days_detail(self):
        """Both metrics compute the median over the exact same gap
        population for the same window -- they must never disagree."""
        result = _compute()
        regularity_row = _row_for_window_end(result, "release_regularity", date(2023, 8, 31))
        companion_row = _row_for_window_end(result, "days_between_releases", date(2023, 8, 31))
        assert companion_row["value"] == pytest.approx(
            _details(regularity_row)["median_days_between_releases"]
        )

    def test_dense_across_every_completed_month(self):
        result = _compute()
        assert len(_rows_for(result, "days_between_releases")) == 20


class TestTimeSinceLastRelease:
    def test_single_snapshot_row_as_of_run_date(self):
        result = _compute()
        rows = _rows_for(result, "time_since_last_release")
        assert len(rows) == 1
        row = rows[0]
        assert row["window_start"] == AS_OF
        assert row["window_end"] == AS_OF
        expected_days = (AS_OF - date(2023, 7, 1)).days
        assert row["value"] == pytest.approx(float(expected_days))
        assert row["flag"] == "ok"
        assert _details(row)["last_release_date"] == "2023-07-01"

    def test_no_row_when_no_releases_collected_yet(self):
        result = compute_all(
            {},
            as_of=AS_OF,
            run_id=RUN_ID,
            computed_at=COMPUTED_AT,
            config=CONFIG,
        )
        assert _rows_for(result, "time_since_last_release") == []
        assert _rows_for(result, "release_frequency") == []
        assert _rows_for(result, "release_regularity") == []
        assert _rows_for(result, "days_between_releases") == []


class TestEmptyReleaseTable:
    def test_compute_all_never_fails_with_no_release_table_passed(self):
        """`release` is an optional key in `compute_all`'s `tables` dict
        (missing key -> empty table of the declared schema, same contract
        every other M0 table follows) -- a caller that hasn't wired the
        `release` source yet must never crash metric computation."""
        result = compute_all(
            {},
            as_of=AS_OF,
            run_id=RUN_ID,
            computed_at=COMPUTED_AT,
            config=CONFIG,
        )
        assert isinstance(result, pa.Table)
