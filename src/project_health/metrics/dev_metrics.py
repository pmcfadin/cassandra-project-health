"""GitHub-PR development metrics + `time_to_first_response_jira` (issue #54).

DECISIONS.md D21 item 2 ("Development"): merge lead time, time to first
review, time to close, review engagement, plus the JIRA responsiveness
metric LFX Insights can't compute for Cassandra (whose GitHub Issues are
disabled). Kept in its own module rather than growing `metrics/engine.py`
further -- `engine.py`'s own module docstring already covers M0 issue #7 plus
issue #53's contributor-sustainability trio, and several other in-flight
issues (#35, #52) are editing `engine.py`/`registry.py`/`pipeline.py` in
parallel, so this module's six new metrics live here and `compute_all` only
needs one import plus one `rows.extend(...)` call to wire them in.

Reuses `metrics.engine`'s private helpers (`_dense_months`, `_make_row`,
`_percentile`, the floor constants) rather than duplicating them -- these six
metrics are dense monthly/snapshot latency and rate/ratio statistics exactly
like engine.py's own M0 metrics, so the same floor/dense-month/row-shape
conventions apply unchanged (METRICS.md §0.6).

Six metrics (all definition_version "1.0", METRICS.md's own definitions
where one already existed, this module's `registry.py` description
otherwise):

- `pr_merge_lead_time` -- median/P90 days, PR opened -> merged.
- `pr_time_to_first_review` -- median/P90 days, PR opened -> first review.
- `pr_time_to_close` -- median/P90 days, PR opened -> closed (any close).
- `pr_review_engagement` -- mean unique reviewers per PR (+ reviews/PR in
  details_json), among PRs reviewed in the window.
- `time_to_first_response_jira` -- METRICS.md §4: median/P90 days, JIRA issue
  opened -> first human (non-author, non-bot) comment. The "genuinely
  missing field" this issue needed was comment metadata itself
  (`collectors/jira.py`'s new `issue_comment` table, issue #54) -- computed
  here, not in `engine.py`, purely to keep this module self-contained.
- `stale_pr_rate` -- METRICS.md §4: share of currently-open PRs with no
  update in 90+ days, one snapshot row per run (mirrors `stale_jira_rate`).

## Why raw GitHub logins, not `resolved_identity` -- except for self-review exclusion

`pipeline.py`'s `extract_raw_identifiers` (identity resolution, #6) is not
extended to `pr`/`pr_review`'s `github_login` raw identifiers by this issue.
Issue #52's `link_github_commit_authors` does add some `github_login`
`identity_link` rows, but only for logins GitHub's own commit-history API
associated with a *git commit* (`collectors/github_commit_authors.py`) -- a
PR reviewer who never authored a commit (or whose commits predate that
collector's own history walk) has no such link, so joining
`pr_review_engagement`'s reviewer counts against `resolved_identity` would
silently undercount rather than cleanly fail. This module counts distinct
`reviewer_raw_value` strings directly instead, the same "raw identifier is
the best available identity" approach `collectors/github.py` already uses
for its own bot exclusion at collection time -- disclosed here rather than
silently assumed identical to the cross-source `unique_reviewers_monthly`.
Joining PR review identities through issue #52's linkage is a reasonable
future enhancement, out of scope for this issue.

The one place this module *does* join `resolved_identity` is self-review
exclusion (`_SELF_REVIEW_JOIN_SQL`/`_EXCLUDE_SELF_REVIEW_SQL` below, fixup
after orchestrator review): GitHub records a PR author's own replies inside
review threads as `COMMENTED` reviews by that same author, which is not a
*reviewer's* review-latency/engagement signal and must never count as "the
first review" or as review-engagement credit. This is compared at the
identity level -- `resolved_identity` when a `github_login` link exists,
falling back to the raw login string otherwise (the common case, since most
logins never get an issue #52 identity_link row at all) -- rather than at
the raw-string level alone, so a PR author who *is* identity-linked (e.g. via
a different login on a different commit) is still excluded from reviewing
their own PR under that other login. An unresolvable side (a null
`author_raw_value`/`reviewer_raw_value`, e.g. a deleted account) is never
treated as matching anything, including itself -- this fixup only removes
*positively confirmed* self-reviews, never anything merely ambiguous.

## Why GitHub-side bot filtering isn't repeated here

`collectors/github.py` already excludes bot-login PRs/reviews/comments at
collection time (its own `_is_bot_login`, applied against `bot_patterns`
entries with `field: github_login`), so `pr`/`pr_review` rows reaching this
module are already clean -- no `bot_identifier` join is needed for them.
JIRA comment authors are *not* filtered at collection time (matching every
other JIRA/git raw table's convention of collecting raw and filtering at
metrics-compute time), so `time_to_first_response_jira` does join against
`bot_identifier` for `jira_username`.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

import duckdb

from project_health.metrics.engine import (
    FLOOR_LATENCY,
    FLOOR_RATE_RATIO,
    _dense_months,
    _make_row,
    _percentile,
)
from project_health.metrics.windows import month_end

DEFAULT_STALE_PR_THRESHOLD_DAYS = 90

# Self-review exclusion (orchestrator review, issue #54 fixup) -- see module
# docstring. Both fragments assume the query aliases the `pr` table `p` and
# the `pr_review` table `r`. `_SELF_REVIEW_JOIN_SQL` must appear after both
# `p` and `r` are already in scope; `_EXCLUDE_SELF_REVIEW_SQL` is a boolean
# expression for a `WHERE`/`AND` clause.
_SELF_REVIEW_JOIN_SQL = """
    LEFT JOIN resolved_identity ria
        ON ria.source_type = 'github_login' AND ria.source_value = p.author_raw_value
    LEFT JOIN resolved_identity rir
        ON rir.source_type = 'github_login' AND rir.source_value = r.reviewer_raw_value
