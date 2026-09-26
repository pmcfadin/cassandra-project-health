"""Tests for project_health.governance.metrics (issue #36)."""

import json
from datetime import date, datetime, timezone

import pyarrow as pa

from project_health.governance.metrics import (
    DEFINITION_VERSION,
    compute_monthly_check_metrics,
    metric_id_for_check,
)
from project_health.schema import get_schema

NOW = datetime(2024, 8, 15, tzinfo=timezone.utc)


def _row(check_id, result, month_day, is_merge=False):
    return {
        "sha": f"sha-{month_day}-{result}-{is_merge}",
        "branch": "trunk",
        "commit_date": datetime(2024, month_day[0], month_day[1], tzinfo=timezone.utc),
        "author": "a",
        "committer": "a",
        "is_merge": is_merge,
        "reviewers": [],
        "jira_keys": [],
        "policy_version": 1,
        "check_id": check_id,
        "result": result,
        "evidence": "e",
        "evidence_url": None,
    }


def _table(rows):
    return pa.Table.from_pylist(rows, schema=get_schema("commit_compliance"))


def test_empty_input_returns_empty_schema_valid_table():
    table = compute_monthly_check_metrics(
        get_schema("commit_compliance").empty_table(), as_of=date(2024, 8, 1), run_id="r1",
        computed_at=NOW,
    )
    assert table.num_rows == 0
    assert table.schema.equals(get_schema("metric_value"))


def test_monthly_pass_rate_excludes_exempt_and_not_in_force_from_denominator():
    rows = [
        _row("reviewer-present", "pass", (6, 1)),
        _row("reviewer-present", "pass", (6, 2)),
        _row("reviewer-present", "fail", (6, 3)),
        _row("reviewer-present", "unknown", (6, 4)),
        _row("reviewer-present", "exempt", (6, 5)),
        _row("reviewer-present", "not_in_force", (6, 6)),
    ]
    table = compute_monthly_check_metrics(
        _table(rows), as_of=date(2024, 8, 1), run_id="r1", computed_at=NOW
    )
    metric_rows = table.to_pylist()
    assert len(metric_rows) == 1
    row = metric_rows[0]
    assert row["metric_id"] == "governance_reviewer_present_pass_rate"
    assert row["definition_version"] == DEFINITION_VERSION
    # scored_n = pass(2) + fail(1) + unknown(1) = 4; value = 2/4 = 0.5
    assert row["n"] == 4
    assert row["value"] == 0.5
    assert row["flag"] == "ok"
    details = json.loads(row["details_json"])
    assert details == {
        "check_id": "reviewer-present",
        "pass": 2,
        "fail": 1,
        "unknown": 1,
        "exempt": 1,
        "not_in_force": 1,
        "total_including_exempt_and_not_in_force": 6,
    }


def test_merge_commits_excluded_from_aggregate_denominator():
    rows = [
        _row("reviewer-present", "pass", (6, 1), is_merge=False),
        # A merge commit with a real trailer -- scored (would appear in
        # commit_compliance) but must not count toward the aggregate rate
        # (docs/spec/GOVERNANCE.md §3).
        _row("reviewer-present", "fail", (6, 2), is_merge=True),
    ]
    table = compute_monthly_check_metrics(
        _table(rows), as_of=date(2024, 8, 1), run_id="r1", computed_at=NOW
    )
    row = table.to_pylist()[0]
    assert row["n"] == 1
    assert row["value"] == 1.0


def test_current_and_future_months_excluded():
    rows = [_row("reviewer-present", "pass", (8, 1))]  # same month as as_of
    table = compute_monthly_check_metrics(
        _table(rows), as_of=date(2024, 8, 15), run_id="r1", computed_at=NOW
    )
    assert table.num_rows == 0


def test_insufficient_data_flag_when_all_exempt():
    rows = [_row("reviewer-present", "exempt", (6, 1))]
    table = compute_monthly_check_metrics(
        _table(rows), as_of=date(2024, 8, 1), run_id="r1", computed_at=NOW
    )
    row = table.to_pylist()[0]
    assert row["n"] == 0
    assert row["value"] is None
    assert row["flag"] == "insufficient_data"


def test_metric_id_for_check_naming():
    assert metric_id_for_check("reviewer-present") == "governance_reviewer_present_pass_rate"
    assert (
        metric_id_for_check("code-style-checkstyle")
        == "governance_code_style_checkstyle_pass_rate"
    )


def test_separate_metric_per_check_id():
    rows = [
        _row("reviewer-present", "pass", (6, 1)),
        _row("jira-ticket-referenced", "pass", (6, 1)),
    ]
    table = compute_monthly_check_metrics(
        _table(rows), as_of=date(2024, 8, 1), run_id="r1", computed_at=NOW
    )
    metric_ids = {r["metric_id"] for r in table.to_pylist()}
    assert metric_ids == {
        "governance_reviewer_present_pass_rate",
        "governance_jira_ticket_referenced_pass_rate",
    }
