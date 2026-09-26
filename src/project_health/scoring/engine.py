"""Scoring orchestration entry point (issue #57): turns one run's
`metric_value` snapshot into the three persisted scoring tables
(`metric_baseline_status`, `dimension_status`, `composite_score`).

This module is the single place that bridges `metrics/engine.py`'s output
(the `metric_value` contract) to `scoring/baseline.py`, `dimension.py` and
`composite.py`'s pure math -- those three stay dependency-free of pyarrow/
storage concerns, this module does the pyarrow/table-building and (for the
two snapshot-style metrics below) the cross-run history assembly.

Called additively from `pipeline.run_pipeline`, mirroring exactly how
`leaderboard.build_leaderboards` is wired in (its own module docstring) --
see that call site for why this stays a small, additive step rather than
threading scoring concerns through the M0 metrics computation itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.metrics.windows import month_start
from project_health.schema import get_schema, validate
from project_health.scoring.baseline import BaselineStatusResult, compute_baseline_status
from project_health.scoring.composite import (
    DimensionScoreResult,
    compute_composite,
    compute_dimension_score,
)
from project_health.scoring.config import ScoringConfig
from project_health.scoring.dimension import DimensionStatusResult, compute_dimension_status
from project_health.scoring.registry import METRIC_SCORING_META, PHASE_1_DIMENSIONS, key_metrics_for

# `stale_jira_rate`/`stale_pr_rate` each emit exactly one snapshot row per RUN
# (`window_start == window_end == as_of`), not a dense monthly series within a
# single run's `metrics.parquet` (`metrics/engine.py`'s own module docstring:
# "history for this metric accumulates only from nightly snapshots going
# forward"). Both are `key` metrics (`stale_jira_rate`, for responsiveness),
# so unlike every other scored metric, their monthly baseline history is
# assembled across this project's own accumulated `snapshots/*/metrics.
# parquet` files -- one representative value per completed month (the value
# from the latest-`computed_at` snapshot observed within that month) -- a
# documented, disclosed convention (see `_monthly_series_from_snapshot_
# history`), not silently only ever seeing a single-point "history."
SNAPSHOT_STYLE_METRICS = frozenset({"stale_jira_rate", "stale_pr_rate"})

# `pmc_joins_quarterly`'s window is a calendar quarter, not a calendar month
# (`metrics/engine.py` `_pmc_joins_quarterly`) -- SCORING.md's baseline model
# (§4.1's "trailing 24 completed months") is defined in monthly terms and
# does not apply to it as-is. It is a `supporting`, not `key`, metric for its
# dimension, so it is excluded from baseline-status scoring entirely rather
# than silently mis-scored against monthly arithmetic that doesn't match its
# own window.
EXCLUDED_METRIC_IDS = frozenset({"pmc_joins_quarterly"})


@dataclass(frozen=True)
class ScoringOutput:
    metric_baseline_status: pa.Table
    dimension_status: pa.Table
    composite_score: pa.Table


def _monthly_series_from_table(table: pa.Table, metric_id: str) -> dict[date, float]:
    out: dict[date, float] = {}
    for row in table.to_pylist():
        if row["metric_id"] != metric_id or row["flag"] != "ok" or row["value"] is None:
            continue
        out[month_start(row["window_end"])] = row["value"]
    return out


def _latest_period(table: pa.Table, metric_id: str) -> date | None:
    """The most recent period this run's `metric_value` table has a row for
    `metric_id`, *whatever its flag* -- the "current" period SCORING.md §5.1
    classifies. `None` if the metric has no row at all this run."""
    months = [
        month_start(row["window_end"])
        for row in table.to_pylist()
        if row["metric_id"] == metric_id
    ]
    return max(months, default=None)


def _insufficient_current_period(metric_id: str, window_end: date) -> BaselineStatusResult:
    """SCORING.md §5.1 rule 1: the current period itself is below its
    sample-size floor (its row is `insufficient_data`), so the metric's
    status is `insufficient_data` -- never silently re-labelled with an older
    period's value, however recent the last `ok` period happens to be."""
    return BaselineStatusResult(
        metric_id=metric_id,
        window_end=window_end,
        current_value=None,
        baseline_median=None,
        baseline_mad=None,
        baseline_months=0,
        modified_z=None,
        status="insufficient_data",
        confirmed=None,
        notable_single_month_event=None,
    )


def _monthly_series_from_snapshot_history(data_dir: Path, metric_id: str) -> dict[date, float]:
    """Every completed month's representative value for `metric_id` across
    every run's `snapshots/<run_id>/metrics.parquet` under `data_dir`, for a
    metric that only ever emits a single as-of-today snapshot row per run
    (see `SNAPSHOT_STYLE_METRICS`). Where more than one run's snapshot falls
    in the same calendar month, the run with the latest `computed_at` wins --
    the most recent nightly snapshot within a month is that month's most
    complete picture of "currently open/stale."
    """
    snapshots_dir = Path(data_dir) / "snapshots"
    if not snapshots_dir.is_dir():
        return {}
    by_month: dict[date, tuple[datetime, float]] = {}
    for run_dir in sorted(snapshots_dir.iterdir()):
        path = run_dir / "metrics.parquet"
        if not path.is_file():
            continue
        table = pq.read_table(path)
        for row in table.to_pylist():
            if row["metric_id"] != metric_id or row["flag"] != "ok" or row["value"] is None:
                continue
            month = month_start(row["window_end"])
            computed_at = row["computed_at"]
            existing = by_month.get(month)
            if existing is None or computed_at >= existing[0]:
                by_month[month] = (computed_at, row["value"])
    return {month: value for month, (_computed_at, value) in by_month.items()}


