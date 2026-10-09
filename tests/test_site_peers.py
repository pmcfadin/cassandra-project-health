"""Tests for /peers/ page wiring in project_health.site.generate (issue #145).

Builds a minimal site the same way `tests/test_site.py` does, with a
synthetic `snapshots/peers/<run_id>/<project>/...` tree added, and checks
the page is generated, linked from nav and Community, and absent/no dangling
link when no peer snapshot exists yet.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.metrics.peer_metrics import compute_peer_metrics
from project_health.schema import get_schema, validate
from project_health.site.generate import generate

RUN_ID = "2026-10-09-abc123"
CODE_SHA = "abc123def4567890abc123def4567890abc1234"
BUILD_TIME = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _metric_value_table(rows: list[dict]) -> pa.Table:
    schema = get_schema("metric_value")
    if not rows:
        return schema.empty_table()
    return validate("metric_value", pa.Table.from_pylist(rows, schema=schema))


def _write_main_snapshot(data_dir: Path) -> None:
    snapshot_dir = data_dir / "snapshots" / RUN_ID
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(_metric_value_table([]), snapshot_dir / "metrics.parquet")


def _write_manifest(data_dir: Path) -> None:
    import json

    manifest = {
        "run_id": RUN_ID,
        "trigger": "schedule",
        "started_at": "2026-10-09T06:17:00Z",
        "completed_at": (BUILD_TIME - timedelta(hours=1)).isoformat(),
        "pipeline_code_sha": CODE_SHA,
        "sources": {},
        "metrics_computed": [],
        "metrics_skipped_insufficient_data": [],
        "data_branch_commit": "d4e5f6",
        "site_deploy_status": "ok",
    }
    manifests_dir = data_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{RUN_ID}.json").write_text(json.dumps(manifest))


def _synthetic_peer_tables(repo: str) -> dict[str, pa.Table]:
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
            "created_at": datetime(2024, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2024, 1, 2, tzinfo=UTC),
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
    return {
        "pr": pr,
        "pr_review": get_schema("pr_review").empty_table(),
        "pr_comment": get_schema("pr_comment").empty_table(),
        "contribution_event": get_schema("contribution_event").empty_table(),
        "release": get_schema("release").empty_table(),
    }


def _write_peers_snapshot(data_dir: Path) -> None:
    for project_id, repo in [
        ("cassandra", "apache/cassandra"),
        ("kafka", "apache/kafka"),
        ("spark", "apache/spark"),
        ("flink", "apache/flink"),
        ("pulsar", "apache/pulsar"),
        ("datafusion", "apache/datafusion"),
    ]:
        tables = _synthetic_peer_tables(repo)
        computed = compute_peer_metrics(
            tables, as_of=date(2024, 6, 1), run_id="peers-run1", computed_at=BUILD_TIME
        )
        out_dir = data_dir / "snapshots" / "peers" / "peers-run1" / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(computed["metric_value"], out_dir / "metric_value.parquet")
        pq.write_table(computed["pr_backlog"], out_dir / "pr_backlog.parquet")


def test_no_peers_snapshot_no_page_no_nav_link(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_main_snapshot(data_dir)
    _write_manifest(data_dir)

    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)

    assert not (out_dir / "peers" / "index.html").exists()
    home_html = (out_dir / "index.html").read_text()
    assert 'href="./peers/"' not in home_html


def test_peers_page_generated_when_snapshot_present(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_main_snapshot(data_dir)
    _write_manifest(data_dir)
    _write_peers_snapshot(data_dir)

    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)

    peers_html = (out_dir / "peers" / "index.html").read_text()
    assert "Peer context" in peers_html
    assert "CHAOSS does not set targets or rank projects" in peers_html
    assert "Apache Kafka" in peers_html

    # Nav link present on every page once peers data exists.
    home_html = (out_dir / "index.html").read_text()
    assert 'href="./peers/"' in home_html
    community_html = (out_dir / "community" / "index.html").read_text()
    assert 'href="../peers/"' in community_html
    assert "Peer context" in community_html
