"""Peer-context metrics (issue #145, DECISIONS.md D30).

Computes the same five comparable metrics for Cassandra *and* each peer,
from whichever single project's raw tables are registered -- **reused,
unmodified private helpers**, not new formulas, so a reader can trust
"same metrics, same code" literally:

1. `time_to_first_response_pr` -- new in this module (`_time_to_first_
   response_pr`): first non-author, non-bot GitHub-PR review *or* comment,
   median days. The only genuinely new metric here -- no existing metric
   unions `pr_review` and `pr_comment` this way (`dev_metrics.
   _pr_time_to_first_review` is reviews-only, the metric already wired to
   Cassandra's own landing-page "Time to First Response" card per D29; see
   this function's own docstring for why that one is deliberately left
   alone rather than changed to match).
2. `change_request_closure_ratio_pr` -- `dev_metrics._change_request_
   closure_ratio_pr`, imported and called as-is.
3. `contributor_absence_factor` -- `engine._contributor_absence_factor`,
   imported and called as-is (needs `resolved_identity`/`bot_identifier`
   registered the same way `engine._connect`/`compute_all` already do).
4. `release_frequency` (+ `release_regularity`/`days_between_releases`/
   `time_since_last_release`, incidentally) -- `release_cadence.
   compute_release_cadence`, imported and called as-is.
5. Open PR backlog by age bucket -- `pr_backlog.compute_pr_backlog`,
   imported and called as-is: since no peer repo is `pr_backlog.
   PRIMARY_REPO` ("apache/cassandra"), every peer's rows fall out of that
   module's existing `_other_repo_rows` path (`other_repo_metric_id(TOTAL/
   DRAFTS/<age bucket>, repo)`) with zero changes needed there either.

Each call computes **one project at a time** -- `tables` must contain only
that one project's rows (Cassandra's own `pr`/`pr_review`/.../`release`
tables, filtered to its primary repo, for the "Cassandra's line" series; or
one peer's `raw/peers/<id>/...` tables). `metrics.engine.compute_all`'s own
per-run isolation (a fresh in-memory DuckDB connection, nothing persisted
across calls) is what makes this safe to call six times in a row with six
different `tables` dicts and never cross-contaminate one project's numbers
with another's.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime

import duckdb
import pyarrow as pa

from project_health.metrics.dev_metrics import _change_request_closure_ratio_pr
from project_health.metrics.engine import (
    _BOT_IDENTIFIER_SCHEMA,
    FLOOR_LATENCY,
    _connect,
    _contributor_absence_factor,
    _dense_months,
    _make_row,
    _percentile,
)
from project_health.metrics.pr_backlog import compute_pr_backlog
from project_health.metrics.release_cadence import compute_release_cadence
from project_health.metrics.windows import month_end
from project_health.normalize.identity import extract_raw_identifiers, resolve_identities
from project_health.schema import get_schema, validate

TIME_TO_FIRST_RESPONSE_PR = "time_to_first_response_pr"
DEFINITION_VERSION = "1.0"

# Self-exclusion for a comment, mirrors `dev_metrics._SELF_REVIEW_JOIN_SQL`'s
# review-side join but compares the raw login strings directly (no
# `resolved_identity` dependency) -- adequate for a brand-new metric with no
# prior "which side is identity-linked" history to preserve; documented here
# rather than silently assumed identical to `dev_metrics`'s own fixup.
_FIRST_RESPONSE_SQL = """
    WITH responses AS (
        SELECT
            p.repo, p.number, p.created_at AS pr_created_at,
            r.submitted_at AS response_at
        FROM pr p
        JOIN pr_review r ON r.repo = p.repo AND r.pr_number = p.number
        WHERE r.submitted_at IS NOT NULL
          AND r.reviewer_raw_value IS NOT NULL
          AND r.reviewer_raw_value != p.author_raw_value
        UNION ALL
        SELECT
            p.repo, p.number, p.created_at AS pr_created_at,
            c.created_at AS response_at
        FROM pr p
        JOIN pr_comment c ON c.repo = p.repo AND c.pr_number = p.number
        WHERE c.created_at IS NOT NULL
          AND c.author_raw_value IS NOT NULL
          AND c.author_raw_value != p.author_raw_value
    ),
    first_response AS (
        SELECT repo, number, pr_created_at, MIN(response_at) AS first_response_at
        FROM responses
        GROUP BY 1, 2, 3
    )
    SELECT
        date_trunc('month', pr_created_at)::DATE AS month_start,
        epoch(first_response_at) - epoch(pr_created_at) AS latency_seconds
    FROM first_response
