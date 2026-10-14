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
from project_health.peers.config import (
    CollectionConfig,
    JiraReleaseVerification,
    PeerProject,
    PeersConfig,
)
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


def _patch_empty_peer_collection(monkeypatch) -> None:
    """Every (peer, pass) completes with zero rows (`_patch_empty_github_
    passes`), every peer's git clone is a real, empty, throwaway repo (so
    `GitCollector.collect` has something valid to walk), and every peer's
    release discovery/verification is a no-op -- the shared fixture used by
    tests below that don't care about the collected content itself, only
    about which directory gets read/written where (issue #176)."""
    import os
    import subprocess

    _patch_empty_github_passes(monkeypatch)

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
    monkeypatch.setattr(peer_collect, "fetch_ga_tags", lambda *a, **k: [])
    monkeypatch.setattr(
        peer_collect,
        "verify_release_counts",
        lambda *a, **k: VerificationResult(
            source_type="jira", tag_derived_by_year={}, independent_by_year={}
        ),
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


def test_sticky_settlement_survives_across_runs_via_run_peers_collection(tmp_path, monkeypatch):
    """End-to-end version of the real backfill bug (issue #150 fixup):
    `open_prs` completes cleanly on run 1, then run 2's own redo of that
    same pass (`open_prs` is "cheap to redo in full," `peers/github.py`'s
    own module docstring) goes `page_capped` -- the resulting settlement
    must stay `True`, never flip back to `False` just because a refresh
    happens to be in progress. `skip_rerun_within_hours=None` so run 2
    actually calls the pass runner again rather than skipping it as
    "recently completed" (a separate optimization, not what this test is
    checking)."""
    peer = _peer("kafka", "apache/kafka")
    peers_config = PeersConfig(
        peers=[peer], collection=CollectionConfig(skip_rerun_within_hours=None)
    )

    _patch_empty_peer_collection(monkeypatch)

    call_count = {"n": 0}

    def fake_open_prs(collector, repo_label, state, bot_patterns, snapshot_id, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _empty_pass_result()  # run 1: completes cleanly
        return PassResult(
            pr_rows=[],
            review_rows=[],
            comment_rows=[],
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
            status="page_capped",
            next_state=PassState(cursor="page-2"),
        )

    monkeypatch.setattr(peer_collect, "run_open_prs_pass", fake_open_prs)

    report1 = peer_pipeline.run_peers_collection(
        peers_config, tmp_path, tmp_path / "work", "run1", token="x", as_of=date(2026, 1, 1)
    )
    assert report1.peer_settlement["kafka"]["open_prs"] is True

    report2 = peer_pipeline.run_peers_collection(
        peers_config, tmp_path, tmp_path / "work", "run2", token="x", as_of=date(2026, 1, 1)
    )
    assert call_count["n"] == 2, "run 2 must actually redo open_prs, not skip it"
    assert report2.peer_settlement["kafka"]["open_prs"] is True, (
        "open_prs going page_capped on its own redo must not unsettle a pass that already "
        "completed once"
    )

    settlement_path = tmp_path / "snapshots" / "peers" / "run2" / "kafka" / "settlement.json"
    payload = json.loads(settlement_path.read_text())
    assert payload["open_prs"] is True
    assert payload["_last_completed_at"]["open_prs"] is not None


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

    def __init__(
        self, config, token=None, rate_limit_floor=500, transport=None, **_kwargs
    ):
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


def test_run_peers_collection_passes_cassandra_data_dir_to_table_reader(tmp_path, monkeypatch):
    """Issue #176: once the peer-context data's own root (`data_dir`) and
    Cassandra's own data dir (`cassandra_data_dir`) are two different
    directories, Cassandra's raw-table reader must be called with
    `cassandra_data_dir`, never `data_dir`."""
    peers_config = PeersConfig(peers=[_peer("kafka", "apache/kafka")])
    _patch_empty_peer_collection(monkeypatch)

    seen_dirs: list[Path] = []
    original = peer_pipeline._cassandra_tables_for_comparison

    def spy(data_dir):
        seen_dirs.append(Path(data_dir))
        return original(data_dir)

    monkeypatch.setattr(peer_pipeline, "_cassandra_tables_for_comparison", spy)

    cassandra_data_dir = tmp_path / "projects" / "cassandra"
    peers_root = tmp_path / "peers"

    peer_pipeline.run_peers_collection(
        peers_config,
        peers_root,
        peers_root / "work",
        "run1",
        token="x",
        as_of=date(2026, 1, 1),
        cassandra_data_dir=cassandra_data_dir,
    )

    assert seen_dirs == [cassandra_data_dir]
    # Cassandra's computed comparison metrics still land under the
    # peer-context data's own root -- never under `cassandra_data_dir`,
    # which is read-only for this module (module docstring).
    assert (peers_root / "snapshots" / "peers" / "run1" / "cassandra").is_dir()
    assert not (cassandra_data_dir / "snapshots").exists()


def test_run_peers_collection_defaults_cassandra_data_dir_to_data_dir(tmp_path, monkeypatch):
    """Backward-compatible default: every pre-#176 caller/test (one shared
    `data_dir` for both Cassandra's own tables and the peer-context data)
    keeps working unchanged -- omitting `cassandra_data_dir` reads
    Cassandra's tables from `data_dir` itself, exactly as before this
    split existed."""
    peers_config = PeersConfig(peers=[_peer("kafka", "apache/kafka")])
    _patch_empty_peer_collection(monkeypatch)

    seen_dirs: list[Path] = []
    original = peer_pipeline._cassandra_tables_for_comparison

    def spy(data_dir):
        seen_dirs.append(Path(data_dir))
        return original(data_dir)

    monkeypatch.setattr(peer_pipeline, "_cassandra_tables_for_comparison", spy)

    peer_pipeline.run_peers_collection(
        peers_config, tmp_path, tmp_path / "work", "run1", token="x", as_of=date(2026, 1, 1)
    )

    assert seen_dirs == [Path(tmp_path)]


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


def test_peer_tables_backlog_not_undercounted_after_partial_refresh(tmp_path):
    """Issue #150 fixup's own audit: `open_prs` is redone in full every
    run, and a run cut off mid-walk (page_capped) only writes whatever PR
    rows its own pages reached *this* run -- it never deletes or replaces
    an earlier run's partition. `_peer_tables`'s read-time dedupe
    (`pipeline._dedupe_pr_rows`, latest `updated_at` per `(repo, number)`,
    read across *every* partition `storage.read_table` has ever written,
    not merely the latest run's own) means an earlier completed run's PR
    rows are never lost just because the current run's own redo didn't
    reach them again. Simulates exactly that sequence -- a completed run
    capturing PRs #1/#2, then a page-capped run that only reaches newly
    opened PR #3 -- and checks the open-PR-backlog total stays 3, not 1,
    confirming a partial refresh does not undercount."""
    from project_health.metrics.pr_backlog import TOTAL, compute_pr_backlog, other_repo_metric_id
    from tests.fixtures.metrics.builders import prs

    source = peer_collect.peer_source("kafka", "github")
    repo = "apache/kafka"

    # Run 1: open_prs pass completes in full -- both currently-open PRs captured.
    run1_table = prs(
        [
            {"repo": repo, "number": 1, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            {"repo": repo, "number": 2, "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
        ]
    )
    storage.write_partition(tmp_path, source, "pr", date(2026, 1, 3), "run1", run1_table)

    # Run 2: a *partial* (page-capped) redo of open_prs only reaches newly
    # opened PR #3 on its one allotted page before the budget cap hits --
    # it never re-fetches #1/#2 this run, but it also never deletes their
    # already-written rows; it simply writes whatever it *did* see.
    run2_table = prs(
        [{"repo": repo, "number": 3, "created_at": datetime(2026, 2, 1, tzinfo=timezone.utc)}]
    )
    storage.write_partition(tmp_path, source, "pr", date(2026, 2, 2), "run2", run2_table)

    tables = peer_pipeline._peer_tables(tmp_path, "kafka")
    # All three PRs -- #1/#2 from the completed run, #3 from the partial
    # run's own one page -- are still present after the partial refresh.
    assert sorted(tables["pr"].column("number").to_pylist()) == [1, 2, 3]

    backlog = compute_pr_backlog(
        tables,
        as_of=date(2026, 3, 1),
        run_id="run2",
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    total_id = other_repo_metric_id(TOTAL, repo)
    feb_rows = [
        row
        for row in backlog.to_pylist()
        if row["metric_id"] == total_id and row["window_start"] == date(2026, 2, 1)
    ]
    assert len(feb_rows) == 1
    assert feb_rows[0]["value"] == 3.0, (
        "a page-capped refresh must not undercount PRs an earlier completed run already "
        "captured -- the backlog total must come from the full accumulated+deduped history, "
        "not only the rows this run's own (possibly partial) pass happened to see"
    )
