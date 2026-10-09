"""Tests for project_health.metrics.pr_backlog (issue #142).

Covers: monthly open-as-of-T reconstruction, age-bucket assignment,
linked-ticket-state assignment at T (including the `pr.linked_issue_keys`
union `pr_issue_link` join and `pipeline._dedupe_issue_rows` dedupe this
module relies on upstream), the no-GitHub-response share and its
sample-size floor, the deliberate absence of a base-branch metric, and the
`build_pr_backlog_registry` registry rows.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from project_health.metrics.pr_backlog import (
    AGE_1_3Y,
    AGE_30_90D,
    AGE_90D_1Y,
    AGE_GT_3Y,
    AGE_LT_30D,
    ALL_METRIC_IDS,
    DRAFTS,
    FLOOR_RATE_RATIO,
    NO_GITHUB_RESPONSE_SHARE,
    NO_TICKET_KEY,
    TICKET_CLOSED_OTHER,
    TICKET_FIXED,
    TICKET_OPEN,
    TOTAL,
    build_pr_backlog_registry,
    compute_pr_backlog,
)
from project_health.pipeline import _dedupe_issue_rows
from tests.fixtures.metrics.builders import issues, pr_comments, pr_issue_link, pr_reviews, prs

UTC = timezone.utc
REPO = "apache/cassandra"
RUN_ID = "run-test"


def _ts(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def _empty_tables(**overrides):
    tables = {
        "pr": prs([]),
        "pr_review": pr_reviews([]),
        "pr_comment": pr_comments([]),
        "pr_issue_link": pr_issue_link([]),
        "issue": issues([]),
    }
    tables.update(overrides)
    return tables


def _rows_for(table, metric_id):
    return sorted(
        (r for r in table.to_pylist() if r["metric_id"] == metric_id),
        key=lambda r: r["window_start"],
    )


def _row_for_month(table, metric_id, year, month):
    for r in _rows_for(table, metric_id):
        if r["window_start"] == date(year, month, 1):
            return r
    return None


def _details(row):
    return json.loads(row["details_json"]) if row and row["details_json"] else {}


# --- Monthly open-as-of-T reconstruction --------------------------------


def test_backlog_total_counts_pr_open_as_of_month_end():
    # PR A: created 2026-01-05, still open -> counts in every month from
    # Jan 2026 through the last completed month.
    # PR B: created 2026-01-10, closed 2026-02-15 -> open as of Jan, not
    # open as of Feb (closed before Feb's month-end) or later.
    pr_rows = [
        {"repo": REPO, "number": 1, "author_raw_value": "alice", "created_at": _ts(2026, 1, 5)},
        {
            "repo": REPO,
            "number": 2,
            "author_raw_value": "bob",
            "created_at": _ts(2026, 1, 10),
            "closed_at": _ts(2026, 2, 15),
        },
    ]
    tables = _empty_tables(pr=prs(pr_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 4, 1), run_id=RUN_ID, computed_at=_ts(2026, 4, 1)
    )

    jan = _row_for_month(result, TOTAL, 2026, 1)
    feb = _row_for_month(result, TOTAL, 2026, 2)
    mar = _row_for_month(result, TOTAL, 2026, 3)

    assert jan["value"] == 2.0
    assert feb["value"] == 1.0  # PR B closed 2026-02-15, after Feb's month-end? no: before Mar.
    assert mar["value"] == 1.0


def test_backlog_closed_exactly_at_month_end_is_excluded_that_month():
    """`closed_at > T` (strictly after) is required to still count as open
    as of T -- a PR closed at exactly a month's last instant is excluded
    from that same month, but still counted as open the month before."""
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "alice",
            "created_at": _ts(2025, 12, 5),
            "closed_at": datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC),
        }
    ]
    tables = _empty_tables(pr=prs(pr_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 3, 1), run_id=RUN_ID, computed_at=_ts(2026, 3, 1)
    )
    dec = _row_for_month(result, TOTAL, 2025, 12)
    jan = _row_for_month(result, TOTAL, 2026, 1)
    feb = _row_for_month(result, TOTAL, 2026, 2)
    assert dec["value"] == 1.0
    assert jan["value"] == 0.0  # closed_at equals Jan's month-end instant, not strictly after.
    assert feb["value"] == 0.0


def test_backlog_headcount_has_no_sample_size_floor():
    """A month with zero open PRs still reports value=0.0, flag='ok' --
    METRICS.md §0.6's headcount exemption, same as active_contributors_monthly."""
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "alice",
            "created_at": _ts(2026, 1, 5),
            "closed_at": _ts(2026, 1, 10),
        }
    ]
    tables = _empty_tables(pr=prs(pr_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 3, 1), run_id=RUN_ID, computed_at=_ts(2026, 3, 1)
    )
    feb = _row_for_month(result, TOTAL, 2026, 2)
    assert feb["value"] == 0.0
    assert feb["flag"] == "ok"


