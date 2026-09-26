"""Integration tests for `scoring/engine.py` (issue #57): bridges a real
`metric_value`-shaped pyarrow table (the actual contract `metrics/engine.py`
produces) into the three scoring output tables, including the cross-run
history assembly for `SNAPSHOT_STYLE_METRICS` (`stale_jira_rate`/
`stale_pr_rate`, which only ever emit one as-of-today row per run) and the
`EXCLUDED_METRIC_IDS` (`pmc_joins_quarterly`, whose window is quarterly, not
monthly).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.metrics.windows import add_months, month_end
from project_health.schema import get_schema, validate
from project_health.scoring.config import load_scoring_config
from project_health.scoring.engine import compute_scoring

COMPUTED_AT = datetime(2025, 1, 15, tzinfo=timezone.utc)
CONFIG = load_scoring_config()


def _dense_monthly_rows(metric_id: str, first_month: date, values: list[float]) -> list[dict]:
    rows = []
    for i, value in enumerate(values):
        month = add_months(first_month, i)
        rows.append(
            {
                "metric_id": metric_id,
                "definition_version": "1.0",
                "window_start": month,
                "window_end": month_end(month),
                "value": value,
                "n": 10,
                "flag": "ok",
                "run_id": "run-1",
                "computed_at": COMPUTED_AT,
                "details_json": None,
            }
        )
    return rows


def _metrics_table(rows: list[dict]) -> pa.Table:
    return validate("metric_value", pa.Table.from_pylist(rows, schema=get_schema("metric_value")))


def test_compute_scoring_end_to_end_shapes_and_excludes_quarterly_metric(tmp_path):
    """A minimal but real `metric_value` table covering reviewer capacity's
    two key metrics (`unique_reviewers_monthly`, `reviewer_hhi`) plus
    `pmc_joins_quarterly` (which must never appear in the output --
    quarterly cadence, `EXCLUDED_METRIC_IDS`)."""
    first_month = date(2023, 1, 1)
    baseline_values = [float(10 + i) for i in range(23)]
    tail = [*baseline_values, 100.0, 100.0]
    rows = []
    rows += _dense_monthly_rows("unique_reviewers_monthly", first_month, tail)
    rows += _dense_monthly_rows("reviewer_hhi", first_month, tail)
    rows.append(
        {
            "metric_id": "pmc_joins_quarterly",
            "definition_version": "1.0",
            "window_start": date(2024, 10, 1),
            "window_end": date(2024, 12, 31),
            "value": 1.0,
            "n": 1,
            "flag": "ok",
            "run_id": "run-1",
            "computed_at": COMPUTED_AT,
            "details_json": None,
        }
    )
    table = _metrics_table(rows)

    result = compute_scoring(
        data_dir=tmp_path,
        run_id="run-1",
        current_metrics_table=table,
        config=CONFIG,
        as_of=date(2025, 1, 15),
        computed_at=COMPUTED_AT,
    )

    metric_ids = set(result.metric_baseline_status.column("metric_id").to_pylist())
    assert "pmc_joins_quarterly" not in metric_ids
    assert {"unique_reviewers_monthly", "reviewer_hhi"} <= metric_ids

    reviewer_capacity_row = next(
        row
        for row in result.dimension_status.to_pylist()
        if row["dimension"] == "reviewer capacity"
    )
    # `unique_reviewers_monthly` (higher) confirmed-improving, `reviewer_hhi`
    # (lower) confirmed-declining -> worst-key-metric rule -> declining.
    assert reviewer_capacity_row["status"] == "declining"
    assert reviewer_capacity_row["driven_by"] == "reviewer_hhi"

    # release cadence has no implemented key metric at all yet
    # (`release_frequency` has no collector) -- always insufficient_data.
    release_row = next(
        row for row in result.dimension_status.to_pylist() if row["dimension"] == "release cadence"
    )
    assert release_row["status"] == "insufficient_data"

    composite_row = result.composite_score.to_pylist()[0]
    assert composite_row["scoring_version"] == CONFIG.scoring_version
    assert composite_row["dimensions_total"] == 5
    assert composite_row["run_id"] == "run-1"


def test_snapshot_style_metric_history_is_assembled_across_runs(tmp_path):
    """`stale_jira_rate` only ever emits one as-of-today snapshot row per
    run (`window_start == window_end == as_of`) -- its 24-month baseline
    must come from `data_dir/snapshots/*/metrics.parquet` across many past
    runs, one representative (latest-`computed_at`) value per month."""
    snapshots_dir = tmp_path / "snapshots"
    first_month = date(2023, 1, 1)

    # 23 prior runs, one per month, each a lone snapshot row.
    for i in range(23):
        month = add_months(first_month, i)
        run_id = f"run-hist-{i}"
        run_dir = snapshots_dir / run_id
        run_dir.mkdir(parents=True)
        row = {
            "metric_id": "stale_jira_rate",
            "definition_version": "1.0",
            "window_start": month,
            "window_end": month,
            "value": float(10 + i),
            "n": 20,
            "flag": "ok",
            "run_id": run_id,
            "computed_at": datetime(month.year, month.month, 15, tzinfo=timezone.utc),
            "details_json": None,
        }
        pq.write_table(_metrics_table([row]), run_dir / "metrics.parquet")

    # T-1 and the current run both deviate hugely (2-of-3 confirmation).
    t_minus_1_month = add_months(first_month, 23)
    current_month = add_months(first_month, 24)
    for i, month in enumerate((t_minus_1_month, current_month)):
        run_id = f"run-current-{i}"
        run_dir = snapshots_dir / run_id
        run_dir.mkdir(parents=True)
        row = {
            "metric_id": "stale_jira_rate",
            "definition_version": "1.0",
            "window_start": month,
            "window_end": month,
            "value": 100.0,
            "n": 20,
            "flag": "ok",
            "run_id": run_id,
            "computed_at": datetime(month.year, month.month, 15, tzinfo=timezone.utc),
            "details_json": None,
        }
        pq.write_table(_metrics_table([row]), run_dir / "metrics.parquet")

    current_computed_at = datetime(current_month.year, current_month.month, 15, tzinfo=timezone.utc)
    current_table = _metrics_table(
        [
            {
                "metric_id": "stale_jira_rate",
                "definition_version": "1.0",
                "window_start": current_month,
                "window_end": current_month,
                "value": 100.0,
                "n": 20,
                "flag": "ok",
                "run_id": "run-current-1",
                "computed_at": current_computed_at,
                "details_json": None,
            }
        ]
    )

    result = compute_scoring(
        data_dir=tmp_path,
        run_id="run-current-1",
        current_metrics_table=current_table,
        config=CONFIG,
        as_of=date(current_month.year, current_month.month, 20),
        computed_at=COMPUTED_AT,
    )
    row = next(
        r for r in result.metric_baseline_status.to_pylist() if r["metric_id"] == "stale_jira_rate"
    )
    assert row["baseline_months"] == 24
    assert row["status"] == "declining"
    assert row["confirmed"] is True


def test_current_period_below_floor_is_insufficient_not_a_stale_older_status(tmp_path):
    """SCORING.md §5.1 rule 1: when a metric's latest period is itself
    `insufficient_data` (e.g. dev@ metrics mid-backfill, whose last `ok`
    month can be years old), the metric is `insufficient_data` *at that
    latest period* -- never classified from an older `ok` month."""
    rows = _dense_monthly_rows(
        "time_to_first_reply_devlist", date(2015, 1, 1), [float(10 + i) for i in range(25)]
    )
    latest = date(2026, 8, 1)
    rows.append(
        {
            "metric_id": "time_to_first_reply_devlist",
            "definition_version": "1.0",
            "window_start": latest,
            "window_end": month_end(latest),
            "value": None,
            "n": 2,
            "flag": "insufficient_data",
            "run_id": "run-1",
            "computed_at": COMPUTED_AT,
            "details_json": None,
        }
    )
    result = compute_scoring(
        data_dir=tmp_path,
        run_id="run-1",
        current_metrics_table=_metrics_table(rows),
        config=CONFIG,
        as_of=date(2026, 9, 15),
        computed_at=COMPUTED_AT,
    )
    row = next(
        r
        for r in result.metric_baseline_status.to_pylist()
        if r["metric_id"] == "time_to_first_reply_devlist"
    )
    assert row["window_end"] == latest
    assert row["status"] == "insufficient_data"
    assert row["modified_z"] is None


def test_key_metrics_total_discloses_the_full_key_roster(tmp_path):
    """A dimension whose key metrics produced no row at all still discloses
    its full key-metric roster ("0 of 1"), never a misleading "0 of 0"."""
    rows = _dense_monthly_rows("truck_factor", date(2023, 1, 1), [5.0] * 25)
    result = compute_scoring(
        data_dir=tmp_path,
        run_id="run-1",
        current_metrics_table=_metrics_table(rows),
        config=CONFIG,
        as_of=date(2025, 3, 15),
        computed_at=COMPUTED_AT,
    )
    by_dim = {r["dimension"]: r for r in result.dimension_status.to_pylist()}
    assert by_dim["release cadence"]["key_metrics_scored"] == 0
    assert by_dim["release cadence"]["key_metrics_total"] == 1
    assert by_dim["contributor sustainability"]["key_metrics_scored"] == 1
    assert by_dim["contributor sustainability"]["key_metrics_total"] == 3
