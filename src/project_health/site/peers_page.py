"""`/peers/` page context builder (issue #145, DECISIONS.md D30).

Reads the latest `snapshots/peers/<run_id>/<project_id>/{metric_value,
pr_backlog}.parquet` files (written by `peers.pipeline.run_peers_collection`)
for Cassandra and every configured peer, and builds one multi-line chart
per metric (Cassandra's own line visually distinguished, every project
named in the legend/tooltip -- issue #145's own spec) plus a current-month
table of all six projects' latest values.

D30 (no targets, no ranking): every string here is a plain fact ("current
value," "as of month X") -- never "ahead of," "behind," "healthier than,"
or any comparative verdict language. The CHAOSS disclaimer
(`CHAOSS_DISCLAIMER`) is rendered once, prominently, at the top of the page.

No snapshot yet -> `available=False`, same "no snapshot -> no page" gate
`site/generate.py`'s other optional pages (thread explorer, conversation
patterns) already use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.metrics.pr_backlog import (
    AGE_1_3Y,
    AGE_30_90D,
    AGE_90D_1Y,
    AGE_GT_3Y,
    AGE_LT_30D,
    TOTAL,
    other_repo_metric_id,
)
from project_health.metrics.peer_metrics import TIME_TO_FIRST_RESPONSE_PR
from project_health.peers.pipeline import CASSANDRA_PROJECT_ID, latest_peers_run_id

CHAOSS_DISCLAIMER = (
    "CHAOSS does not set targets or rank projects. These numbers are shown for context only "
    "-- Cassandra and five comparable Apache Software Foundation projects, chosen by the "
    "project owner, computed with the same code and the same definitions."
)

METRICS_SPEC_URL = (
    "https://github.com/pmcfadin/cassandra-project-health/blob/main/docs/spec/METRICS.md"
    "#13-peer-context-chaoss-starter-metrics-across-comparable-asf-projects-issue-145"
    "-decisionsmd-d30"
)

AGE_BUCKET_ORDER: tuple[tuple[str, str], ...] = (
    (AGE_LT_30D, "<30d"),
    (AGE_30_90D, "30-90d"),
    (AGE_90D_1Y, "90d-1y"),
    (AGE_1_3Y, "1-3y"),
    (AGE_GT_3Y, ">3y"),
)

# Project display order -- Cassandra always first (D30: Cassandra's own
# line is highlighted, never ranked against the rest).
_PEER_ORDER: tuple[tuple[str, str], ...] = (
    (CASSANDRA_PROJECT_ID, "Apache Cassandra"),
    ("kafka", "Apache Kafka"),
    ("spark", "Apache Spark"),
    ("flink", "Apache Flink"),
    ("pulsar", "Apache Pulsar"),
    ("datafusion", "Apache DataFusion"),
)

METRIC_ORDER: tuple[tuple[str, str], ...] = (
    (TIME_TO_FIRST_RESPONSE_PR, "Time to First Response (GitHub PRs)"),
    ("change_request_closure_ratio_pr", "Change Request Closure Ratio"),
    ("contributor_absence_factor", "Contributor Absence Factor"),
    ("release_frequency", "Release Frequency (trailing 24 months)"),
)


def _read_optional_table(path: Path, schema_table_name: str) -> pa.Table:
    from project_health.schema import get_schema, validate

    if not path.is_file():
        return get_schema(schema_table_name).empty_table()
    return validate(schema_table_name, pq.read_table(path))


def _details(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("details_json"):
        return {}
    return json.loads(row["details_json"])


def _backlog_total_rows(pr_backlog_rows: list[dict], project_id: str) -> list[dict]:
    """`open_pr_backlog_total`-equivalent rows for `project_id`.

    Cassandra's own filtered comparison tables carry `repo ==
    pr_backlog.PRIMARY_REPO`, so `compute_pr_backlog` emits the plain,
    unsuffixed ids for it; every peer repo is never `PRIMARY_REPO`, so it
    emits `other_repo_metric_id(..., repo)`-suffixed ids instead (module
    docstring of `metrics/pr_backlog.py`) -- this helper picks the right
    metric_id family per project rather than assuming one shape."""
    if project_id == CASSANDRA_PROJECT_ID:
        total_id, age_ids = TOTAL, dict(AGE_BUCKET_ORDER)
    else:
        repo = f"apache/{project_id}"
        total_id = other_repo_metric_id(TOTAL, repo)
        age_ids = {other_repo_metric_id(mid, repo): label for mid, label in AGE_BUCKET_ORDER}
    return [r for r in pr_backlog_rows if r["metric_id"] in {total_id, *age_ids}], total_id, age_ids


def _line_chart_spec(
    metric_id: str, series_by_project: dict[str, list[dict]], *, value_label: str
) -> str | None:
    """One multi-line Vega-Lite spec per metric -- every project its own
    line, Cassandra's own styled distinctly (bold stroke) rather than
    colour alone, so it reads in both light and dark/greyscale contexts.
    Every project named in the legend and the tooltip (issue #145's own
    spec: "each peer named in legend/tooltip")."""
    values: list[dict[str, Any]] = []
    for project_id, display_name in _PEER_ORDER:
        for row in series_by_project.get(project_id, []):
            if row["metric_id"] != metric_id or row["value"] is None:
                continue
            values.append(
                {
                    "project": display_name,
                    "month": row["window_start"].isoformat(),
                    "value": row["value"],
                    "n": row["n"],
                    "is_cassandra": project_id == CASSANDRA_PROJECT_ID,
                }
            )
    if not values:
        return None

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 220,
        "data": {"values": values},
        "layer": [
            {
                "mark": {"type": "line", "interpolate": "linear"},
                "encoding": {
                    "x": {"field": "month", "type": "temporal", "title": None},
                    "y": {"field": "value", "type": "quantitative", "title": value_label},
                    "color": {"field": "project", "type": "nominal", "title": None},
                    "strokeWidth": {
                        "field": "is_cassandra",
                        "type": "nominal",
                        "legend": None,
                        "scale": {"domain": [False, True], "range": [1.5, 3.5]},
                    },
                    "tooltip": [
                        {"field": "project", "type": "nominal", "title": "Project"},
                        {"field": "month", "type": "temporal", "title": "Month"},
                        {"field": "value", "type": "quantitative", "title": value_label},
                        {"field": "n", "type": "quantitative", "title": "n"},
                    ],
                },
            }
        ],
    }
    return json.dumps(spec)


def build_peers_context(data_dir: str | Path, run_id: str | None = None) -> dict[str, Any]:
    """Build `/peers/` page context. `run_id` defaults to the latest
    `snapshots/peers/<run_id>/` directory (`latest_peers_run_id`)."""
    data_dir = Path(data_dir)
    run_id = run_id or latest_peers_run_id(data_dir)
    if run_id is None:
        return {"available": False, "chaoss_disclaimer": CHAOSS_DISCLAIMER}

    metric_rows_by_project: dict[str, list[dict]] = {}
    backlog_rows_by_project: dict[str, list[dict]] = {}
    for project_id, _ in _PEER_ORDER:
        snapshot_dir = data_dir / "snapshots" / "peers" / run_id / project_id
        metric_rows_by_project[project_id] = _read_optional_table(
            snapshot_dir / "metric_value.parquet", "metric_value"
        ).to_pylist()
        # `pr_backlog.parquet` is also `metric_value`-shaped output
        # (`compute_pr_backlog`'s own return type) -- a separate file, same
        # schema, per `peers.pipeline._write_snapshot`.
        backlog_rows_by_project[project_id] = _read_optional_table(
            snapshot_dir / "pr_backlog.parquet", "metric_value"
        ).to_pylist()

    if not any(metric_rows_by_project.values()):
        return {"available": False, "chaoss_disclaimer": CHAOSS_DISCLAIMER}

    charts = []
    for metric_id, label in METRIC_ORDER:
        charts.append(
            {
                "metric_id": metric_id,
                "label": label,
                "chart_spec": _line_chart_spec(
                    metric_id, metric_rows_by_project, value_label=label
                ),
            }
        )

    # Open PR backlog (total) chart -- its own metric_id per project (see
    # `_backlog_total_rows`), normalized to one shared metric_id
    # ("open_pr_backlog_total") here purely so `_line_chart_spec` -- which
    # keys its per-row filter on a single literal metric_id -- can treat
    # every project's rows the same way despite their differing raw ids.
    backlog_series: dict[str, list[dict]] = {}
    for project_id, _ in _PEER_ORDER:
        rows, total_id, _age_ids = _backlog_total_rows(
            backlog_rows_by_project.get(project_id, []), project_id
        )
        backlog_series[project_id] = [
            {**r, "metric_id": "open_pr_backlog_total"}
            for r in rows
            if r["metric_id"] == total_id
        ]
    charts.append(
        {
            "metric_id": "open_pr_backlog_total",
            "label": "Open PR backlog (total)",
            "chart_spec": _line_chart_spec(
                "open_pr_backlog_total", backlog_series, value_label="Open PRs"
            ),
        }
    )

    # Current-month table: one row per project, one column per metric.
    table_rows = []
    for project_id, display_name in _PEER_ORDER:
        rows_by_metric: dict[str, list[dict]] = {}
        for row in metric_rows_by_project.get(project_id, []):
            rows_by_metric.setdefault(row["metric_id"], []).append(row)

        cells = []
        as_of_month = None
        for metric_id, _label in METRIC_ORDER:
            series = sorted(rows_by_metric.get(metric_id, []), key=lambda r: r["window_start"])
            latest = series[-1] if series else None
            if latest and latest["value"] is not None:
                cells.append(round(latest["value"], 2))
                as_of_month = latest["window_end"]
            else:
                cells.append(None)

        backlog_rows, backlog_total_id, _age_ids = _backlog_total_rows(
            backlog_rows_by_project.get(project_id, []), project_id
        )
        backlog_series_sorted = sorted(
            (r for r in backlog_rows if r["metric_id"] == backlog_total_id),
            key=lambda r: r["window_start"],
        )
        backlog_latest = backlog_series_sorted[-1] if backlog_series_sorted else None
        backlog_value = None
        if backlog_latest and backlog_latest["value"] is not None:
            backlog_value = int(backlog_latest["value"])
        cells.append(backlog_value)
        if backlog_latest:
            as_of_month = as_of_month or backlog_latest["window_end"]

        table_rows.append(
            {
                "project_id": project_id,
                "display_name": display_name,
                "is_cassandra": project_id == CASSANDRA_PROJECT_ID,
                "as_of_month": as_of_month,
                "cells": cells,
            }
        )

    table_columns = [label for _, label in METRIC_ORDER] + ["Open PR backlog (total)"]

    return {
        "available": True,
        # Named distinctly from the main pipeline's own `run_id` (already in
        # `common_ctx`, every page's shared render context) -- this is the
        # separate, weekly `peers.yml` run_id this snapshot came from.
        "peers_run_id": run_id,
        "chaoss_disclaimer": CHAOSS_DISCLAIMER,
        "metrics_spec_url": METRICS_SPEC_URL,
        "charts": charts,
        "table_columns": table_columns,
        "table_rows": table_rows,
    }