def test_drafts_subset_of_total():
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "alice",
            "created_at": _ts(2026, 1, 5),
            "is_draft": True,
        },
        {
            "repo": REPO,
            "number": 2,
            "author_raw_value": "bob",
            "created_at": _ts(2026, 1, 6),
            "is_draft": False,
        },
    ]
    tables = _empty_tables(pr=prs(pr_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    jan_drafts = _row_for_month(result, DRAFTS, 2026, 1)
    jan_total = _row_for_month(result, TOTAL, 2026, 1)
    assert jan_drafts["value"] == 1.0
    assert jan_total["value"] == 2.0


# --- Age buckets ----------------------------------------------------------


def test_age_buckets_assign_each_pr_to_exactly_one_bucket():
    pr_rows = [
        # ~10 days old at month end -> <30d
        {"repo": REPO, "number": 1, "author_raw_value": "a", "created_at": _ts(2026, 6, 20)},
        # ~60 days old -> 30-90d
        {"repo": REPO, "number": 2, "author_raw_value": "b", "created_at": _ts(2026, 5, 1)},
        # ~180 days old -> 90d-1y
        {"repo": REPO, "number": 3, "author_raw_value": "c", "created_at": _ts(2026, 1, 1)},
        # ~2 years old -> 1-3y
        {"repo": REPO, "number": 4, "author_raw_value": "d", "created_at": _ts(2024, 6, 30)},
        # ~5 years old -> >3y
        {"repo": REPO, "number": 5, "author_raw_value": "e", "created_at": _ts(2021, 6, 30)},
    ]
    tables = _empty_tables(pr=prs(pr_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 7, 1), run_id=RUN_ID, computed_at=_ts(2026, 7, 1)
    )

    buckets = {
        AGE_LT_30D: 1,
        AGE_30_90D: 1,
        AGE_90D_1Y: 1,
        AGE_1_3Y: 1,
        AGE_GT_3Y: 1,
    }
    for metric_id, expected in buckets.items():
        row = _row_for_month(result, metric_id, 2026, 6)
        assert row["value"] == expected, metric_id

    total = _row_for_month(result, TOTAL, 2026, 6)
    assert total["value"] == 5.0


# --- Linked-ticket state at T ---------------------------------------------


def test_ticket_state_no_key_in_title():
    pr_rows = [{"repo": REPO, "number": 1, "author_raw_value": "a", "created_at": _ts(2026, 1, 5)}]
    tables = _empty_tables(pr=prs(pr_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    row = _row_for_month(result, NO_TICKET_KEY, 2026, 1)
    assert row["value"] == 1.0


def test_ticket_state_still_open():
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "a",
            "created_at": _ts(2026, 1, 5),
            "linked_issue_keys": ["CASSANDRA-1"],
        }
    ]
    issue_rows = [
        {"issue_key": "CASSANDRA-1", "created_at": _ts(2025, 1, 1), "updated_at": _ts(2026, 1, 5)}
    ]
    tables = _empty_tables(pr=prs(pr_rows), issue=issues(issue_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    row = _row_for_month(result, TICKET_OPEN, 2026, 1)
    assert row["value"] == 1.0


def test_ticket_state_fixed_at_or_before_t():
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "a",
            "created_at": _ts(2026, 1, 5),
            "linked_issue_keys": ["CASSANDRA-1"],
        }
    ]
    issue_rows = [
        {
            "issue_key": "CASSANDRA-1",
            "created_at": _ts(2025, 1, 1),
            "updated_at": _ts(2026, 1, 20),
            "resolved_at": _ts(2026, 1, 20),
            "resolution": "Fixed",
        }
    ]
    tables = _empty_tables(pr=prs(pr_rows), issue=issues(issue_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    row = _row_for_month(result, TICKET_FIXED, 2026, 1)
    assert row["value"] == 1.0
    assert _row_for_month(result, TICKET_OPEN, 2026, 1)["value"] == 0.0


def test_ticket_state_resolved_but_not_fixed_is_closed_other():
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "a",
            "created_at": _ts(2026, 1, 5),
            "linked_issue_keys": ["CASSANDRA-1"],
        }
    ]
    issue_rows = [
        {
            "issue_key": "CASSANDRA-1",
            "created_at": _ts(2025, 1, 1),
            "updated_at": _ts(2026, 1, 20),
            "resolved_at": _ts(2026, 1, 20),
            "resolution": "Won't Fix",
        }
    ]
    tables = _empty_tables(pr=prs(pr_rows), issue=issues(issue_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    row = _row_for_month(result, TICKET_CLOSED_OTHER, 2026, 1)
    assert row["value"] == 1.0


def test_ticket_state_resolution_after_t_still_counts_as_open():
    """A ticket resolved AFTER the reconstructed month-end T must read as
    'still open' for that earlier month -- resolved_at <= T gates the bucket."""
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "a",
            "created_at": _ts(2026, 1, 5),
            "linked_issue_keys": ["CASSANDRA-1"],
        }
    ]
    issue_rows = [
        {
            "issue_key": "CASSANDRA-1",
            "created_at": _ts(2025, 1, 1),
            "updated_at": _ts(2026, 3, 1),
            "resolved_at": _ts(2026, 3, 1),
            "resolution": "Fixed",
        }
    ]
    tables = _empty_tables(pr=prs(pr_rows), issue=issues(issue_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 4, 1), run_id=RUN_ID, computed_at=_ts(2026, 4, 1)
    )
    jan_open = _row_for_month(result, TICKET_OPEN, 2026, 1)
    feb_open = _row_for_month(result, TICKET_OPEN, 2026, 2)
    mar_fixed = _row_for_month(result, TICKET_FIXED, 2026, 3)
    assert jan_open["value"] == 1.0
    assert feb_open["value"] == 1.0
    assert mar_fixed["value"] == 1.0


def test_ticket_key_union_with_pr_issue_link_backfill():
    """issue #105: a PR collected before `pr.linked_issue_keys` existed
    (null column) still gets its ticket key from `pr_issue_link`."""
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "a",
            "created_at": _ts(2026, 1, 5),
            "linked_issue_keys": None,
        }
    ]
    issue_rows = [
        {
            "issue_key": "CASSANDRA-9",
            "created_at": _ts(2025, 1, 1),
            "updated_at": _ts(2026, 1, 20),
            "resolved_at": _ts(2026, 1, 20),
            "resolution": "Fixed",
        }
    ]
    link_rows = [{"repo": REPO, "number": 1, "issue_key": "CASSANDRA-9"}]
    tables = _empty_tables(
        pr=prs(pr_rows), issue=issues(issue_rows), pr_issue_link=pr_issue_link(link_rows)
    )
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    assert _row_for_month(result, TICKET_FIXED, 2026, 1)["value"] == 1.0
    assert _row_for_month(result, NO_TICKET_KEY, 2026, 1)["value"] == 0.0


