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

from project_health.peers import collect as peer_collect
from project_health.peers import pipeline as peer_pipeline
from project_health.peers import rotation as peer_rotation
from project_health.peers.config import JiraReleaseVerification, PeerProject, PeersConfig
from project_health.peers.github import PassResult, PassState
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


def _patch_empty_github_passes(monkeypatch) -> None:
    """Every (peer, pass) in this test module's scenarios completes with
    zero rows -- the three pass-runner functions `collect_and_write_github`
    calls (`project_health.peers.github`) are monkeypatched directly,
    replacing the old single `collect_peer_prs`/`GitHubCollector.collect()`
    mock this module used before the issue #145 fixup (orchestrator review
    of PR #147) replaced that single ASC-from-scratch walk with three
    bounded-recency-window passes."""
    monkeypatch.setattr(
        peer_collect, "run_created_desc_pass", lambda *a, **k: _empty_pass_result()
    )
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )


def test_run_peers_collection_writes_snapshots_for_cassandra_and_every_peer(tmp_path, monkeypatch):
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )

    _patch_empty_github_passes(monkeypatch)
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


def test_run_peers_collection_writes_settlement_per_peer(tmp_path, monkeypatch):
    """Issue #150: each peer gets its own `settlement.json`, reflecting the
    watermarks this same run's `collect_and_write_github` just persisted --
    never written for Cassandra (module docstring: it has no three-pass
    peer backfill of its own)."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )

    def fake_created_desc(
        collector, repo_label, state, window_start, bot_patterns, snapshot_id, **kwargs
    ):
        # kafka completes its created_desc backfill this run; spark's is
        # still mid-walk.
        if repo_label == "apache/kafka":
            return PassResult(
                pr_rows=[],
                review_rows=[],
                comment_rows=[],
                bot_prs_excluded=0,
                bot_reviews_excluded=0,
                bot_comments_excluded=0,
                status="completed",
                next_state=PassState(high_watermark="2024-01-01T00:00:00+00:00"),
            )
        return PassResult(
            pr_rows=[],
            review_rows=[],
            comment_rows=[],
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
            status="partial",
            next_state=PassState(cursor="resume-here"),
        )

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_created_desc)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
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
        peers_config, tmp_path, tmp_path / "work", "run1", token="x", as_of=date(2026, 1, 1)
    )

    # kafka: created_desc settled (high_watermark set), open_prs/closed_search
    # settled (completed with cursor cleared) -- fully settled, 3/3.
    assert report.peer_settlement["kafka"] == {
        "created_desc": True,
        "open_prs": True,
        "closed_search": True,
    }
    # spark: created_desc still mid-walk -- unsettled on that one pass.
    assert report.peer_settlement["spark"]["created_desc"] is False

    kafka_settlement_path = (
        tmp_path / "snapshots" / "peers" / "run1" / "kafka" / "settlement.json"
    )
    assert kafka_settlement_path.exists()
    assert json.loads(kafka_settlement_path.read_text())["created_desc"] is True

    # Cassandra never gets a settlement.json -- not a peer, no three-pass
    # backfill state of its own.
    cassandra_settlement_path = (
        tmp_path / "snapshots" / "peers" / "run1" / "cassandra" / "settlement.json"
    )
    assert not cassandra_settlement_path.exists()

    manifest = json.loads((tmp_path / "manifests" / "peers-run1.json").read_text())
    assert manifest["peer_settlement"]["kafka"]["created_desc"] is True
    assert manifest["peer_settlement"]["spark"]["created_desc"] is False


def test_disk_budget_skips_remaining_peers(tmp_path, monkeypatch):
    """issue #145's own disk-budget rule: once free space drops at or below
    `min_free_disk_bytes`, every remaining peer's git/release collection is
    skipped, not attempted."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    )

    _patch_empty_github_passes(monkeypatch)
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


