"""Community page "Open PR backlog" section context builder (issue #142).

The per-bucket counts (`metrics_meta.PR_BACKLOG_METRICS`) already render as
ordinary cards in the Community page's existing Responsiveness group
(`generate.py`'s standard dimension-grouped grid) -- this module builds the
section's own **compact current-snapshot table** (the latest completed
month's age/ticket-state/no-response figures side by side, rather than
scattered across a dozen separate cards) plus the disclosed "base branch
split isn't available" note the issue's own real-data check asked for.

D25 (neutral, informational site): every string here avoids verdict/
pass-fail/threshold/"healthy"/"should"/"cleanup" wording -- counts and a
share only, framed as "what the record shows."

An honest no-data state (no snapshot for this run, or the computation
produced zero rows) returns a valid context with `available=False`,
matching every other optional section on this site.
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
    TICKET_CLOSED_OTHER,
    TICKET_FIXED,
    TICKET_OPEN,
    TOTAL,
)
from project_health.schema import get_schema, validate

METRICS_SPEC_URL = (
    "https://github.com/pmcfadin/cassandra-project-health/blob/main/docs/spec/METRICS.md"
    "#12-open-pr-backlog-descriptive-only-not-in-the-summary-table"
)

BASE_BRANCH_NOTE = (
    "Base branch split (trunk vs. release branches vs. other) is not shown: this project's "
    "GitHub collector does not currently record a PR's base branch, so it cannot be "
    "reconstructed from existing raw data."
)

AGE_BUCKET_LABELS: tuple[tuple[str, str], ...] = (
    (AGE_LT_30D, "<30 days"),
    (AGE_30_90D, "30-90 days"),
    (AGE_90D_1Y, "90 days-1 year"),
    (AGE_1_3Y, "1-3 years"),
    (AGE_GT_3Y, ">3 years"),
)

TICKET_STATE_LABELS: tuple[tuple[str, str], ...] = (
    (TICKET_OPEN, "Linked ticket still open"),
    (NO_TICKET_KEY, "No ticket key in title"),
    (TICKET_FIXED, "Linked ticket Fixed"),
    (TICKET_CLOSED_OTHER, "Linked ticket closed (other)"),
)


def _read_optional_snapshot_table(data_dir: Path, run_id: str) -> pa.Table:
    path = Path(data_dir) / "snapshots" / run_id / "pr_backlog_metric_value.parquet"
    if not path.is_file():
        return get_schema("metric_value").empty_table()
    return validate("metric_value", pq.read_table(path))


def _details(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["details_json"]) if row.get("details_json") else {}


def _fmt_pct(value: float | None) -> str | None:
    return f"{round(value * 100)}%" if value is not None else None


def build_pr_backlog_context(data_dir: str | Path, run_id: str) -> dict[str, Any]:
    data_dir = Path(data_dir)
    table = _read_optional_snapshot_table(data_dir, run_id)
    rows = table.to_pylist()

    if not rows:
        return {
            "available": False,
            "metrics_spec_url": METRICS_SPEC_URL,
            "base_branch_note": BASE_BRANCH_NOTE,
        }

    rows_by_metric: dict[str, dict] = {}
    for row in rows:
        existing = rows_by_metric.get(row["metric_id"])
        if existing is None or row["window_start"] > existing["window_start"]:
            rows_by_metric[row["metric_id"]] = row

    total_row = rows_by_metric.get(TOTAL)
    if total_row is None:
        return {
            "available": False,
            "metrics_spec_url": METRICS_SPEC_URL,
            "base_branch_note": BASE_BRANCH_NOTE,
        }

    as_of_month = total_row["window_end"]
    total = int(total_row["value"]) if total_row["value"] is not None else 0
    drafts_row = rows_by_metric.get(DRAFTS)
    drafts = int(drafts_row["value"]) if drafts_row and drafts_row["value"] is not None else None

    age_rows = [
        {
            "label": label,
            "count": int(rows_by_metric[metric_id]["value"])
            if metric_id in rows_by_metric and rows_by_metric[metric_id]["value"] is not None
            else None,
        }
        for metric_id, label in AGE_BUCKET_LABELS
    ]
    ticket_rows = [
        {
            "label": label,
            "count": int(rows_by_metric[metric_id]["value"])
            if metric_id in rows_by_metric and rows_by_metric[metric_id]["value"] is not None
            else None,
        }
        for metric_id, label in TICKET_STATE_LABELS
    ]

    no_response_row = rows_by_metric.get(NO_GITHUB_RESPONSE_SHARE)
    no_response_share = (
        _fmt_pct(no_response_row["value"])
        if no_response_row and no_response_row["value"] is not None
        else None
    )
    no_response_n = _details(no_response_row).get("n_denominator") if no_response_row else None

    return {
        "available": True,
        "as_of_month": as_of_month,
        "total": total,
        "drafts": drafts,
        "age_rows": age_rows,
        "ticket_rows": ticket_rows,
        "no_response_share": no_response_share,
        "no_response_n": no_response_n,
        "metrics_spec_url": METRICS_SPEC_URL,
        "base_branch_note": BASE_BRANCH_NOTE,
    }