def test_ticket_state_uses_already_deduped_issue_table():
    """`pipeline.run_pipeline` always calls `_dedupe_issue_rows` before
    handing `issue` to `compute_pr_backlog` (same contract
    `compute_review_responsiveness` relies on) -- an older row (no
    resolution yet) and a newer backfill row (resolution filled in) for the
    same issue_key must collapse to the newer row's resolved state."""
    pr_rows = [
        {
            "repo": REPO,
            "number": 1,
            "author_raw_value": "a",
            "created_at": _ts(2026, 1, 5),
            "linked_issue_keys": ["CASSANDRA-1"],
        }
    ]
    issue_rows = [
        # Older snapshot: not yet resolved.
        {
            "issue_key": "CASSANDRA-1",
            "created_at": _ts(2025, 1, 1),
            "updated_at": _ts(2026, 1, 10),
            "source_snapshot_id": "run-1:jira",
        },
        # Newer snapshot (same updated_at timestamp would tie-break on
        # resolution presence; here it's simply later): now resolved Fixed.
        {
            "issue_key": "CASSANDRA-1",
            "created_at": _ts(2025, 1, 1),
            "updated_at": _ts(2026, 1, 20),
            "resolved_at": _ts(2026, 1, 20),
            "resolution": "Fixed",
            "source_snapshot_id": "run-2:jira",
        },
    ]
    deduped_issue = _dedupe_issue_rows(issues(issue_rows))
    assert deduped_issue.num_rows == 1  # dedupe collapsed both snapshots to one row.

    tables = _empty_tables(pr=prs(pr_rows), issue=deduped_issue)
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    assert _row_for_month(result, TICKET_FIXED, 2026, 1)["value"] == 1.0
    assert _row_for_month(result, TICKET_OPEN, 2026, 1)["value"] == 0.0