class _FakeCollector:
    """Stand-in for `collectors.github.GitHubCollector` that only records
    the token it was constructed with -- `collect_and_write_github` builds
    exactly one of these per run, so capturing its `token` constructor arg
    is the one place `run_peers_collection`'s own token-resolution
    (fixup, orchestrator review of PR #147: a run that left `token=None`
    ran release-verification's REST calls unauthenticated) is observable
    from outside `peer_collect`."""

    def __init__(self, config, token=None, rate_limit_floor=500, transport=None):
        self.seen_tokens.append(token)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


def _fake_collector_class(seen_tokens: list[str | None]):
    return type("_FakeCollector", (_FakeCollector,), {"seen_tokens": seen_tokens})


def test_resolves_token_when_none_given(tmp_path, monkeypatch):
    """Real-run regression (2026-10-09): `collect_and_write_releases`'
    plain REST calls have no fallback token resolution of their own (unlike
    `GitHubCollector`), so a run that leaves `token=None` must still
    resolve one here -- otherwise release collection runs unauthenticated
    and hits GitHub's 60 req/hr limit after a few dozen GA tags (verified
    live against apache/kafka's own tag history)."""
    peers_config = PeersConfig(peers=[_peer("kafka", "apache/kafka")])

    seen_tokens: list[str | None] = []

    def fake_collect_and_write_releases(*a, token=None, **k):
        seen_tokens.append(token)
        return peer_collect.PeerReleaseCollectionReport(
            peer_id="kafka",
            release_count=0,
            verification=VerificationResult(
                source_type="jira", tag_derived_by_year={}, independent_by_year={}
            ),
        )

    monkeypatch.setattr(peer_collect, "GitHubCollector", _fake_collector_class(seen_tokens))
    _patch_empty_github_passes(monkeypatch)
    monkeypatch.setattr(
        peer_pipeline, "collect_and_write_releases", fake_collect_and_write_releases
    )
    monkeypatch.setattr(peer_collect.git_clone, "clone_shallow_bare", lambda *a, **k: None)
    monkeypatch.setattr(peer_pipeline, "resolve_github_token", lambda: "resolved-token")

    peer_pipeline.run_peers_collection(
        peers_config,
        tmp_path,
        tmp_path / "work",
        "run1",
        as_of=date(2026, 1, 1),
    )

    assert seen_tokens == ["resolved-token", "resolved-token"]


def test_does_not_override_an_explicitly_passed_token(tmp_path, monkeypatch):
    peers_config = PeersConfig(peers=[_peer("kafka", "apache/kafka")])
    seen_tokens: list[str | None] = []

    monkeypatch.setattr(peer_collect, "GitHubCollector", _fake_collector_class(seen_tokens))
    _patch_empty_github_passes(monkeypatch)
    monkeypatch.setattr(peer_collect.git_clone, "clone_shallow_bare", lambda *a, **k: None)
    monkeypatch.setattr(peer_collect, "fetch_ga_tags", lambda *a, **k: [])
    monkeypatch.setattr(
        peer_collect,
        "verify_release_counts",
        lambda *a, **k: VerificationResult(
            source_type="jira", tag_derived_by_year={}, independent_by_year={}
        ),
    )
    def _must_not_be_called() -> str:
        raise AssertionError("resolve_github_token should not be called when token is given")

    monkeypatch.setattr(peer_pipeline, "resolve_github_token", _must_not_be_called)

    peer_pipeline.run_peers_collection(
        peers_config,
        tmp_path,
        tmp_path / "work",
        "run1",
        token="explicit-token",
        as_of=date(2026, 1, 1),
    )

    assert seen_tokens == ["explicit-token"]


def test_latest_peers_run_id_none_when_no_snapshots(tmp_path):
    assert peer_pipeline.latest_peers_run_id(tmp_path) is None


