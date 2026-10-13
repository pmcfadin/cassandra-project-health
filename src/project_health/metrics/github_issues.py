"""GitHub Issues metrics (issue #174, epic #168 6a): `time_to_first_response_
gh_issue` and `issue_closure_ratio_gh`, for every project with a
`github_issues:` config (`projects/<id>.yaml`'s `GitHubIssuesConfig`, issue
#169) -- easy-stress (its ONLY tracker), gocql-driver/ccm (alongside JIRA).

Reuses `metrics.engine`'s private helpers (`_dense_months`, `_make_row`,
`_percentile`, the floor constants) rather than duplicating them, same
import pattern `metrics/dev_metrics.py` already uses -- these two metrics
are dense monthly latency/rate-ratio statistics exactly like that module's
own `time_to_first_response_jira`/`change_request_closure_ratio_pr`.

- `time_to_first_response_gh_issue` -- median/P90 **hours** (not days, see
  below) from `gh_issue.created_at` to the first `gh_issue_comment`
  authored by someone other than the issue's own author, for issues opened
  in each completed calendar month -- same "opened in window" framing, same
  low-n/`insufficient_data` floor behavior, as
  `dev_metrics._time_to_first_response_jira`. Reported in hours (issue
  #174's own spec) rather than this project's usual days unit: GitHub
  Issues on a GitHub-Issues-tracked repo (easy-stress, gocql-driver, ccm)
  typically get a first response within hours, not days, and a days-scale
  number would round away exactly the signal this metric exists to show
  (`value_kind="hours"`, `site/metrics_meta.py`).
- `issue_closure_ratio_gh` -- closed/opened ratio over GitHub issues, same
  `closed(m) / opened(m)` *shape* `change_request_closure_ratio_{pr,jira_
  patch}` uses (METRICS.md §4 / DECISIONS.md D29), but **not** that CHAOSS
  metric's own issue-tracker analogue (fixup, orchestrator review of PR
  #184): CHAOSS's "Change Request Closure Ratio" is specifically about
  change requests -- pull requests / submitted patches -- and a GitHub
  issue is never a change request, so this metric is never shown on that
  CHAOSS Starter Project Health Home card (only on the Community page's
  Responsiveness section). It's cited instead as adapted from CHAOSS's own
  "Issues Closed" metric (`metrics/registry.py`'s own description has the
  verified citation detail). Count of GitHub issues *closed* in a completed
  calendar month, divided by the count of GitHub issues *opened* (created)
  in that same month. Dense monthly, `n` is the opened count -- a month
  with zero opened issues is `insufficient_data`, never a false zero.

## Why no `bot_identifier` join is needed here

`collectors/github_issues.py` already excludes bot-login issues/comments at
COLLECTION time (its own `_is_bot_login`, applied against `bot_patterns`
entries with `field: github_login`) -- the same convention
`collectors/github.py` uses for `pr`/`pr_review`/`pr_comment` -- so
`gh_issue`/`gh_issue_comment` rows reaching this module are already clean.
This is unlike JIRA's `issue_comment` (filtered at metrics-compute time via
`bot_identifier`), because this is a GitHub-sourced table following
GitHub's own collection-time filtering convention, not JIRA's.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime

import duckdb

from project_health.metrics.engine import (
    FLOOR_LATENCY,
    FLOOR_RATE_RATIO,
    _dense_months,
    _make_row,
    _percentile,
)
from project_health.metrics.windows import month_end


def _time_to_first_response_gh_issue(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Median/P90 **hours** from `gh_issue.created_at` to the first
    `gh_issue_comment` authored by someone other than the issue's own
    author (`gh_issue.author_raw_value`), for issues opened in each
    completed calendar month -- same "opened in window" framing as
    `dev_metrics._time_to_first_response_jira`, with the matching
    `closed_in_window` cross-check in `details_json` (bucketed by the
    qualifying comment's own month instead).

    `details_json` also carries `n_opened_in_window` (every issue opened
    that month, for context) vs. `n` (the subset with a computable first
    response as of this run). Bot exclusion already happened at collection
    time (module docstring) -- no `bot_identifier` join here.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', created_at)::DATE AS m FROM gh_issue"
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    opened_counts = dict(
        con.execute(
            "SELECT date_trunc('month', created_at)::DATE AS m, COUNT(*) FROM gh_issue GROUP BY 1"
        ).fetchall()
    )

    latency_rows = con.execute(
        """
        WITH first_response AS (
            SELECT c.repo, c.issue_number, MIN(c.created_at) AS first_response_at
            FROM gh_issue_comment c
            JOIN gh_issue i ON i.repo = c.repo AND i.number = c.issue_number
            WHERE c.author_raw_value IS NOT NULL
              AND (i.author_raw_value IS NULL OR c.author_raw_value <> i.author_raw_value)
            GROUP BY 1, 2
        )
        SELECT
            date_trunc('month', i.created_at)::DATE AS opened_month,
            date_trunc('month', fr.first_response_at)::DATE AS responded_month,
            epoch(fr.first_response_at) - epoch(i.created_at) AS latency_seconds
        FROM gh_issue i
        JOIN first_response fr ON fr.repo = i.repo AND fr.issue_number = i.number
        """
    ).fetchall()

    by_opened_month: dict[date, list[float]] = {}
    by_responded_month: dict[date, list[float]] = {}
    for opened_month, responded_month, latency_seconds in latency_rows:
        hours = latency_seconds / 3600.0
        by_opened_month.setdefault(opened_month, []).append(hours)
        by_responded_month.setdefault(responded_month, []).append(hours)

    out = []
    for month in _dense_months(first_month, as_of):
        latencies = by_opened_month.get(month, [])
        n = len(latencies)
        median_hours = statistics.median(latencies) if latencies else None
        p90_hours = _percentile(latencies, 0.90) if latencies else None

        closed_latencies = by_responded_month.get(month, [])
        closed_window = {
            "n": len(closed_latencies),
            "median_hours": statistics.median(closed_latencies) if closed_latencies else None,
            "p90_hours": _percentile(closed_latencies, 0.90) if closed_latencies else None,
        }

        out.append(
            _make_row(
                metric_id="time_to_first_response_gh_issue",
                window_start=month,
                window_end=month_end(month),
                raw_value=median_hours,
                n=n,
                floor=FLOOR_LATENCY,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "p90_hours": p90_hours,
                    "n_opened_in_window": opened_counts.get(month, 0),
                    "closed_in_window": closed_window,
                },
            )
        )
    return out


def _issue_closure_ratio_gh(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """GitHub-Issues closed/opened ratio, adapted from CHAOSS's "Issues
    Closed" metric -- NOT "Change Request Closure Ratio" (fixup,
    orchestrator review of PR #184: a GitHub issue is never a change
    request, see module docstring). For each completed calendar month, the
    count of issues *closed* in that month divided by the count of issues
    *opened* (created) in that same month. Dense monthly; `n` is the opened
    count -- a month with zero opened issues has an undefined ratio and is
    reported as `insufficient_data`, never a false zero. Not bounded at
    1.0, same "a one-time backlog cleanup can push this above 1" caveat
    `dev_metrics._change_request_closure_ratio_pr` documents for its own
    GitHub-PR series.
    """
    opened_rows = con.execute(
        "SELECT date_trunc('month', created_at)::DATE AS m, COUNT(*) FROM gh_issue GROUP BY 1"
    ).fetchall()
    if not opened_rows:
        return []
    opened_by_month: dict[date, int] = dict(opened_rows)
    closed_by_month: dict[date, int] = dict(
        con.execute(
            "SELECT date_trunc('month', closed_at)::DATE AS m, COUNT(*) FROM gh_issue "
            "WHERE closed_at IS NOT NULL GROUP BY 1"
        ).fetchall()
    )

    out = []
    for month in _dense_months(min(opened_by_month), as_of):
        n_opened = opened_by_month.get(month, 0)
        n_closed = closed_by_month.get(month, 0)
        raw_value = (n_closed / n_opened) if n_opened else None
        out.append(
            _make_row(
                metric_id="issue_closure_ratio_gh",
                window_start=month,
                window_end=month_end(month),
                raw_value=raw_value,
                n=n_opened,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={"n_opened": n_opened, "n_closed": n_closed},
            )
        )
    return out


def compute_github_issue_metrics(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Both issue #174 metrics' `metric_value` row dicts, in one call --
    `metrics.engine.compute_all`'s single integration point for this
    module."""
    rows: list[dict] = []
    rows.extend(_time_to_first_response_gh_issue(con, as_of, run_id, computed_at))
    rows.extend(_issue_closure_ratio_gh(con, as_of, run_id, computed_at))
    return rows
