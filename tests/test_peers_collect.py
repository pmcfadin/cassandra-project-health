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


from project_health.peers import collect as peer_collect
from project_health.peers.config import JiraReleaseVerification, PeerProject, PeersConfig
from project_health.peers.github import PassResult, PassState
from project_health.peers.release import GaTag, VerificationResult
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


def _pr_row(repo: str, number: int, author: str) -> dict:
    return {
        "repo": repo,
        "number": number,
        "state": "OPEN",
        "is_draft": False,
        "merged": False,
        "author_identity_id": None,
        "author_raw_type": "github_login",
        "author_raw_value": author,
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
    }


def _empty_pass_result() -> PassResult:
    return PassResult(
        pr_rows=[],
        review_rows=[],
        comment_rows=[],
        bot_prs_excluded=0,
        bot_reviews_excluded=0,
        bot_comments_excluded=0,
        status="completed",
        next_state=PassState(),
    )


def test_collect_and_write_github_splits_by_repo(tmp_path, monkeypatch):
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )

    def fake_created_desc(
        collector, repo_label, state, window_start, bot_patterns, snapshot_id, **kwargs
    ):
        author = "alice" if repo_label == "apache/kafka" else "bob"
        return PassResult(
            pr_rows=[_pr_row(repo_label, 1, author)],
            review_rows=[],
            comment_rows=[],
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
            status="completed",
            next_state=PassState(high_watermark="2024-01-01T00:00:00+00:00"),
        )

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_created_desc)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    report = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
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

    # Each pass's resumable state is persisted per peer.
    stored = storage.read_watermark(tmp_path, kafka_github_source, table="pr_pass_created_desc")
    assert stored is not None
    assert PassState.from_json(stored).high_watermark == "2024-01-01T00:00:00+00:00"


def test_collect_and_write_github_stops_cleanly_on_budget_floor(tmp_path, monkeypatch):
    """Once any (peer, pass) hits the rate-limit floor, every remaining
    (peer, pass) pair is recorded 'skipped' with its watermark untouched --
    same shared-budget discipline the GraphQL collector itself documents."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )

    def fake_created_desc(
        collector, repo_label, state, window_start, bot_patterns, snapshot_id, **kwargs
    ):
        return PassResult(
            pr_rows=[_pr_row(repo_label, 1, "alice")],
            review_rows=[],
            comment_rows=[],
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
            status="partial",
            next_state=PassState(cursor="resume-here"),
        )

    calls: list[str] = []

    def tracking_open_prs(*a, **k):
        calls.append("open_prs")
        return _empty_pass_result()

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_created_desc)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", tracking_open_prs)
    monkeypatch.setattr(peer_collect, "run_closed_search_pass", tracking_open_prs)

    report = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
    )

    # kafka's created_desc pass hit the floor; every later (peer, pass) --
    # including kafka's own remaining passes and all of spark's -- skipped.
    assert calls == []
    statuses = {(o.peer_id, o.pass_name): o.status for o in report.pass_outcomes}
    assert statuses[("kafka", "created_desc")] == "partial"
    assert statuses[("kafka", "open_prs")] == "skipped"
    assert statuses[("spark", "created_desc")] == "skipped"

    # The partial pass's resume cursor was persisted; skipped passes never touch storage.
    kafka_github_source = peer_collect.peer_source("kafka", "github")
    stored = storage.read_watermark(tmp_path, kafka_github_source, table="pr_pass_created_desc")
    assert PassState.from_json(stored).cursor == "resume-here"


def test_page_cap_does_not_block_other_peers(tmp_path, monkeypatch):
    """Real-run regression (2026-10-09, fixup round 1): apache/kafka's own
    `created_desc` pass alone consumed the *entire* shared GraphQL budget
    (96 pages) before reaching its window-start stop condition, leaving
    every other peer's every pass 'skipped' for the whole run. A page cap
    reached ('page_capped') must NOT set the same 'budget exhausted,
    skip everyone else' flag the real rate-limit floor ('partial') does --
    spark must still get its own turn."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )

    calls: list[str] = []

    def fake_created_desc(
        collector, repo_label, state, window_start, bot_patterns, snapshot_id, **kwargs
    ):
        calls.append(f"created_desc:{repo_label}")
        return PassResult(
            pr_rows=[_pr_row(repo_label, 1, "alice")],
            review_rows=[],
            comment_rows=[],
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
            status="page_capped",
            next_state=PassState(cursor="resume-here"),
        )

    def fake_other_pass(collector, repo_label, *a, **k):
        calls.append(f"other:{repo_label}")
        return _empty_pass_result()

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_created_desc)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", fake_other_pass)
    monkeypatch.setattr(peer_collect, "run_closed_search_pass", fake_other_pass)

    report = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
    )

    # Both peers' created_desc pass ran -- page-capping kafka's did not
    # skip spark the way a real rate-limit floor hit would.
    assert "created_desc:apache/kafka" in calls
    assert "created_desc:apache/spark" in calls
    statuses = {(o.peer_id, o.pass_name): o.status for o in report.pass_outcomes}
    assert statuses[("kafka", "created_desc")] == "page_capped"
    assert statuses[("spark", "created_desc")] == "page_capped"
    assert "skipped" not in statuses.values()
    # Resumable: still-incomplete passes have their cursor persisted.
    kafka_source = peer_collect.peer_source("kafka", "github")
    stored = storage.read_watermark(tmp_path, kafka_source, table="pr_pass_created_desc")
    assert PassState.from_json(stored).cursor == "resume-here"


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