def test_rotation_state_persists_and_advances_across_runs(tmp_path, monkeypatch):
    """issue #148: `run_peers_collection` reads `state/peers/rotation.json`'s
    `next_start_index` before each run and advances it by exactly one peer
    afterward -- a missing file (today's real data branch, 3-4 real runs
    in, never written) starts at 0, and the offset wraps back to 0 once it
    cycles through every peer."""
    peers_config = PeersConfig(
        peers=[
            _peer("kafka", "apache/kafka"),
            _peer("spark", "apache/spark"),
            _peer("flink", "apache/flink"),
        ]
    )

    seen_rotation_indices: list[int] = []

    def fake_collect_and_write_github(*a, **k):
        seen_rotation_indices.append(k.get("rotation_start_index"))
        return peer_collect.PeerGithubCollectionReport(per_peer_pr_counts={}, pass_outcomes=[])

    def fake_collect_and_write_git(peer, *a, **k):
        return peer_collect.PeerGitCollectionReport(
            peer_id=peer.id, commits_collected=0, clone_bytes=0
        )

    def fake_collect_and_write_releases(peer, *a, **k):
        return peer_collect.PeerReleaseCollectionReport(
            peer_id=peer.id,
            release_count=0,
            verification=VerificationResult(
                source_type="jira", tag_derived_by_year={}, independent_by_year={}
            ),
        )

    monkeypatch.setattr(peer_pipeline, "collect_and_write_github", fake_collect_and_write_github)
    monkeypatch.setattr(peer_pipeline, "collect_and_write_git", fake_collect_and_write_git)
    monkeypatch.setattr(
        peer_pipeline, "collect_and_write_releases", fake_collect_and_write_releases
    )

    # No prior rotation state -- the real data branch's current situation.
    assert peer_rotation.read_next_start_index(tmp_path) == 0

    peer_pipeline.run_peers_collection(
        peers_config, tmp_path, tmp_path / "work", "run1", token="x", as_of=date(2026, 1, 1)
    )
    assert seen_rotation_indices == [0]
    assert peer_rotation.read_next_start_index(tmp_path) == 1

    peer_pipeline.run_peers_collection(
        peers_config, tmp_path, tmp_path / "work", "run2", token="x", as_of=date(2026, 1, 1)
    )
    assert seen_rotation_indices == [0, 1]
    assert peer_rotation.read_next_start_index(tmp_path) == 2

    peer_pipeline.run_peers_collection(
        peers_config, tmp_path, tmp_path / "work", "run3", token="x", as_of=date(2026, 1, 1)
    )
    # Wraps back to 0 once the offset has cycled through every one of the
    # 3 configured peers.
    peer_pipeline.run_peers_collection(
        peers_config, tmp_path, tmp_path / "work", "run4", token="x", as_of=date(2026, 1, 1)
    )
    assert seen_rotation_indices == [0, 1, 2, 0]


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


def _release_row(release_id: str, collected_at: datetime) -> dict:
    return {
        "release_id": release_id,
        "tag_name": release_id,
        "version": release_id,
        "major_minor": "1.0",
        "release_date": date(2024, 1, 1),
        "release_date_source": "git_tag",
        "archive_verified": None,
        "archive_date": None,
        "repo": "apache/kafka",
        "source_snapshot_id": "s1",
        "collected_at": collected_at,
    }


def test_peer_tables_dedupes_release_rows_written_by_two_runs(tmp_path):
    """Real-run regression (2026-10-09): `collect_and_write_releases` has
    no watermark -- it re-fetches and re-writes every GA tag on every run
    (same full-refresh design `collectors.release` documents), so the same
    `release_id` legitimately lands in more than one partition. Reading
    without dedup inflated every peer's release count ~2x after a second
    collection run."""
    from project_health.peers.collect import peer_source

    source = peer_source("kafka", "release")
    table1 = pa.Table.from_pylist(
        [_release_row("4.0.0", datetime(2026, 1, 1, tzinfo=timezone.utc))],
        schema=get_schema("release"),
    )
    table2 = pa.Table.from_pylist(
        [_release_row("4.0.0", datetime(2026, 1, 2, tzinfo=timezone.utc))],
        schema=get_schema("release"),
    )
    storage.write_partition(tmp_path, source, "release", date(2026, 1, 1), "run1", table1)
    storage.write_partition(tmp_path, source, "release", date(2026, 1, 2), "run2", table2)

    tables = peer_pipeline._peer_tables(tmp_path, "kafka")
    assert tables["release"].num_rows == 1
