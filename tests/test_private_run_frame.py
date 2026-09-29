"""Tests for project_health.private_run.frame (issue #110).

Builds tiny synthetic Parquet partitions for `ponymail/message_thread`,
`ponymail/message`, and `jira/issue` via `project_health.storage.
write_partition` (the same real write path collectors use), then checks the
frame builders read them back correctly -- no network, no real Cassandra
data anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pyarrow as pa

from project_health import storage
from project_health.private_run.frame import (
    load_dev_author_history,
    load_dev_messages_for_threads,
    load_dev_thread_frame,
    load_jira_author_history,
    load_jira_thread_frame,
)


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _write_message_thread(data_dir, rows: list[dict], run_id: str = "run-1") -> None:
    table = pa.table(
        {
            "thread_id": pa.array([r["thread_id"] for r in rows], type=pa.string()),
            "list": pa.array([r["list"] for r in rows], type=pa.string()),
            "root_message_id": pa.array([r["thread_id"] + "-root" for r in rows], type=pa.string()),
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
    storage.write_partition(data_dir, "ponymail", "message_thread", "2026-09-25", run_id, table)


def _write_message(data_dir, rows: list[dict], run_id: str = "run-1") -> None:
    table = pa.table(
        {
            "message_id": pa.array([r["message_id"] for r in rows], type=pa.string()),
            "list": pa.array([r["list"] for r in rows], type=pa.string()),
            "sender_identity_id": pa.array([None] * len(rows), type=pa.string()),
            "sender_raw_type": pa.array(["mailing_list_address"] * len(rows), type=pa.string()),
            "sender_raw_value": pa.array([r["sender_raw_value"] for r in rows], type=pa.string()),
            "sender_display_name": pa.array([None] * len(rows), type=pa.string()),
            "occurred_at": pa.array(
                [r["occurred_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "subject_hash": pa.array(["a" * 64] * len(rows), type=pa.string()),
            "in_reply_to": pa.array([r.get("in_reply_to") for r in rows], type=pa.string()),
            "references": pa.array([[] for _ in rows], type=pa.list_(pa.string())),
            "thread_id": pa.array([r["thread_id"] for r in rows], type=pa.string()),
            "source_snapshot_id": pa.array(["snap-1"] * len(rows), type=pa.string()),
        }
    )
    storage.write_partition(data_dir, "ponymail", "message", "2026-09-25", run_id, table)


def _write_issue(data_dir, rows: list[dict], run_id: str = "run-1") -> None:
    n = len(rows)
    table = pa.table(
        {
            "issue_key": pa.array([r["issue_key"] for r in rows], type=pa.string()),
            "summary": pa.array([None] * n, type=pa.string()),
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
    storage.write_partition(data_dir, "jira", "issue", "2026-09-25", run_id, table)


def _write_issue_comment(data_dir, rows: list[dict], run_id: str = "run-1") -> None:
    n = len(rows)
    table = pa.table(
        {
            "comment_id": pa.array([r["comment_id"] for r in rows], type=pa.string()),
            "issue_key": pa.array([r["issue_key"] for r in rows], type=pa.string()),
            "author_identity_id": pa.array([None] * n, type=pa.string()),
            "author_raw_type": pa.array(["jira_username"] * n, type=pa.string()),
            "author_raw_value": pa.array(
                [r["author_raw_value"] for r in rows], type=pa.string()
            ),
            "created_at": pa.array(
                [r["created_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "source_snapshot_id": pa.array(["snap-1"] * n, type=pa.string()),
        }
    )
    storage.write_partition(data_dir, "jira", "issue_comment", "2026-09-25", run_id, table)


class TestLoadDevThreadFrame:
    def test_buckets_threads_by_started_at_quarter(self, tmp_path):
        _write_message_thread(
            tmp_path,
            [
                {"thread_id": "t1", "list": "dev", "started_at": _ts(2024, 1, 5)},
                {"thread_id": "t2", "list": "dev", "started_at": _ts(2024, 2, 1)},
                {"thread_id": "t3", "list": "dev", "started_at": _ts(2024, 4, 1)},
            ],
        )
        frame = load_dev_thread_frame(tmp_path, "dev", {"2024Q1", "2024Q2"})
        assert sorted(frame["2024Q1"]) == ["t1", "t2"]
        assert frame["2024Q2"] == ["t3"]

    def test_filters_to_requested_list(self, tmp_path):
        _write_message_thread(
            tmp_path,
            [
                {"thread_id": "dev-thread", "list": "dev", "started_at": _ts(2024, 1, 5)},
                {"thread_id": "user-thread", "list": "user", "started_at": _ts(2024, 1, 5)},
            ],
        )
        frame = load_dev_thread_frame(tmp_path, "dev", {"2024Q1"})
        assert frame["2024Q1"] == ["dev-thread"]

    def test_excludes_quarters_outside_the_requested_set(self, tmp_path):
        _write_message_thread(
            tmp_path, [{"thread_id": "t1", "list": "dev", "started_at": _ts(2019, 6, 1)}]
        )
        frame = load_dev_thread_frame(tmp_path, "dev", {"2024Q1"})
        assert frame == {}

    def test_no_partitions_written_yet_returns_empty(self, tmp_path):
        assert load_dev_thread_frame(tmp_path, "dev", {"2024Q1"}) == {}

    def test_dedupes_a_thread_id_written_by_two_overlapping_collection_runs(self, tmp_path):
        # issue #112 fixup: the raw message_thread table is append-only
        # across nightly runs and isn't guaranteed unique per thread_id --
        # verified live against the real data branch (1,129/731/11,763
        # duplicate message_id/thread_id/issue_key rows). Two partitions,
        # same thread_id, must collapse to one entry in the frame.
        _write_message_thread(
            tmp_path,
            [{"thread_id": "t1", "list": "dev", "started_at": _ts(2024, 1, 5)}],
            run_id="run-1",
        )
        _write_message_thread(
            tmp_path,
            [{"thread_id": "t1", "list": "dev", "started_at": _ts(2024, 1, 5)}],
            run_id="run-2",
        )
        frame = load_dev_thread_frame(tmp_path, "dev", {"2024Q1"})
        assert frame["2024Q1"] == ["t1"]


class TestLoadJiraThreadFrame:
    def test_buckets_issues_by_created_at_quarter(self, tmp_path):
        _write_issue(
            tmp_path,
            [
                {"issue_key": "CASSANDRA-1", "created_at": _ts(2024, 1, 5)},
                {"issue_key": "CASSANDRA-2", "created_at": _ts(2024, 7, 1)},
            ],
        )
        frame = load_jira_thread_frame(tmp_path, "CASSANDRA", {"2024Q1", "2024Q3"})
        assert frame["2024Q1"] == ["CASSANDRA-1"]
        assert frame["2024Q3"] == ["CASSANDRA-2"]

    def test_filters_to_the_configured_project_key(self, tmp_path):
        _write_issue(
            tmp_path,
            [
                {"issue_key": "CASSANDRA-1", "created_at": _ts(2024, 1, 5)},
                {"issue_key": "OTHERPROJ-1", "created_at": _ts(2024, 1, 5)},
            ],
        )
        frame = load_jira_thread_frame(tmp_path, "CASSANDRA", {"2024Q1"})
        assert frame["2024Q1"] == ["CASSANDRA-1"]

    def test_no_partitions_written_yet_returns_empty(self, tmp_path):
        assert load_jira_thread_frame(tmp_path, "CASSANDRA", {"2024Q1"}) == {}

    def test_dedupes_an_issue_key_written_by_two_overlapping_collection_runs(self, tmp_path):
        _write_issue(
            tmp_path, [{"issue_key": "CASSANDRA-1", "created_at": _ts(2024, 1, 5)}], run_id="run-1"
        )
        _write_issue(
            tmp_path, [{"issue_key": "CASSANDRA-1", "created_at": _ts(2024, 1, 5)}], run_id="run-2"
        )
        frame = load_jira_thread_frame(tmp_path, "CASSANDRA", {"2024Q1"})
        assert frame["2024Q1"] == ["CASSANDRA-1"]


class TestLoadDevMessagesForThreads:
    def test_returns_only_requested_threads(self, tmp_path):
        _write_message(
            tmp_path,
            [
                {
                    "message_id": "<m1@x>",
                    "list": "dev",
                    "sender_raw_value": "alice@example.com",
                    "occurred_at": _ts(2024, 1, 1),
                    "thread_id": "t1",
                },
                {
                    "message_id": "<m2@x>",
                    "list": "dev",
                    "sender_raw_value": "bob@example.com",
                    "occurred_at": _ts(2024, 1, 2),
                    "thread_id": "t1",
                },
                {
                    "message_id": "<m3@x>",
                    "list": "dev",
                    "sender_raw_value": "carol@example.com",
                    "occurred_at": _ts(2024, 1, 1),
                    "thread_id": "t2",
                },
            ],
        )
        rows = load_dev_messages_for_threads(tmp_path, "dev", {"t1"})
        assert set(rows) == {"t1"}
        assert {r["message_id"] for r in rows["t1"]} == {"<m1@x>", "<m2@x>"}

    def test_empty_thread_id_set_returns_empty_without_reading(self, tmp_path):
        # no partitions written at all -- must not raise FileNotFoundError.
        assert load_dev_messages_for_threads(tmp_path, "dev", set()) == {}

    def test_dedupes_a_message_id_written_by_two_overlapping_collection_runs(self, tmp_path):
        # issue #112 fixup: without this, the same real message would be
        # counted twice in `pending` (private_run.runner), silently
        # double-weighting it in every downstream rate.
        row = {
            "message_id": "<dup@x>",
            "list": "dev",
            "sender_raw_value": "alice@example.com",
            "occurred_at": _ts(2024, 1, 1),
            "thread_id": "t1",
        }
        _write_message(tmp_path, [row], run_id="run-1")
        _write_message(tmp_path, [row], run_id="run-2")
        rows = load_dev_messages_for_threads(tmp_path, "dev", {"t1"})
        assert len(rows["t1"]) == 1
        assert rows["t1"][0]["message_id"] == "<dup@x>"


class TestLoadDevAuthorHistory:
    def test_returns_sorted_timestamps_per_raw_author(self, tmp_path):
        _write_message(
            tmp_path,
            [
                {
                    "message_id": "<m1@x>",
                    "list": "dev",
                    "sender_raw_value": "alice@example.com",
                    "occurred_at": _ts(2024, 1, 3),
                    "thread_id": "t1",
                },
                {
                    "message_id": "<m2@x>",
                    "list": "dev",
                    "sender_raw_value": "alice@example.com",
                    "occurred_at": _ts(2024, 1, 1),
                    "thread_id": "t1",
                },
                {
                    "message_id": "<m3@x>",
                    "list": "dev",
                    "sender_raw_value": "bob@example.com",
                    "occurred_at": _ts(2024, 1, 2),
                    "thread_id": "t1",
                },
            ],
        )
        history = load_dev_author_history(tmp_path, "dev")
        assert history["alice@example.com"] == [_ts(2024, 1, 1), _ts(2024, 1, 3)]
        assert history["bob@example.com"] == [_ts(2024, 1, 2)]

    def test_covers_the_whole_table_not_just_one_thread(self, tmp_path):
        # This is the core requirement issue #114 needs: a message not
        # sampled into any thread this run touches must still contribute
        # to that author's history.
        _write_message(
            tmp_path,
            [
                {
                    "message_id": "<old@x>",
                    "list": "dev",
                    "sender_raw_value": "alice@example.com",
                    "occurred_at": _ts(2018, 1, 1),
                    "thread_id": "ancient-unsampled-thread",
                }
            ],
        )
        history = load_dev_author_history(tmp_path, "dev")
        assert history["alice@example.com"] == [_ts(2018, 1, 1)]

    def test_filters_to_requested_list(self, tmp_path):
        _write_message(
            tmp_path,
            [
                {
                    "message_id": "<m1@x>",
                    "list": "dev",
                    "sender_raw_value": "alice@example.com",
                    "occurred_at": _ts(2024, 1, 1),
                    "thread_id": "t1",
                },
                {
                    "message_id": "<m2@x>",
                    "list": "user",
                    "sender_raw_value": "alice@example.com",
                    "occurred_at": _ts(2024, 1, 1),
                    "thread_id": "t2",
                },
            ],
        )
        history = load_dev_author_history(tmp_path, "dev")
        assert len(history["alice@example.com"]) == 1

    def test_no_partitions_written_yet_returns_empty(self, tmp_path):
        assert load_dev_author_history(tmp_path, "dev") == {}

    def test_dedupes_by_message_id(self, tmp_path):
        row = {
            "message_id": "<dup@x>",
            "list": "dev",
            "sender_raw_value": "alice@example.com",
            "occurred_at": _ts(2024, 1, 1),
            "thread_id": "t1",
        }
        _write_message(tmp_path, [row], run_id="run-1")
        _write_message(tmp_path, [row], run_id="run-2")
        history = load_dev_author_history(tmp_path, "dev")
        assert history["alice@example.com"] == [_ts(2024, 1, 1)]


class TestLoadJiraAuthorHistory:
    def test_returns_sorted_timestamps_per_author_across_issues(self, tmp_path):
        _write_issue_comment(
            tmp_path,
            [
                {
                    "comment_id": "1",
                    "issue_key": "EXAMPLE-1",
                    "author_raw_value": "dave",
                    "created_at": _ts(2024, 1, 3),
                },
                {
                    "comment_id": "2",
                    "issue_key": "EXAMPLE-2",
                    "author_raw_value": "dave",
                    "created_at": _ts(2024, 1, 1),
                },
            ],
        )
        history = load_jira_author_history(tmp_path, "EXAMPLE")
        assert history["dave"] == [_ts(2024, 1, 1), _ts(2024, 1, 3)]

    def test_filters_to_requested_project_key_prefix(self, tmp_path):
        _write_issue_comment(
            tmp_path,
            [
                {
                    "comment_id": "1",
                    "issue_key": "EXAMPLE-1",
                    "author_raw_value": "dave",
                    "created_at": _ts(2024, 1, 1),
                },
                {
                    "comment_id": "2",
                    "issue_key": "OTHER-1",
                    "author_raw_value": "dave",
                    "created_at": _ts(2024, 1, 1),
                },
            ],
        )
        history = load_jira_author_history(tmp_path, "EXAMPLE")
        assert len(history["dave"]) == 1

    def test_no_partitions_written_yet_returns_empty(self, tmp_path):
        assert load_jira_author_history(tmp_path, "EXAMPLE") == {}

    def test_dedupes_by_comment_id(self, tmp_path):
        row = {
            "comment_id": "dup",
            "issue_key": "EXAMPLE-1",
            "author_raw_value": "dave",
            "created_at": _ts(2024, 1, 1),
        }
        _write_issue_comment(tmp_path, [row], run_id="run-1")
        _write_issue_comment(tmp_path, [row], run_id="run-2")
        history = load_jira_author_history(tmp_path, "EXAMPLE")
        assert history["dave"] == [_ts(2024, 1, 1)]
