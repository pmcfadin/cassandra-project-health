"""Community page "Open PR backlog" section context builder (issue #142).

Orchestrator review of PR #143 (reviewer feedback: the site is too
verbose): the 12 `open_pr_backlog_*` metric_ids stay computed and
downloadable as `data/<id>.json`/`.csv` (`generate.py` still runs them
through `_build_series`/the per-metric download loop), but they no longer
render as 12 separate cards in the Community page's Responsiveness
dimension grid (`generate._render_pages` excludes
`metrics_meta.PR_BACKLOG_METRICS`' ids from that card grid specifically).
Instead, this module builds the section's own compact rendering: one
stacked chart of the `PRIMARY_REPO` (apache/cassandra) backlog by age
bucket over the last 36 months, one smaller stacked chart by
linked-ticket state, a one-row current-month table (total, drafts,
no-GitHub-response share), a single "Based on" line, and a single
sentence disclosing that base branch isn't collected.

## Second orchestrator review: scoped to apache/cassandra, plus one more table

The charts/table/headline above are apache/cassandra-only (`metrics/
pr_backlog.py`'s own `PRIMARY_REPO` scoping -- the issue's own verified
549-open-PR snapshot is itself apache/cassandra-only, and the
linked-ticket-state bucket is meaningless for a repo tracked by a
different JIRA project, or none). This module also builds one more
table, "Other project repositories" (`_other_repos_table`): each other
configured repo's own current total/drafts/age-bucket counts (no
ticket-state column, for the reason above), read from the same
`pr_backlog_metric_value.parquet` snapshot's `other_repo_metric_id` rows
-- discovered from whichever repos the data actually has (`details_json.
repo`), not a hardcoded list.

D25 (neutral, informational site): every string here avoids verdict/
pass-fail/threshold/"healthy"/"should"/"cleanup" wording -- counts and a
share only, framed as "what the record shows."

An honest no-data state (no snapshot for this run, or the computation
produced zero rows) still returns a valid context with `available=False`.
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
    DRAFTS,
    NO_GITHUB_RESPONSE_SHARE,
    NO_TICKET_KEY,
    PRIMARY_REPO,
    TICKET_CLOSED_OTHER,
    TICKET_FIXED,
    TICKET_OPEN,
    TOTAL,
    other_repo_metric_id,
)
from project_health.schema import get_schema, validate
from project_health.site import chart_spec
from project_health.site.metrics_meta import PR_BACKLOG_PRIOR_ART

METRICS_SPEC_URL = (
    "https://github.com/pmcfadin/cassandra-project-health/blob/main/docs/spec/METRICS.md"
    "#12-open-pr-backlog-descriptive-only-not-in-the-summary-table"
)

BASE_BRANCH_NOTE = (
    "Base branch (trunk vs. release branches) is not collected yet, so it is not shown here."
)

CHART_WINDOW_MONTHS = 36

AGE_BUCKET_ORDER: tuple[tuple[str, str], ...] = (
    (AGE_LT_30D, "<30d"),
    (AGE_30_90D, "30-90d"),
    (AGE_90D_1Y, "90d-1y"),
    (AGE_1_3Y, "1-3y"),
    (AGE_GT_3Y, ">3y"),
)

TICKET_STATE_ORDER: tuple[tuple[str, str], ...] = (
    (TICKET_OPEN, "Still open"),
    (TICKET_FIXED, "Fixed"),
    (TICKET_CLOSED_OTHER, "Closed (other)"),
    (NO_TICKET_KEY, "No ticket key"),
)


def _read_optional_snapshot_table(data_dir: Path, run_id: str) -> pa.Table:
    path = Path(data_dir) / "snapshots" / run_id / "pr_backlog_metric_value.parquet"
    if not path.is_file():
        return get_schema("metric_value").empty_table()
    return validate("metric_value", pq.read_table(path))


def _details(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row or not row.get("details_json"):
        return {}
    return json.loads(row["details_json"])


def _fmt_pct(value: float | None) -> str | None:
    return f"{round(value * 100)}%" if value is not None else None


def _stacked_chart_spec(
    rows_by_metric: dict[str, list[dict]],
    bucket_order: tuple[tuple[str, str], ...],
    *,
    height: int,
) -> str | None:
    """A small, self-contained multi-series Vega-Lite stacked-bar spec --
    same "no existing chart here encodes more than one series, so this is
    its own spec" reasoning `review_responsiveness_page.py`'s own
    `_trailing12m_chart_spec` documents, restricted to the last
    `CHART_WINDOW_MONTHS` (36, matching `chart_spec.DEFAULT_WINDOW_MONTHS`)
    months of data."""
    all_months = sorted(
        {
            row["window_start"]
            for metric_id, _ in bucket_order
            for row in rows_by_metric.get(metric_id, [])
        }
    )
    if not all_months:
        return None
    recent_months = all_months[-CHART_WINDOW_MONTHS:]

    values: list[dict[str, Any]] = []
    for metric_id, label in bucket_order:
        rows_by_month = {r["window_start"]: r for r in rows_by_metric.get(metric_id, [])}
        for month in recent_months:
            row = rows_by_month.get(month)
            count = row["value"] if row and row["value"] is not None else 0
            values.append({"month": month.isoformat(), "bucket": label, "count": count})

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": height,
        "data": {"values": values},
        "mark": "bar",
        "encoding": {
            "x": {"field": "month", "type": "temporal", "title": None},
            "y": {"field": "count", "type": "quantitative", "title": "Open PRs", "stack": "zero"},
            "color": {
                "field": "bucket",
                "type": "nominal",
                "title": None,
                "sort": [label for _, label in bucket_order],
            },
            "tooltip": [
                {"field": "month", "type": "temporal", "title": "Month"},
                {"field": "bucket", "type": "nominal", "title": "Bucket"},
                {"field": "count", "type": "quantitative", "title": "Count"},
            ],
        },
    }
    return json.dumps(spec)


AGE_BUCKET_LABELS: tuple[str, ...] = tuple(label for _, label in AGE_BUCKET_ORDER)


def _other_repos_table(rows: list[dict], rows_by_metric: dict[str, list[dict]]) -> list[dict]:
    """One row per non-`PRIMARY_REPO` repo found in the snapshot (via
    `details_json.repo`, not a hardcoded repo list) -- current total,
    drafts, and age-bucket counts, no ticket-state column (see module
    docstring)."""
    repos: set[str] = set()
    for row in rows:
        repo = _details(row).get("repo")
        if repo and repo != PRIMARY_REPO:
            repos.add(repo)

    table_rows: list[dict[str, Any]] = []
    for repo in sorted(repos):
        total_rows = sorted(
            rows_by_metric.get(other_repo_metric_id(TOTAL, repo), []),
            key=lambda r: r["window_start"],
        )
        if not total_rows:
            continue
        latest = total_rows[-1]
        latest_month = latest["window_start"]
        total = int(latest["value"]) if latest["value"] is not None else None

        drafts_by_month = {
            r["window_start"]: r for r in rows_by_metric.get(other_repo_metric_id(DRAFTS, repo), [])
        }
        drafts_row = drafts_by_month.get(latest_month)
        drafts = (
            int(drafts_row["value"]) if drafts_row and drafts_row["value"] is not None else None
        )

        age_cells: list[int | None] = []
        for metric_id, _ in AGE_BUCKET_ORDER:
            bucket_by_month = {
                r["window_start"]: r
                for r in rows_by_metric.get(other_repo_metric_id(metric_id, repo), [])
            }
            bucket_row = bucket_by_month.get(latest_month)
            age_cells.append(
                int(bucket_row["value"]) if bucket_row and bucket_row["value"] is not None else None
            )

        table_rows.append(
            {
                "repo": repo.split("/")[-1],
                "as_of_month": latest["window_end"],
                "total": total,
                "drafts": drafts,
                "age_cells": age_cells,
            }
        )
    return table_rows


def build_pr_backlog_context(data_dir: str | Path, run_id: str) -> dict[str, Any]:
    data_dir = Path(data_dir)
    table = _read_optional_snapshot_table(data_dir, run_id)
    rows = table.to_pylist()

    if not rows:
        return {
            "available": False,
            "metrics_spec_url": METRICS_SPEC_URL,
            "base_branch_note": BASE_BRANCH_NOTE,
            "prior_art": PR_BACKLOG_PRIOR_ART,
        }

    rows_by_metric: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_metric.setdefault(row["metric_id"], []).append(row)

    total_rows = sorted(rows_by_metric.get(TOTAL, []), key=lambda r: r["window_start"])
    if not total_rows:
        return {
            "available": False,
            "metrics_spec_url": METRICS_SPEC_URL,
            "base_branch_note": BASE_BRANCH_NOTE,
            "prior_art": PR_BACKLOG_PRIOR_ART,
        }

    latest = total_rows[-1]
    latest_month = latest["window_start"]
    as_of_month = latest["window_end"]
    total = int(latest["value"]) if latest["value"] is not None else 0

    drafts_by_month = {r["window_start"]: r for r in rows_by_metric.get(DRAFTS, [])}
    drafts_row = drafts_by_month.get(latest_month)
    drafts = int(drafts_row["value"]) if drafts_row and drafts_row["value"] is not None else None

    no_resp_by_month = {
        r["window_start"]: r for r in rows_by_metric.get(NO_GITHUB_RESPONSE_SHARE, [])
    }
    no_resp_row = no_resp_by_month.get(latest_month)
    no_response_share = (
        _fmt_pct(no_resp_row["value"])
        if no_resp_row and no_resp_row["value"] is not None
        else None
    )
    no_response_n = _details(no_resp_row).get("n_denominator")

    return {
        "available": True,
        "primary_repo": PRIMARY_REPO,
        "as_of_month": as_of_month,
        "total": total,
        "drafts": drafts,
        "no_response_share": no_response_share,
        "no_response_n": no_response_n,
        "age_chart_spec": _stacked_chart_spec(rows_by_metric, AGE_BUCKET_ORDER, height=240),
        "age_chart_id": "pr-backlog-age",
        "age_chart_meta_json": chart_spec.chart_meta_json(
            chart_id="pr-backlog-age",
            title="Open PR backlog by age",
            time_field="month",
            time_type="month",
            series_field="bucket",
        ),
        "ticket_chart_spec": _stacked_chart_spec(rows_by_metric, TICKET_STATE_ORDER, height=160),
        "ticket_chart_id": "pr-backlog-ticket",
        "ticket_chart_meta_json": chart_spec.chart_meta_json(
            chart_id="pr-backlog-ticket",
            title="Open PR backlog by linked-ticket state",
            time_field="month",
            time_type="month",
            series_field="bucket",
        ),
        "other_repos": _other_repos_table(rows, rows_by_metric),
        "age_bucket_labels": AGE_BUCKET_LABELS,
        "prior_art": PR_BACKLOG_PRIOR_ART,
        "base_branch_note": BASE_BRANCH_NOTE,
        "metrics_spec_url": METRICS_SPEC_URL,
    }
