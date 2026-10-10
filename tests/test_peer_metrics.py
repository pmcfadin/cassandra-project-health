"""Tests for project_health.metrics.peer_metrics (issue #145)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa
import pytest

from project_health.metrics.peer_metrics import TIME_TO_FIRST_RESPONSE_PR, compute_peer_metrics
from project_health.schema import get_schema

REPO = "apache/kafka"
COMPUTED_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)
AS_OF = date(2025, 1, 1)


def _pr_row(
    number: int, created: datetime, closed: datetime | None, merged: bool, author="author1"
):
    return {
        "repo": REPO,
        "number": number,
        "state": "MERGED" if merged else ("CLOSED" if closed else "OPEN"),
        "is_draft": False,
        "merged": merged,
        "author_identity_id": None,
        "author_raw_type": "github_login",
        "author_raw_value": author,
        "title_hash": "x" * 10,
        "linked_issue_keys": [],
        "created_at": created,
        "updated_at": created,
        "closed_at": closed,
        "merged_at": closed if merged else None,
        "additions": 1,
        "deletions": 1,
        "changed_files": 1,
        "source_snapshot_id": "s1",
    }


@pytest.fixture
def base_tables():
    pr_rows = [
        _pr_row(
            1,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 5, tzinfo=timezone.utc),
            True,
        ),
        _pr_row(2, datetime(2024, 2, 1, tzinfo=timezone.utc), None, False),
    ]
    pr = pa.Table.from_pylist(pr_rows, schema=get_schema("pr"))

    review_rows = [
        {
            "review_id": "r1",
            "repo": REPO,
            "pr_number": 1,
            "reviewer_identity_id": None,
            "reviewer_raw_type": "github_login",
            "reviewer_raw_value": "reviewer1",
            "state": "APPROVED",
            "submitted_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "source_snapshot_id": "s1",
        }
    ]
    pr_review = pa.Table.from_pylist(review_rows, schema=get_schema("pr_review"))

    comment_rows = [
        {
            "comment_id": "c1",
            "repo": REPO,
            "pr_number": 2,
            "review_id": None,
            "comment_type": "issue_comment",
            "author_identity_id": None,
            "author_raw_type": "github_login",
            "author_raw_value": "commenter1",
            "created_at": datetime(2024, 2, 3, tzinfo=timezone.utc),
            "source_snapshot_id": "s1",
        }
    ]
    pr_comment = pa.Table.from_pylist(comment_rows, schema=get_schema("pr_comment"))

    contrib_rows = [
        {
            "event_id": f"c{i}",
            "identity_id": None,
            "author_raw_type": "git_email",
            "author_raw_value": f"dev{i % 3}@example.org",
            "author_display_name": f"Dev {i % 3}",
            "event_type": "code_commit",
            "occurred_at": datetime(2024, (i % 12) + 1, 1, tzinfo=timezone.utc),
            "repo": REPO,
            "source_ref": f"sha{i}",
            "source_snapshot_id": "s1",
        }
        for i in range(20)
    ]
    contribution_event = pa.Table.from_pylist(contrib_rows, schema=get_schema("contribution_event"))

    release_rows = [
        {
            "release_id": "4.0.0",
            "tag_name": "4.0.0",
            "version": "4.0.0",
            "major_minor": "4.0",
            "release_date": date(2024, 3, 1),
            "release_date_source": "git_tag",
            "archive_verified": None,
            "archive_date": None,
            "repo": REPO,
            "source_snapshot_id": "s1",
            "collected_at": datetime(2024, 3, 1, tzinfo=timezone.utc),
        }
    ]
    release = pa.Table.from_pylist(release_rows, schema=get_schema("release"))

    return {
        "pr": pr,
        "pr_review": pr_review,
        "pr_comment": pr_comment,
        "contribution_event": contribution_event,
        "release": release,
    }


def test_computes_all_five_metric_families(base_tables):
    computed = compute_peer_metrics(
        base_tables, as_of=AS_OF, run_id="run1", computed_at=COMPUTED_AT
    )
    metric_ids = set(computed["metric_value"].column("metric_id").to_pylist())
    assert "time_to_first_response_pr" == TIME_TO_FIRST_RESPONSE_PR
    assert TIME_TO_FIRST_RESPONSE_PR in metric_ids
    assert "change_request_closure_ratio_pr" in metric_ids
    assert "contributor_absence_factor" in metric_ids
    assert "release_frequency" in metric_ids

    backlog_metric_ids = set(computed["pr_backlog"].column("metric_id").to_pylist())
    assert any("open_pr_backlog_total__" in mid for mid in backlog_metric_ids)


def test_time_to_first_response_counts_review_and_comment(base_tables):
    computed = compute_peer_metrics(
        base_tables, as_of=AS_OF, run_id="run1", computed_at=COMPUTED_AT
    )
    rows = [
        r
        for r in computed["metric_value"].to_pylist()
        if r["metric_id"] == TIME_TO_FIRST_RESPONSE_PR
    ]
    # PR #1 (Jan 2024) has a review response, PR #2 (Feb 2024) has a comment
    # response -- both should be counted (n=1 each month).
    by_month = {r["window_start"]: r for r in rows}
    assert by_month[date(2024, 1, 1)]["n"] == 1
    assert by_month[date(2024, 2, 1)]["n"] == 1


def test_self_comments_excluded_from_time_to_first_response():
    pr_rows = [_pr_row(1, datetime(2024, 1, 1, tzinfo=timezone.utc), None, False, author="alice")]
    pr = pa.Table.from_pylist(pr_rows, schema=get_schema("pr"))
    comment_rows = [
        {
            "comment_id": "c1",
            "repo": REPO,
            "pr_number": 1,
            "review_id": None,
            "comment_type": "issue_comment",
            "author_identity_id": None,
            "author_raw_type": "github_login",
            "author_raw_value": "alice",  # same as PR author -- must be excluded
            "created_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "source_snapshot_id": "s1",
        }
    ]
    pr_comment = pa.Table.from_pylist(comment_rows, schema=get_schema("pr_comment"))
    tables = {
        "pr": pr,
        "pr_review": get_schema("pr_review").empty_table(),
        "pr_comment": pr_comment,
        "contribution_event": get_schema("contribution_event").empty_table(),
        "release": get_schema("release").empty_table(),
    }
    computed = compute_peer_metrics(tables, as_of=AS_OF, run_id="run1", computed_at=COMPUTED_AT)
    rows = [
        r
        for r in computed["metric_value"].to_pylist()
        if r["metric_id"] == TIME_TO_FIRST_RESPONSE_PR
    ]
    assert rows == [] or all(r["n"] == 0 for r in rows)


def test_empty_tables_produce_no_rows_not_a_crash():
    empty = {
        "pr": get_schema("pr").empty_table(),
        "pr_review": get_schema("pr_review").empty_table(),
        "pr_comment": get_schema("pr_comment").empty_table(),
        "contribution_event": get_schema("contribution_event").empty_table(),
        "release": get_schema("release").empty_table(),
    }
    computed = compute_peer_metrics(empty, as_of=AS_OF, run_id="run1", computed_at=COMPUTED_AT)
    assert computed["metric_value"].num_rows == 0
    assert computed["pr_backlog"].num_rows == 0


def test_different_projects_isolated_in_separate_calls(base_tables):
    """Each call registers its own fresh DuckDB connection (`engine._connect`)
    -- calling `compute_peer_metrics` twice with two different repos' data
    must never mix their numbers."""
    other_repo = "apache/spark"
    other_pr_rows = [
        _pr_row(
            1,
            datetime(2024, 6, 1, tzinfo=timezone.utc),
            datetime(2024, 6, 10, tzinfo=timezone.utc),
            True,
        )
    ]
    for row in other_pr_rows:
        row["repo"] = other_repo
    other_tables = dict(base_tables)
    other_tables["pr"] = pa.Table.from_pylist(other_pr_rows, schema=get_schema("pr"))
    other_tables["pr_review"] = get_schema("pr_review").empty_table()
    other_tables["pr_comment"] = get_schema("pr_comment").empty_table()
    other_tables["contribution_event"] = get_schema("contribution_event").empty_table()
    other_tables["release"] = get_schema("release").empty_table()

    first = compute_peer_metrics(base_tables, as_of=AS_OF, run_id="run1", computed_at=COMPUTED_AT)
    second = compute_peer_metrics(other_tables, as_of=AS_OF, run_id="run1", computed_at=COMPUTED_AT)

    first_backlog_repos = {
        r["metric_id"].rsplit("__", 1)[-1] for r in first["pr_backlog"].to_pylist()
    }
    second_backlog_repos = {
        r["metric_id"].rsplit("__", 1)[-1] for r in second["pr_backlog"].to_pylist()
    }
    assert first_backlog_repos == {"kafka"}
    assert second_backlog_repos == {"spark"}
