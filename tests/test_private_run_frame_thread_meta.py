"""Tests for `project_health.private_run.frame.load_jira_issue_meta`
(issue #122, D27): the public per-thread export
(`thread_export.py`, wired in `runner.py`) uses it to look up each
classified JIRA "thread"'s summary/created_at.

dev@'s equivalent metadata is captured inline by `runner.
collect_dev_pending` (never a separate frame-layer lookup -- a fixup round
of issue #122 removed this module's original `load_dev_thread_meta`,
which was silently dropping threads whose local `root_message_id` didn't
resolve against Pony Mail's live per-month digest; see `frame.
load_jira_issue_meta`'s own docstring and `runner.collect_dev_pending`'s
docstring for the full story), so there is no dev@ counterpart to test
here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pyarrow as pa

from project_health import storage
from project_health.private_run.frame import load_jira_issue_meta


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _write_issue(data_dir, rows: list[dict]) -> None:
    n = len(rows)
    table = pa.table(
        {
            "issue_key": pa.array([r["issue_key"] for r in rows], type=pa.string()),
            "summary": pa.array([r.get("summary") for r in rows], type=pa.string()),
            "status": pa.array([None] * n, type=pa.string()),
            "status_category": pa.array([None] * n, type=pa.string()),
            "priority": pa.array([None] * n, type=pa.string()),
            "issue_type": pa.array([None] * n, type=pa.string()),
            "created_at": pa.array(
                [r["created_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "updated_at": pa.array(
                [r["created_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "resolved_at": pa.array([None] * n, type=pa.timestamp("us", tz="UTC")),
            "reporter_identity_id": pa.array([None] * n, type=pa.string()),
            "reporter_raw": pa.array([None] * n, type=pa.string()),
            "assignee_identity_id": pa.array([None] * n, type=pa.string()),
            "assignee_raw": pa.array([None] * n, type=pa.string()),
            "resolution": pa.array([None] * n, type=pa.string()),
            "source_snapshot_id": pa.array(["snap-1"] * n, type=pa.string()),
        }
    )
    storage.write_partition(data_dir, "jira", "issue", "2026-09-25", "run-1", table)


class TestLoadJiraIssueMeta:
    def test_returns_summary_and_created_at_for_requested_issues(self, tmp_path):
        _write_issue(
            tmp_path,
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "summary": "Fix the thing",
                    "created_at": _ts(2024, 1, 1),
                },
                {"issue_key": "CASSANDRA-2", "summary": "Other", "created_at": _ts(2024, 2, 1)},
            ],
        )
        result = load_jira_issue_meta(tmp_path, "CASSANDRA", {"CASSANDRA-1"})
        assert set(result) == {"CASSANDRA-1"}
        assert result["CASSANDRA-1"]["summary"] == "Fix the thing"
        assert result["CASSANDRA-1"]["created_at"] == _ts(2024, 1, 1)

    def test_filters_by_project_key_prefix(self, tmp_path):
        _write_issue(
            tmp_path,
            [{"issue_key": "OTHER-1", "summary": "x", "created_at": _ts(2024, 1, 1)}],
        )
        result = load_jira_issue_meta(tmp_path, "CASSANDRA", {"OTHER-1"})
        assert result == {}

    def test_empty_issue_keys_returns_empty_without_reading(self, tmp_path):
        assert load_jira_issue_meta(tmp_path, "CASSANDRA", set()) == {}
