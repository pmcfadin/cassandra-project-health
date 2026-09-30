"""Tests for project_health.private_run.thread_export (issue #122, D27:
the public per-thread export, `--out/threads.jsonl`)."""

from __future__ import annotations

import json

from project_health.private_run.thread_export import build_thread_row, write_threads_jsonl


def _row(**overrides):
    base = dict(
        venue="mailing_list",
        thread_key="abc123",
        url="https://lists.apache.org/thread/abc123",
        subject="[DISCUSS] Something",
        started_at="2024-01-01T00:00:00+00:00",
        quarter="2024Q1",
        n_messages=3,
        n_distinct_participants=2,
        escalation=False,
        deescalation=False,
        constructive_resolution=True,
        abandonment_after_friction=False,
        pile_on=False,
        peak_intensity_tier=1,
        label_counts={"technical_disagreement": 2},
    )
    base.update(overrides)
    return build_thread_row(**base)


class TestBuildThreadRow:
    def test_shape_and_outcome_flags(self):
        row = _row()
        assert row["venue"] == "mailing_list"
        assert row["thread_key"] == "abc123"
        assert row["url"] == "https://lists.apache.org/thread/abc123"
        assert row["subject"] == "[DISCUSS] Something"
        assert row["started_at"] == "2024-01-01T00:00:00+00:00"
        assert row["quarter"] == "2024Q1"
        assert row["n_messages"] == 3
        assert row["n_distinct_participants"] == 2
        assert row["outcome"] == {
            "escalation": False,
            "deescalation": False,
            "constructive_resolution": True,
            "abandonment_after_friction": False,
            "pile_on": False,
        }
        assert row["peak_intensity_tier"] == 1
        assert row["label_counts"] == {"technical_disagreement": 2}

    def test_label_counts_sorted(self):
        row = _row(label_counts={"hostility": 1, "dismissiveness": 3, "acknowledgment": 2})
        assert list(row["label_counts"].keys()) == ["acknowledgment", "dismissiveness", "hostility"]

    def test_subject_and_started_at_may_be_none(self):
        row = _row(subject=None, started_at=None)
        assert row["subject"] is None
        assert row["started_at"] is None


class TestWriteThreadsJsonl:
    def test_writes_one_json_line_per_row_sorted(self, tmp_path):
        rows = [
            _row(venue="jira_comment", thread_key="CASSANDRA-2", quarter="2024Q1",
                 url="https://issues.apache.org/jira/browse/CASSANDRA-2"),
            _row(venue="mailing_list", thread_key="aaa", quarter="2024Q1"),
            _row(venue="jira_comment", thread_key="CASSANDRA-1", quarter="2024Q1",
                 url="https://issues.apache.org/jira/browse/CASSANDRA-1"),
        ]
        path = write_threads_jsonl(tmp_path, rows)
        assert path == tmp_path / "threads.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        parsed = [json.loads(line) for line in lines]
        keys = [(r["venue"], r["quarter"], r["thread_key"]) for r in parsed]
        assert keys == sorted(keys)
        assert keys[0][0] == "jira_comment"  # jira_comment sorts before mailing_list

    def test_rebuilds_from_scratch_not_append(self, tmp_path):
        write_threads_jsonl(tmp_path, [_row(thread_key="t1")])
        path = write_threads_jsonl(tmp_path, [_row(thread_key="t2")])
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["thread_key"] == "t2"

    def test_custom_filename(self, tmp_path):
        path = write_threads_jsonl(tmp_path, [_row()], filename="custom-threads.jsonl")
        assert path.name == "custom-threads.jsonl"
        assert path.exists()