"""


def _time_to_first_response_pr(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Median/P90 days from a PR's `created_at` to the earliest non-author
    `pr_review.submitted_at` or `pr_comment.created_at` (whichever is
    first), bucketed by the PR's creation month. See module docstring for
    why this is a new metric rather than a reuse of `dev_metrics.
    _pr_time_to_first_review`."""
    rows = con.execute(_FIRST_RESPONSE_SQL).fetchall()
    by_month: dict[date, list[float]] = {}
    for month_start_, latency_seconds in rows:
        by_month.setdefault(month_start_, []).append(latency_seconds / 86400.0)
    if not by_month:
        return []

    out = []
    for month in _dense_months(min(by_month), as_of):
        latencies = by_month.get(month, [])
        n = len(latencies)
        median_days = statistics.median(latencies) if latencies else None
        p90_days = _percentile(latencies, 0.90) if latencies else None
        out.append(
            _make_row(
                metric_id=TIME_TO_FIRST_RESPONSE_PR,
                window_start=month,
                window_end=month_end(month),
                raw_value=median_days,
                n=n,
                floor=FLOOR_LATENCY,
                run_id=run_id,
                computed_at=computed_at,
                details={"p90_days": p90_days, "n": n},
            )
        )
    return out


def build_identity_link(contribution_event: pa.Table, *, now: datetime) -> pa.Table:
    """Naive (email-exact-match) `identity_link` table for one project's
    `contribution_event` rows (`normalize.identity`, unmodified) -- no
    manual overrides (peers have none), fed straight into
    `compute_peer_metrics` so `contributor_absence_factor`'s
    `resolved_identity` join has something to match against. Pure/stateless
    exactly like Cassandra's own per-run identity resolution
    (`pipeline.py`); never persisted, recomputed every call."""
    raw_identifiers = extract_raw_identifiers(contribution_events=contribution_event)
    resolution = resolve_identities(raw_identifiers, [], now=now)
    return resolution.identity_link


def compute_peer_metrics(
    tables: dict[str, pa.Table],
    *,
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> dict[str, pa.Table]:
    """The five issue #145 metrics for one project's `tables`
    (`contribution_event`, `pr`, `pr_review`, `pr_comment`, `release`;
    `identity_link` is derived here from `contribution_event` if not
    already present -- see `build_identity_link`).

    Returns `{"metric_value": <time_to_first_response_pr +
    change_request_closure_ratio_pr + contributor_absence_factor +
    release_cadence's own four rows>, "pr_backlog": <compute_pr_backlog's
    own output, unfiltered>}`. Both are already-validated `metric_value`
    tables; callers (`peers.pipeline`) write each to its own snapshot path.
    """
    tables = dict(tables)
    if "identity_link" not in tables or tables["identity_link"].num_rows == 0:
        tables["identity_link"] = build_identity_link(
            tables.get("contribution_event", get_schema("contribution_event").empty_table()),
            now=computed_at,
        )

    con = _connect(tables)
    try:
        con.register("bot_identifier", _BOT_IDENTIFIER_SCHEMA.empty_table())
        rows: list[dict] = []
        rows.extend(_time_to_first_response_pr(con, as_of, run_id, computed_at))
        rows.extend(_change_request_closure_ratio_pr(con, as_of, run_id, computed_at))
        rows.extend(_contributor_absence_factor(con, as_of, run_id, computed_at))
        rows.extend(compute_release_cadence(con, as_of, run_id, computed_at))
    finally:
        con.close()

    schema = get_schema("metric_value")
    metric_value = (
        validate("metric_value", pa.Table.from_pylist(rows, schema=schema))
        if rows
        else schema.empty_table()
    )

    pr_backlog_table = compute_pr_backlog(
        tables, as_of=as_of, run_id=run_id, computed_at=computed_at
    )

    return {"metric_value": metric_value, "pr_backlog": pr_backlog_table}
