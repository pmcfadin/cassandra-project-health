"""Tests for project_health.peers.pipeline (issue #145).

Exercises `run_peers_collection` end to end against a tmp data dir, with
every network/clone-touching function monkeypatched -- no real HTTP calls,
no real clones.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa

from project_health.collectors.github import GitHubCollectionResult, GitHubRepoOutcome
from project_health.peers import collect as peer_collect
from project_health.peers import pipeline as peer_pipeline
from project_health.peers.config import JiraReleaseVerification, PeerProject, PeersConfig
from project_health.peers.release import VerificationResult
from project_health.schema import get_schema
from project_health import storage


def _peer(peer_id: str, repo: str) -> PeerProject:
    return PeerProject(
        id=peer_id,
        display_name=peer_id,
        repo=repo,
        default_branch="main",
        tag_prefix="",
        release_verification=JiraReleaseVerification(base_url="https://x", project_key="X"),
    )


def _empty_github_result(repos: list[str]) -> GitHubCollectionResult:
    return GitHubCollectionResult(
        prs=get_schema("pr").empty_table(),
        reviews=get_schema("pr_review").empty_table(),
        comments=get_schema("pr_comment").empty_table(),
        repos={
            repo: GitHubRepoOutcome(
                repo=repo,
                status="ok",
                next_watermark="cursor",
                pr_count=0,
                review_count=0,
                comment_count=0,
                bot_prs_excluded=0,
                bot_reviews_excluded=0,
                bot_comments_excluded=0,
            )
            for repo in repos
        },
        status="ok",
    )


def test_run_peers_collection_writes_snapshots_for_cassandra_and_every_peer(tmp_path, monkeypatch):
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )

    monkeypatch.setattr(
        peer_collect,
        "collect_peer_prs",
        lambda *a, **k: _empty_github_result(["apache/kafka", "apache/spark"]),
    )
    monkeypatch.setattr(peer_collect.git_clone, "clone_shallow_bare", lambda *a, **k: None)
    monkeypatch.setattr(peer_collect, "fetch_ga_tags", lambda *a, **k: [])
    monkeypatch.setattr(
        peer_collect,
        "verify_release_counts",
        lambda *a, **k: VerificationResult(
            source_type="jira", tag_derived_by_year={}, independent_by_year={}
        ),
    )

    # GitCollector.collect will be called against whatever path
    # `clone_shallow_bare` "created" (a no-op above) -- make it a real,
    # empty git repo so `GitCollector.collect` has something valid to walk.
    import subprocess
    import os

    def fake_clone(remote_url, local_path, *, branch, shallow_since):
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            }
        )
        Path(local_path).mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-b", branch], cwd=local_path, env=env, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=local_path,
            check=True,
            capture_output=True,
        )
        (Path(local_path) / "f.txt").write_text("x")
        subprocess.run(
            ["git", "-C", str(local_path), "add", "f.txt"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(local_path), "commit", "-m", "init"],
            env=env,
            check=True,
            capture_output=True,
        )

    monkeypatch.setattr(peer_collect.git_clone, "clone_shallow_bare", fake_clone)

    report = peer_pipeline.run_peers_collection(
        peers_config,
        tmp_path,
        tmp_path / "work",
        "run1",
        token="x",
        as_of=date(2026, 1, 1),
    )

    assert report.metrics_by_project.keys() == {"cassandra", "kafka", "spark"}

    for project_id in ("cassandra", "kafka", "spark"):
        snapshot_dir = tmp_path / "snapshots" / "peers" / "run1" / project_id
        assert (snapshot_dir / "metric_value.parquet").exists()
        assert (snapshot_dir / "pr_backlog.parquet").exists()

    manifest_path = tmp_path / "manifests" / "peers-run1.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["run_id"] == "run1"

    assert peer_pipeline.latest_peers_run_id(tmp_path) == "run1"


def test_disk_budget_skips_remaining_peers(tmp_path, monkeypatch):
    """issue #145's own disk-budget rule: once free space drops at or below
    `min_free_disk_bytes`, every remaining peer's git/release collection is
    skipped, not attempted."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )

    monkeypatch.setattr(
        peer_collect,
        "collect_peer_prs",
        lambda *a, **k: _empty_github_result(["apache/kafka", "apache/spark"]),
    )
    monkeypatch.setattr(peer_collect, "fetch_ga_tags", lambda *a, **k: [])
    monkeypatch.setattr(
        peer_collect,
        "verify_release_counts",
        lambda *a, **k: VerificationResult(
            source_type="jira", tag_derived_by_year={}, independent_by_year={}
        ),
    )
    clone_calls: list[str] = []
    monkeypatch.setattr(
        peer_collect.git_clone,
        "clone_shallow_bare",
        lambda remote_url, local_path, *, branch, shallow_since: clone_calls.append(remote_url),
    )

    class _FakeUsage:
        free = 1  # effectively zero free space

    monkeypatch.setattr(peer_pipeline.shutil, "disk_usage", lambda path: _FakeUsage())

    report = peer_pipeline.run_peers_collection(
        peers_config,
        tmp_path,
        tmp_path / "work",
        "run1",
        token="x",
        as_of=date(2026, 1, 1),
        min_free_disk_bytes=2 * 1024**3,
    )

    assert clone_calls == []
    assert report.disk_budget_skipped_peers == ["kafka", "spark"]
    assert report.git_reports == []


def test_latest_peers_run_id_none_when_no_snapshots(tmp_path):
    assert peer_pipeline.latest_peers_run_id(tmp_path) is None


def test_cassandra_tables_for_comparison_filters_to_primary_repo(tmp_path):
    pr_rows = [
        {
            "repo": "apache/cassandra",
            "number": 1,
            "state": "OPEN",
            "is_draft": False,
            "merged": False,
            "author_identity_id": None,
            "author_raw_type": "github_login",
            "author_raw_value": "alice",
            "title_hash": "x" * 10,
            "linked_issue_keys": [],
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "closed_at": None,
            "merged_at": None,
            "additions": 1,
            "deletions": 1,
            "changed_files": 1,
            "source_snapshot_id": "s1",
        },
        {
            "repo": "apache/cassandra-dtest",
            "number": 1,
            "state": "OPEN",
            "is_draft": False,
            "merged": False,
            "author_identity_id": None,
            "author_raw_type": "github_login",
            "author_raw_value": "bob",
            "title_hash": "y" * 10,
            "linked_issue_keys": [],
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "closed_at": None,
            "merged_at": None,
            "additions": 1,
            "deletions": 1,
            "changed_files": 1,
            "source_snapshot_id": "s1",
        },
    ]
    pr_table = pa.Table.from_pylist(pr_rows, schema=get_schema("pr"))
    storage.write_partition(tmp_path, "github", "pr", date(2026, 1, 1), "run0", pr_table)

    tables = peer_pipeline._cassandra_tables_for_comparison(tmp_path)
    assert tables["pr"].num_rows == 1
    assert tables["pr"].column("repo").to_pylist() == ["apache/cassandra"]