"""

# `COALESCE(identity_id, 'login:' || raw_value)` resolves to NULL when
# raw_value itself is NULL (concatenation with NULL is NULL in SQL), so an
# unresolvable side's "IS NOT NULL" check is FALSE and the surrounding AND
# short-circuits to FALSE -- `NOT FALSE` keeps the row. Only a *positive*
# match (both sides resolve to the same non-null identity) is excluded.
_EXCLUDE_SELF_REVIEW_SQL = """
    NOT (
        COALESCE(ria.identity_id, 'login:' || p.author_raw_value) IS NOT NULL
        AND COALESCE(rir.identity_id, 'login:' || r.reviewer_raw_value) IS NOT NULL
        AND COALESCE(ria.identity_id, 'login:' || p.author_raw_value)
            = COALESCE(rir.identity_id, 'login:' || r.reviewer_raw_value)
    )
"""


def _pr_merge_lead_time(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Median/P90 days from `pr.created_at` to `pr.merged_at`, bucketed by
    merge month; dense from the first month any PR merged through the last
    completed month before `as_of`."""
    rows = con.execute(
        """
        SELECT
            date_trunc('month', merged_at)::DATE AS month_start,
            epoch(merged_at) - epoch(created_at) AS latency_seconds
        FROM pr
        WHERE merged AND merged_at IS NOT NULL
        """
    ).fetchall()
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
                metric_id="pr_merge_lead_time",
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


def _pr_time_to_first_review(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Median/P90 days from a PR's `created_at` to its earliest *non-self*
    `pr_review.submitted_at`, among PRs with at least one qualifying review,
    bucketed by the PR's creation month. Excludes reviews where the reviewer
    is the PR author (`_EXCLUDE_SELF_REVIEW_SQL`, module docstring) -- GitHub
    records an author's own replies inside review threads as `COMMENTED`
    reviews by that author, which are not a reviewer's time-to-review signal."""
    rows = con.execute(
        f"""
        WITH first_review AS (
            SELECT
                p.repo, p.number, p.created_at AS pr_created_at,
                MIN(r.submitted_at) AS first_review_at
            FROM pr p
            JOIN pr_review r ON r.repo = p.repo AND r.pr_number = p.number
            {_SELF_REVIEW_JOIN_SQL}
            WHERE r.submitted_at IS NOT NULL
              AND {_EXCLUDE_SELF_REVIEW_SQL}
            GROUP BY 1, 2, 3
        )
        SELECT
            date_trunc('month', pr_created_at)::DATE AS month_start,
            epoch(first_review_at) - epoch(pr_created_at) AS latency_seconds
        FROM first_review
        """
    ).fetchall()
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
                metric_id="pr_time_to_first_review",
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


def _pr_time_to_close(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Median/P90 days from `pr.created_at` to `pr.closed_at` (merged or
    not), bucketed by close month. Checked for the same self-activity concern
    as `_pr_time_to_first_review`/`_pr_review_engagement` (orchestrator
    review, issue #54 fixup): this metric reads only `pr.created_at`/
    `pr.closed_at`/`merged`, with no `pr_review` join and no notion of "who
    acted" at all, so a PR author's own activity cannot be miscounted as
    someone else's here -- no fix needed."""
    rows = con.execute(
        """
        SELECT
            date_trunc('month', closed_at)::DATE AS month_start,
            epoch(closed_at) - epoch(created_at) AS latency_seconds,
            merged
        FROM pr
        WHERE closed_at IS NOT NULL
        """
    ).fetchall()
    by_month: dict[date, list[float]] = {}
    merged_by_month: dict[date, int] = {}
    for month_start_, latency_seconds, merged in rows:
        by_month.setdefault(month_start_, []).append(latency_seconds / 86400.0)
        if merged:
            merged_by_month[month_start_] = merged_by_month.get(month_start_, 0) + 1
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
                metric_id="pr_time_to_close",
                window_start=month,
                window_end=month_end(month),
                raw_value=median_days,
                n=n,
                floor=FLOOR_LATENCY,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "p90_days": p90_days,
                    "n": n,
                    "n_merged": merged_by_month.get(month, 0),
                },
            )
        )
    return out


