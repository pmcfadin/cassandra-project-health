"""Tests for `project_health.private_run.frame`'s issue #122 (D27)
additions: `load_dev_thread_meta` and `load_jira_issue_meta`, which the
public per-thread export (`thread_export.py`, wired in `runner.py`) uses
to look up each classified thread's root message id / started_at (dev@)
or summary / created_at (JIRA)."""

from __future__ import annotations

from datetime import datetime, timezone

import pyarrow as pa

from project_health import storage
from project_health.private_run.frame import load_dev_thread_meta, load_jira_issue_meta


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _write_message_thread(data_dir, rows: list[dict]) -> None:
    table = pa.table(
        {
            "thread_id": pa.array([r["thread_id"] for r in rows], type=pa.string()),
            "list": pa.array([r.get("list", "dev") for r in rows], type=pa.string()),
            "root_message_id": pa.array(
                [r.get("root_message_id", r["thread_id"] + "-root") for r in rows],
                type=pa.string(),
            ),
            "started_at": pa.array(
                [r["started_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "last_activity_at": pa.array(
                [r["started_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "message_count": pa.array([r.get("message_count", 1) for r in rows], type=pa.int64()),
            "source_snapshot_id": pa.array(["snap-1"] * len(rows), type=pa.string()),
        }
    )
    storage.write_partition(data_dir, "ponymail", "message_thread", "2026-09-25", "run-1", table)


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


class TestLoadDevThreadMeta:
    def test_returns_root_message_id_and_started_at_for_requested_threads(self, tmp_path):
        _write_message_thread(
            tmp_path,
            [
                {"thread_id": "t1", "started_at": _ts(2024, 1, 1)},
                {"thread_id": "t2", "started_at": _ts(2024, 2, 1)},
            ],
        )
        result = load_dev_thread_meta(tmp_path, "dev", {"t1"})
        assert set(result) == {"t1"}
        assert result["t1"]["root_message_id"] == "t1-root"
        assert result["t1"]["started_at"] == _ts(2024, 1, 1)

    def test_filters_by_list_name(self, tmp_path):
        _write_message_thread(
            tmp_path,
            [{"thread_id": "t1", "list": "user", "started_at": _ts(2024, 1, 1)}],
        )
        result = load_dev_thread_meta(tmp_path, "dev", {"t1"})
        assert result == {}

    def test_empty_thread_ids_returns_empty_without_reading(self, tmp_path):
        assert load_dev_thread_meta(tmp_path, "dev", set()) == {}

    def test_no_table_returns_empty(self, tmp_path):
        assert load_dev_thread_meta(tmp_path, "dev", {"t1"}) == {}


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
