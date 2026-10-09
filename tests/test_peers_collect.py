"""Tests for project_health.peers.collect (issue #145).

`collect_and_write_git` is exercised against a small, deterministic local
git repo built fresh into `tmp_path` (monkeypatching
`git_clone.clone_shallow_bare` to build it in place of a real network
clone) -- same "no network dependency" discipline
`tests/test_git_collector.py`/`tests/test_release_collector.py` use.
`collect_and_write_github`/`collect_and_write_releases` monkeypatch the
network-touching functions they call directly.
"""

from __future__ import annotations

import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa

from project_health.collectors.github import GitHubCollectionResult, GitHubRepoOutcome
from project_health.peers import collect as peer_collect
from project_health.peers.config import JiraReleaseVerification, PeerProject, PeersConfig
from project_health.peers.release import GaTag, VerificationResult
from project_health.schema import get_schema
from project_health import storage


def _peer(peer_id: str, repo: str, branch: str = "main") -> PeerProject:
    return PeerProject(
        id=peer_id,
        display_name=peer_id,
        repo=repo,
        default_branch=branch,
        tag_prefix="",
        release_verification=JiraReleaseVerification(base_url="https://x", project_key="X"),
    )


def _pr_result(repo_a: str, repo_b: str) -> GitHubCollectionResult:
    pr_rows = [
        {
            "repo": repo_a,
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
            "repo": repo_b,
            "number": 1,
            "state": "OPEN",
            "is_draft": False,
            "merged": False,
            "author_identity_id": None,
            "author_raw_type": "github_login",
            "author_raw_value": "bob",
            "title_hash": "y" * 10,
            "linked_issue_keys": [],
            "created_at": datetime(2024, 2, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2024, 2, 1, tzinfo=timezone.utc),
            "closed_at": None,
            "merged_at": None,
            "additions": 1,
            "deletions": 1,
            "changed_files": 1,
            "source_snapshot_id": "s1",
        },
    ]
    prs = pa.Table.from_pylist(pr_rows, schema=get_schema("pr"))
    reviews = get_schema("pr_review").empty_table()
    comments = get_schema("pr_comment").empty_table()
    repos = {
        repo_a: GitHubRepoOutcome(
            repo=repo_a,
            status="ok",
            next_watermark="cursor-a",
            pr_count=1,
            review_count=0,
            comment_count=0,
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
        ),
        repo_b: GitHubRepoOutcome(
            repo=repo_b,
            status="ok",
            next_watermark="cursor-b",
            pr_count=1,
            review_count=0,
            comment_count=0,
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
        ),
    }
    return GitHubCollectionResult(
        prs=prs, reviews=reviews, comments=comments, repos=repos, status="ok"
    )


def test_collect_and_write_github_splits_by_repo(tmp_path, monkeypatch):
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )
    result = _pr_result("apache/kafka", "apache/spark")

    monkeypatch.setattr(peer_collect, "collect_peer_prs", lambda *a, **k: result)

    report = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1)
    )

    assert report.per_peer_pr_counts == {"kafka": 1, "spark": 1}
    kafka_github_source = peer_collect.peer_source("kafka", "github")
    spark_github_source = peer_collect.peer_source("spark", "github")
    kafka_pr = storage.read_table(tmp_path, kafka_github_source, "pr")
    assert kafka_pr.num_rows == 1
    assert kafka_pr.column("repo").to_pylist() == ["apache/kafka"]
    spark_pr = storage.read_table(tmp_path, spark_github_source, "pr")
    assert spark_pr.num_rows == 1
    assert spark_pr.column("repo").to_pylist() == ["apache/spark"]

    assert storage.read_watermark(tmp_path, kafka_github_source) == "cursor-a"
    assert storage.read_watermark(tmp_path, spark_github_source) == "cursor-b"


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _build_fixture_repo(repo_path: Path, branch: str = "main") -> None:
    env = _git_env()
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", branch], cwd=repo_path, env=env, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    for i in range(3):
        (repo_path / "file.txt").write_text(f"version {i}\n")
        subprocess.run(
            ["git", "-C", str(repo_path), "add", "file.txt"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m", f"commit {i}"],
            env=env,
            check=True,
            capture_output=True,
        )


def test_collect_and_write_git_cleans_up_clone(tmp_path, monkeypatch):
    peer = _peer("kafka", "apache/kafka", branch="main")
    workdir = tmp_path / "work"

    def fake_clone(remote_url, local_path, *, branch, shallow_since):
        _build_fixture_repo(Path(local_path), branch=branch)

    monkeypatch.setattr(peer_collect.git_clone, "clone_shallow_bare", fake_clone)

    report = peer_collect.collect_and_write_git(
        peer, 48, tmp_path, workdir, "run1", date(2026, 1, 1), []
    )

    assert report.error is None
    assert report.commits_collected == 3
    # Clone must be deleted after collection (issue #145 disk budget).
    assert not (workdir / "kafka-clone").exists()

    contrib = storage.read_table(
        tmp_path, peer_collect.peer_source("kafka", "git"), "contribution_event"
    )
    assert contrib.num_rows == 3
    assert contrib.column("repo").to_pylist() == ["apache/kafka"] * 3


def test_collect_and_write_git_reports_clone_failure(tmp_path, monkeypatch):
    peer = _peer("kafka", "apache/kafka")
    workdir = tmp_path / "work"

    def fake_clone(remote_url, local_path, *, branch, shallow_since):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(peer_collect.git_clone, "clone_shallow_bare", fake_clone)

    report = peer_collect.collect_and_write_git(
        peer, 48, tmp_path, workdir, "run1", date(2026, 1, 1), []
    )
    assert report.error is not None
    assert report.commits_collected == 0


def test_collect_and_write_releases_writes_partition_and_verifies(tmp_path, monkeypatch):
    peer = _peer("kafka", "apache/kafka")
    tags = [
        GaTag(tag_name="4.0.0", version="4.0.0", major_minor="4.0", release_date=date(2024, 1, 1))
    ]
    verification = VerificationResult(
        source_type="jira", tag_derived_by_year={2024: 1}, independent_by_year={2024: 1}
    )

    monkeypatch.setattr(peer_collect, "fetch_ga_tags", lambda *a, **k: tags)
    monkeypatch.setattr(peer_collect, "verify_release_counts", lambda *a, **k: verification)

    report = peer_collect.collect_and_write_releases(peer, tmp_path, "run1", date(2026, 1, 1))

    assert report.release_count == 1
    assert report.verification.tag_derived_by_year == {2024: 1}
    release_source = peer_collect.peer_source("kafka", "release")
    release_table = storage.read_table(tmp_path, release_source, "release")
    assert release_table.num_rows == 1
    assert release_table.column("repo").to_pylist() == ["apache/kafka"]