def _pr_review_engagement(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Mean unique reviewers per PR (value) and mean/median reviews per PR
    (details_json), among PRs with >=1 *non-self* review in a completed
    calendar month (bucketed by `pr_review.submitted_at`). Excludes the PR
    author's own reviews from every count here (unique reviewers per PR,
    reviews per PR, and the total unique-reviewer count) -- same
    `_EXCLUDE_SELF_REVIEW_SQL` exclusion as `_pr_time_to_first_review`."""
    per_pr_rows = con.execute(
        f"""
        SELECT
            date_trunc('month', r.submitted_at)::DATE AS month_start,
            r.repo, r.pr_number,
            COUNT(DISTINCT r.reviewer_raw_value) AS unique_reviewers,
            COUNT(*) AS review_count
        FROM pr_review r
        JOIN pr p ON p.repo = r.repo AND p.number = r.pr_number
        {_SELF_REVIEW_JOIN_SQL}
        WHERE r.submitted_at IS NOT NULL AND r.reviewer_raw_value IS NOT NULL
          AND {_EXCLUDE_SELF_REVIEW_SQL}
        GROUP BY 1, 2, 3
        """
    ).fetchall()
    if not per_pr_rows:
        return []

    by_month: dict[date, list[tuple[int, int]]] = {}
    for month_start_, _repo, _pr_number, unique_reviewers, review_count in per_pr_rows:
        by_month.setdefault(month_start_, []).append((unique_reviewers, review_count))

    total_unique_by_month = dict(
        con.execute(
            f"""
            SELECT date_trunc('month', r.submitted_at)::DATE AS month_start,
                   COUNT(DISTINCT r.reviewer_raw_value) AS n_unique
            FROM pr_review r
            JOIN pr p ON p.repo = r.repo AND p.number = r.pr_number
            {_SELF_REVIEW_JOIN_SQL}
            WHERE r.submitted_at IS NOT NULL AND r.reviewer_raw_value IS NOT NULL
              AND {_EXCLUDE_SELF_REVIEW_SQL}
            GROUP BY 1
            """
        ).fetchall()
    )

    out = []
    for month in _dense_months(min(by_month), as_of):
        per_pr = by_month.get(month, [])
        n_prs = len(per_pr)
        if per_pr:
            unique_reviewer_counts = [u for u, _r in per_pr]
            review_counts = [r for _u, r in per_pr]
            mean_unique_reviewers = statistics.mean(unique_reviewer_counts)
            mean_reviews_per_pr = statistics.mean(review_counts)
            median_reviews_per_pr = statistics.median(review_counts)
            n_reviews = sum(review_counts)
        else:
            mean_unique_reviewers = None
            mean_reviews_per_pr = None
            median_reviews_per_pr = None
            n_reviews = 0
        out.append(
            _make_row(
                metric_id="pr_review_engagement",
                window_start=month,
                window_end=month_end(month),
                raw_value=mean_unique_reviewers,
                n=n_prs,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "mean_reviews_per_pr": mean_reviews_per_pr,
                    "median_reviews_per_pr": median_reviews_per_pr,
                    "n_prs": n_prs,
                    "n_reviews": n_reviews,
                    "n_unique_reviewers_total": total_unique_by_month.get(month, 0),
                },
            )
        )
    return out


def _time_to_first_response_jira(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """METRICS.md §4: median/P90 days from `issue.created_at` to the first
    `issue_comment` authored by someone other than the issue's reporter and
    not a bot (`bot_identifier`, joined on `jira_username`), for issues
    opened in each completed calendar month ("opened in window" framing).
    `details_json.closed_in_window` carries the same statistic bucketed by
    the qualifying comment's own month instead (METRICS.md §4's "reports
    both ... side by side").

    Confirmed (orchestrator review, issue #54 fixup): this excludes the
    reporter's own comments by comparing `jc.author_raw_value` against
    `i.reporter_raw` only -- there is no separate assignee comparison, and
    none is needed. When the assignee happens to be the same person as the
    reporter (`i.assignee_raw == i.reporter_raw`), that person's
    `author_raw_value` is identical to `i.reporter_raw` for any comment they
    write, so the existing reporter check already excludes it; a distinct
    assignee (the common case) was never a "self" response to begin with and
    was never excluded, which is correct -- an assignee who isn't the
    reporter answering their own assigned issue is a genuine first response.

    Issue #79: `issue.created_at`/`.count` is always current (the `issue`
    table itself isn't gapped by the comment backfill), but a month can still
    be missing part or all of its `issue_comment` data for issues collected
    before issue #54 added comment collection -- production's JIRA watermark
    was already current when #54 shipped, so those historical issues have no
    comment rows and won't get any until `pipeline._collect_jira_comment_
    backfill`'s budgeted backfill reaches them. A month with any issue not
    yet covered by that backfill (`comment_backfill_checked`, written for
    *every* issue whose comments have ever been fetched, checked or
    unchecked -- see that table's schema docstring) gets `details_json.
    backfill_in_progress: true`, the same disclosure pattern
    `_devlist_eligible_months`'s dev@ backfill uses. This never changes
    `value`/`flag` -- a month with zero *qualifying* comment data already
    reads as `insufficient_data`/`None` under the ordinary sample floor
    (never a real zero); the flag only discloses *why* a gap might be an
    artifact of collection state, not a genuine absence of responses. An
    empty `comment_backfill_checked` table (no row for the coverage query to
    even find, e.g. a golden test that doesn't model backfill state, or
    `compute_all` called without that table) is treated as "not modeling
    backfill" and never flags any month -- mirrors `_devlist_eligible_
    months`'s `watermark_month=None` fallback for the same reason.
    """
    checked_total = con.execute("SELECT COUNT(*) FROM comment_backfill_checked").fetchone()[0]
    backfill_pending_by_month: dict[date, bool] = {}
    if checked_total > 0:
        coverage_rows = con.execute(
            """
            SELECT
                date_trunc('month', i.created_at)::DATE AS m,
                COUNT(*) AS n_total,
                COUNT(DISTINCT c.issue_key) AS n_covered
            FROM issue i
            LEFT JOIN comment_backfill_checked c ON c.issue_key = i.issue_key
            GROUP BY 1
            """
        ).fetchall()
        backfill_pending_by_month = {
            m: n_covered < n_total for m, n_total, n_covered in coverage_rows
        }

    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', created_at)::DATE AS m FROM issue"
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    opened_counts = dict(
        con.execute(
            "SELECT date_trunc('month', created_at)::DATE AS m, COUNT(*) FROM issue GROUP BY 1"
        ).fetchall()
    )

    latency_rows = con.execute(
        """
        WITH first_response AS (
            SELECT jc.issue_key, MIN(jc.created_at) AS first_response_at
            FROM issue_comment jc
            JOIN issue i ON i.issue_key = jc.issue_key
            LEFT JOIN bot_identifier bi
                ON bi.raw_type = 'jira_username' AND bi.raw_value = jc.author_raw_value
            WHERE bi.raw_value IS NULL
              AND jc.author_raw_value IS NOT NULL
              AND (i.reporter_raw IS NULL OR jc.author_raw_value <> i.reporter_raw)
            GROUP BY 1
        )
        SELECT
            date_trunc('month', i.created_at)::DATE AS opened_month,
            date_trunc('month', fr.first_response_at)::DATE AS responded_month,
            epoch(fr.first_response_at) - epoch(i.created_at) AS latency_seconds
        FROM issue i
        JOIN first_response fr ON fr.issue_key = i.issue_key
        """
    ).fetchall()

    by_opened_month: dict[date, list[float]] = {}
    by_responded_month: dict[date, list[float]] = {}
    for opened_month, responded_month, latency_seconds in latency_rows:
        days = latency_seconds / 86400.0
        by_opened_month.setdefault(opened_month, []).append(days)
        by_responded_month.setdefault(responded_month, []).append(days)

    out = []
    for month in _dense_months(first_month, as_of):
        latencies = by_opened_month.get(month, [])
        n = len(latencies)
        median_days = statistics.median(latencies) if latencies else None
        p90_days = _percentile(latencies, 0.90) if latencies else None

        closed_latencies = by_responded_month.get(month, [])
        closed_window = {
            "n": len(closed_latencies),
            "median_days": statistics.median(closed_latencies) if closed_latencies else None,
            "p90_days": _percentile(closed_latencies, 0.90) if closed_latencies else None,
        }

        details: dict = {
            "p90_days": p90_days,
            "n_opened_in_window": opened_counts.get(month, 0),
            "closed_in_window": closed_window,
        }
        if backfill_pending_by_month.get(month, False):
            # issue #79: at least one issue opened this month hasn't had its
            # comments checked yet -- see this function's docstring.
            details["backfill_in_progress"] = True

        out.append(
            _make_row(
                metric_id="time_to_first_response_jira",
                window_start=month,
                window_end=month_end(month),
                raw_value=median_days,
                n=n,
                floor=FLOOR_LATENCY,
                run_id=run_id,
                computed_at=computed_at,
                details=details,
            )
        )
    return out


def _stale_pr_rate(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    threshold_days: int,
) -> list[dict]:
    """METRICS.md §4: share of currently-open PRs (`state='OPEN'`) with no
    `pr.updated_at` change in `threshold_days`+ days, across every
    configured repo. One snapshot row per run, mirroring `stale_jira_rate`.

    Checked for the same self-activity concern as `_pr_time_to_first_review`/
    `_pr_review_engagement` (orchestrator review, issue #54 fixup): this
    metric has no `pr_review` join and no reviewer-identity comparison at
    all, only `pr.state`/`pr.updated_at` -- there is no "self-review" for a
    self-review exclusion to fix here. The metric's own `note` field already
    discloses the *related but distinct* limitation that `pr.updated_at`
    bumps on the PR author's own commits/pushes too, not just reviewer
    activity -- that is GitHub's own field semantics, not an identity-join
    bug, and is unaffected by this fixup.
    """
    cutoff = as_of - timedelta(days=threshold_days)
    n_open, n_stale = con.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE state = 'OPEN') AS n_open,
            COUNT(*) FILTER (WHERE state = 'OPEN' AND updated_at::DATE < ?) AS n_stale
        FROM pr
        """,
        [cutoff],
    ).fetchone()
    by_repo = con.execute(
        """
        SELECT
            repo,
            COUNT(*) FILTER (WHERE state = 'OPEN') AS n_open,
            COUNT(*) FILTER (WHERE state = 'OPEN' AND updated_at::DATE < ?) AS n_stale
        FROM pr
        GROUP BY 1
        ORDER BY 1
        """,
        [cutoff],
    ).fetchall()

    raw_value = (n_stale / n_open) if n_open else None
    return [
        _make_row(
            metric_id="stale_pr_rate",
            window_start=as_of,
            window_end=as_of,
            raw_value=raw_value,
            n=n_open or 0,
            floor=FLOOR_RATE_RATIO,
            run_id=run_id,
            computed_at=computed_at,
            details={
                "n_open": n_open or 0,
                "n_stale": n_stale or 0,
                "threshold_days": threshold_days,
                "by_repo": [
                    {"repo": repo, "n_open": repo_open, "n_stale": repo_stale}
                    for repo, repo_open, repo_stale in by_repo
                ],
                "note": (
                    "Reuses pr.updated_at (GitHub's own updatedAt, which bumps on any "
                    "review/comment/label/CI activity, not only human activity) as the "
                    "staleness signal, same simplification stale_jira_rate makes for "
                    "issue.updated_at -- this row is a single snapshot as of window_end, not "
                    "a monthly time series."
                ),
            },
        )
    ]


def compute_dev_metrics(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    stale_pr_threshold_days: int = DEFAULT_STALE_PR_THRESHOLD_DAYS,
) -> list[dict]:
    """All six issue #54 metrics' `metric_value` row dicts, in one call --
    `metrics.engine.compute_all`'s single integration point for this module.
    """
    rows: list[dict] = []
    rows.extend(_pr_merge_lead_time(con, as_of, run_id, computed_at))
    rows.extend(_pr_time_to_first_review(con, as_of, run_id, computed_at))
    rows.extend(_pr_time_to_close(con, as_of, run_id, computed_at))
    rows.extend(_pr_review_engagement(con, as_of, run_id, computed_at))
    rows.extend(_time_to_first_response_jira(con, as_of, run_id, computed_at))
    rows.extend(_stale_pr_rate(con, as_of, run_id, computed_at, stale_pr_threshold_days))
    return rows