def compute_scoring(
    *,
    data_dir: str | Path,
    run_id: str,
    current_metrics_table: pa.Table,
    config: ScoringConfig,
    as_of: date,
    computed_at: datetime,
) -> ScoringOutput:
    """Compute this run's baseline statuses, dimension statuses and composite
    score from `current_metrics_table` (this run's full `metric_value`
    snapshot -- dense monthly history per metric, `metrics/engine.py`) plus,
    for `SNAPSHOT_STYLE_METRICS` only, this run's accumulated history under
    `data_dir/snapshots/`.
    """
    data_dir = Path(data_dir)
    metric_ids_present = set(current_metrics_table.column("metric_id").to_pylist())

    metric_status_rows: list[dict] = []
    metric_results: dict[str, BaselineStatusResult] = {}
    for metric_id, meta in METRIC_SCORING_META.items():
        if meta.phase != 1 or metric_id in EXCLUDED_METRIC_IDS:
            continue
        if metric_id in SNAPSHOT_STYLE_METRICS:
            monthly_values = _monthly_series_from_snapshot_history(data_dir, metric_id)
        elif metric_id in metric_ids_present:
            monthly_values = _monthly_series_from_table(current_metrics_table, metric_id)
        else:
            continue  # not yet implemented (e.g. release_frequency) -- no row this run

        latest_period = _latest_period(current_metrics_table, metric_id)
        latest_ok = max(monthly_values, default=None)
        if latest_period is not None and (latest_ok is None or latest_ok < latest_period):
            # The current period is below its floor (or every period is):
            # insufficient_data, never a stale older period's status (e.g.
            # dev@ metrics mid-backfill, whose last `ok` month can be years
            # behind the run's latest completed month).
            result = _insufficient_current_period(metric_id, latest_period)
        else:
            result = compute_baseline_status(
                metric_id, meta.direction_of_good, monthly_values, config.baseline
            )
        if result is None:
            continue
        metric_results[metric_id] = result
        metric_status_rows.append(
            {
                "metric_id": metric_id,
                "dimension": meta.dimension,
                "role": meta.role,
                "direction_of_good": meta.direction_of_good,
                "window_end": result.window_end,
                "current_value": result.current_value,
                "baseline_median": result.baseline_median,
                "baseline_mad": result.baseline_mad,
                "baseline_months": result.baseline_months,
                "modified_z": result.modified_z,
                "status": result.status,
                "confirmed": result.confirmed,
                "notable_single_month_event": result.notable_single_month_event,
                "scoring_version": config.scoring_version,
                "run_id": run_id,
                "computed_at": computed_at,
            }
        )

    window_end_for_dimensions = max((r.window_end for r in metric_results.values()), default=as_of)

    dimension_status_rows: list[dict] = []
    dimension_statuses: dict[str, DimensionStatusResult] = {}
    dimension_scores: dict[str, DimensionScoreResult] = {}
    for dimension in PHASE_1_DIMENSIONS:
        key_ids = key_metrics_for(dimension)
        key_results = {mid: metric_results[mid] for mid in key_ids if mid in metric_results}
        status_result = compute_dimension_status(dimension, key_results)
        dimension_statuses[dimension] = status_result

        direction_by_metric = {mid: METRIC_SCORING_META[mid].direction_of_good for mid in key_ids}
        score_result = compute_dimension_score(
            dimension, key_results, direction_by_metric, config.composite
        )
        # Disclose against the dimension's *full* key roster (SCORING.md §5.3),
        # including key metrics with no row this run (not yet implemented, or
        # no data) -- "0 of 2 key metrics scored", never a misleading "0 of 0".
        score_result = replace(score_result, key_metrics_total=len(key_ids))
        dimension_scores[dimension] = score_result

        dimension_status_rows.append(
            {
                "dimension": dimension,
                "window_end": window_end_for_dimensions,
                "status": status_result.status,
                "driven_by": ",".join(status_result.driven_by) if status_result.driven_by else None,
                "score": score_result.score,
                "key_metrics_scored": score_result.key_metrics_scored,
                "key_metrics_total": score_result.key_metrics_total,
                "scoring_version": config.scoring_version,
                "run_id": run_id,
                "computed_at": computed_at,
            }
        )

    composite_result = compute_composite(dimension_scores, dimension_statuses, config.composite)
    composite_row = {
        "window_end": window_end_for_dimensions,
        "composite": composite_result.composite,
        "dimensions_included": composite_result.dimensions_included,
        "dimensions_total": composite_result.dimensions_total,
        "has_declining_dimension": composite_result.has_declining_dimension,
        "dimensions_json": json.dumps(
            [
                {
                    "dimension": entry.dimension,
                    "weight": entry.weight,
                    "renormalized_weight": entry.renormalized_weight,
                    "score": entry.score,
                    "status": entry.status,
                    "included": entry.included,
                    "key_metrics_scored": entry.key_metrics_scored,
                    "key_metrics_total": entry.key_metrics_total,
                }
                for entry in composite_result.breakdown
            ],
            sort_keys=True,
        ),
        "scoring_version": config.scoring_version,
        "run_id": run_id,
        "computed_at": computed_at,
    }

    metric_status_table = validate(
        "metric_baseline_status",
        pa.Table.from_pylist(metric_status_rows, schema=get_schema("metric_baseline_status")),
    )
    dimension_status_table = validate(
        "dimension_status",
        pa.Table.from_pylist(dimension_status_rows, schema=get_schema("dimension_status")),
    )
    composite_table = validate(
        "composite_score",
        pa.Table.from_pylist([composite_row], schema=get_schema("composite_score")),
    )

    return ScoringOutput(
        metric_baseline_status=metric_status_table,
        dimension_status=dimension_status_table,
        composite_score=composite_table,
    )
