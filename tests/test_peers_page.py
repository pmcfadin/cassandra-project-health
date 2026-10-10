"""Tests for project_health.site.peers_page (issue #145)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from project_health.metrics.peer_metrics import TIME_TO_FIRST_RESPONSE_PR, compute_peer_metrics
from project_health.schema import get_schema
from project_health.site.peers_page import CHAOSS_DISCLAIMER, build_peers_context

REPO = "apache/kafka"


def _synthetic_tables(repo: str) -> dict[str, pa.Table]:
    # 6 PRs, all created in the same month, each with a same-month comment
    # response -- enough to clear METRICS.md §0.6's rate/ratio floor (5),
    # same reasoning `test_peer_metrics.py`'s own fixtures document, so the
    # chart actually has a non-null point to render.
    pr_rows = [
        {
            "repo": repo,
            "number": i,
            "state": "OPEN",
            "is_draft": False,
            "merged": False,
            "author_identity_id": None,
            "author_raw_type": "github_login",
            "author_raw_value": f"author{i}",
            "title_hash": "x" * 10,
            "linked_issue_keys": [],
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "closed_at": None,
            "merged_at": None,
            "additions": 1,
            "deletions": 1,
            "changed_files": 1,
            "source_snapshot_id": "s1",
        }
        for i in range(1, 7)
    ]
    pr = pa.Table.from_pylist(pr_rows, schema=get_schema("pr"))

    comment_rows = [
        {
            "comment_id": f"c{i}",
            "repo": repo,
            "pr_number": i,
            "review_id": None,
            "comment_type": "issue_comment",
            "author_identity_id": None,
            "author_raw_type": "github_login",
            "author_raw_value": "commenter1",
            "created_at": datetime(2024, 1, 3, tzinfo=timezone.utc),
            "source_snapshot_id": "s1",
        }
        for i in range(1, 7)
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
            "repo": repo,
            "source_ref": f"sha{i}",
            "source_snapshot_id": "s1",
        }
        for i in range(15)
    ]
    contribution_event = pa.Table.from_pylist(contrib_rows, schema=get_schema("contribution_event"))

    return {
        "pr": pr,
        "pr_review": get_schema("pr_review").empty_table(),
        "pr_comment": pr_comment,
        "contribution_event": contribution_event,
        "release": get_schema("release").empty_table(),
    }


def _write_snapshot(tmp_path, run_id: str, project_id: str, repo: str) -> None:
    tables = _synthetic_tables(repo)
    computed = compute_peer_metrics(
        tables,
        as_of=date(2025, 1, 1),
        run_id=run_id,
        computed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    out_dir = tmp_path / "snapshots" / "peers" / run_id / project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(computed["metric_value"], out_dir / "metric_value.parquet")
    pq.write_table(computed["pr_backlog"], out_dir / "pr_backlog.parquet")


@pytest.fixture
def populated_data_dir(tmp_path):
    _write_snapshot(tmp_path, "run1", "cassandra", "apache/cassandra")
    _write_snapshot(tmp_path, "run1", "kafka", "apache/kafka")
    _write_snapshot(tmp_path, "run1", "spark", "apache/spark")
    _write_snapshot(tmp_path, "run1", "flink", "apache/flink")
    _write_snapshot(tmp_path, "run1", "pulsar", "apache/pulsar")
    _write_snapshot(tmp_path, "run1", "datafusion", "apache/datafusion")
    return tmp_path


def test_no_snapshot_is_unavailable_not_an_error(tmp_path):
    context = build_peers_context(tmp_path)
    assert context["available"] is False
    assert context["chaoss_disclaimer"] == CHAOSS_DISCLAIMER


def test_available_with_snapshot(populated_data_dir):
    context = build_peers_context(populated_data_dir, run_id="run1")
    assert context["available"] is True
    assert context["chaoss_disclaimer"] == CHAOSS_DISCLAIMER
    assert len(context["table_rows"]) == 6
    assert context["table_rows"][0]["project_id"] == "cassandra"
    assert context["table_rows"][0]["is_cassandra"] is True


def test_every_peer_appears_in_at_least_one_chart(populated_data_dir):
    context = build_peers_context(populated_data_dir, run_id="run1")
    time_to_first_response_section = next(
        c for c in context["peer_sections"] if c["id"] == TIME_TO_FIRST_RESPONSE_PR
    )
    assert time_to_first_response_section["chart_spec"] is not None
    assert time_to_first_response_section["chaoss_label"] == "CHAOSS: Time to First Response"
    import json

    spec = json.loads(time_to_first_response_section["chart_spec"])
    projects_in_chart = {v["project"] for v in spec["data"]["values"]}
    assert "Apache Cassandra" in projects_in_chart
    assert "Apache Kafka" in projects_in_chart

    # Every project gets a summary-row headline item, named, even one with
    # no qualifying months this run (issue #144's "section title, CHAOSS
    # topic link, headline numbers" pattern, reused unchanged for #145).
    summary_names = {item["name"] for item in time_to_first_response_section["summary_items"]}
    assert summary_names == {
        "Apache Cassandra",
        "Apache Kafka",
        "Apache Spark",
        "Apache Flink",
        "Apache Pulsar",
        "Apache DataFusion",
    }


def test_backlog_total_chart_present(populated_data_dir):
    context = build_peers_context(populated_data_dir, run_id="run1")
    backlog_section = next(
        c for c in context["peer_sections"] if c["id"] == "open_pr_backlog_total"
    )
    assert backlog_section["chart_spec"] is not None
    assert backlog_section["chaoss_label"] == "CHAOSS: Change Requests (adapted)"


def test_latest_run_id_used_when_not_given(populated_data_dir):
    context = build_peers_context(populated_data_dir)
    assert context["available"] is True
    assert context["peers_run_id"] == "run1"