# --- No-GitHub-response share ----------------------------------------------


def test_no_github_response_share_excludes_authors_own_activity():
    pr_rows = [
        {"repo": REPO, "number": 1, "author_raw_value": "alice", "created_at": _ts(2026, 1, 1)},
        {"repo": REPO, "number": 2, "author_raw_value": "bob", "created_at": _ts(2026, 1, 1)},
        {"repo": REPO, "number": 3, "author_raw_value": "carol", "created_at": _ts(2026, 1, 1)},
        {"repo": REPO, "number": 4, "author_raw_value": "dan", "created_at": _ts(2026, 1, 1)},
        {"repo": REPO, "number": 5, "author_raw_value": "erin", "created_at": _ts(2026, 1, 1)},
    ]
    # PR 1: a non-author review -> has a response.
    review_rows = [
        {
            "repo": REPO,
            "pr_number": 1,
            "reviewer_raw_value": "reviewer-x",
            "submitted_at": _ts(2026, 1, 5),
        }
    ]
    # PR 2: only the author's own comment -> still no visible response.
    comment_rows = [
        {"repo": REPO, "pr_number": 2, "author_raw_value": "bob", "created_at": _ts(2026, 1, 5)}
    ]
    # PRs 3, 4, 5: no activity at all -> no response.
    tables = _empty_tables(
        pr=prs(pr_rows), pr_review=pr_reviews(review_rows), pr_comment=pr_comments(comment_rows)
    )
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    row = _row_for_month(result, NO_GITHUB_RESPONSE_SHARE, 2026, 1)
    details = _details(row)
    assert details["n_hit"] == 4  # PRs 2, 3, 4, 5 have no visible non-author response.
    assert details["n_denominator"] == 5
    assert row["value"] == 4 / 5
    assert row["flag"] == "ok"  # n=5 clears FLOOR_RATE_RATIO.
    assert details["label"] == "github_only_review_may_occur_in_jira"


def test_no_github_response_share_below_floor_is_insufficient_data():
    pr_rows = [
        {"repo": REPO, "number": 1, "author_raw_value": "a", "created_at": _ts(2026, 1, 1)},
    ]
    tables = _empty_tables(pr=prs(pr_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    row = _row_for_month(result, NO_GITHUB_RESPONSE_SHARE, 2026, 1)
    assert row["flag"] == "insufficient_data"
    assert row["value"] is None
    assert FLOOR_RATE_RATIO == 5


# --- Base branch: deliberately not emitted --------------------------------


def test_no_base_branch_metric_is_emitted():
    """Issue #142's own real-data check: PR base branch isn't collected
    (schema/tables.py's PR table has no base-ref column), so this module
    must never fabricate a base-branch metric_id."""
    assert not any("base" in metric_id for metric_id in ALL_METRIC_IDS)

    pr_rows = [{"repo": REPO, "number": 1, "author_raw_value": "a", "created_at": _ts(2026, 1, 1)}]
    tables = _empty_tables(pr=prs(pr_rows))
    result = compute_pr_backlog(
        tables, as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    assert not any("base" in r["metric_id"] for r in result.to_pylist())


# --- Empty input -----------------------------------------------------------


def test_compute_pr_backlog_with_no_prs_returns_empty_table():
    result = compute_pr_backlog(
        _empty_tables(), as_of=date(2026, 2, 1), run_id=RUN_ID, computed_at=_ts(2026, 2, 1)
    )
    assert result.num_rows == 0


# --- Registry ---------------------------------------------------------------


def test_build_pr_backlog_registry_covers_every_metric_id():
    table = build_pr_backlog_registry(_ts(2026, 10, 9))
    rows_by_id = {r["metric_id"]: r for r in table.to_pylist()}
    assert set(rows_by_id) == set(ALL_METRIC_IDS)
    for metric_id in ALL_METRIC_IDS:
        row = rows_by_id[metric_id]
        assert row["version"] == "1.0"
        assert row["description"]
        assert row["changelog_note"]
