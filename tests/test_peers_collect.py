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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


from project_health.peers import collect as peer_collect
from project_health.peers.config import (
    CollectionConfig,
    JiraReleaseVerification,
    PeerProject,
    PeersConfig,
)
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


# --- issue #148: rotation + fair-share budget ------------------------------


def test_rotation_start_index_changes_peer_processing_order(tmp_path, monkeypatch):
    peers_config = PeersConfig(
        peers=[
            _peer("kafka", "apache/kafka"),
            _peer("spark", "apache/spark"),
            _peer("flink", "apache/flink"),
        ]
    )
    calls: list[str] = []

    def fake_pass(collector, repo_label, *a, **k):
        calls.append(repo_label)
        return _empty_pass_result()

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_pass)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", fake_pass)
    monkeypatch.setattr(peer_collect, "run_closed_search_pass", fake_pass)

    report = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x", rotation_start_index=1
    )

    assert report.rotation_start_index == 1
    assert report.peer_order == ["spark", "flink", "kafka"]
    # The actual pass-runner call order follows the rotated order, not
    # `peers_config.peers`' own fixed config order.
    assert calls[0] == "apache/spark"


def test_fair_share_caps_pages_below_configured_max(tmp_path, monkeypatch):
    """A tight per-run page budget (10) split across 2 not-done peers
    caps each at 5 pages per pass -- well below the 20-page
    per-(peer, pass) ceiling `max_pages_per_pass` alone would allow."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")],
        collection=CollectionConfig(max_pages_per_pass=20, github_page_budget_per_run=10),
    )
    captured_max_pages: dict[str, int] = {}

    def fake_created_desc(
        collector, repo_label, state, window_start, bot_patterns, snapshot_id, **kwargs
    ):
        captured_max_pages[repo_label] = kwargs.get("max_pages")
        return PassResult(
            pr_rows=[],
            review_rows=[],
            comment_rows=[],
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
            status="page_capped",
            next_state=PassState(cursor="x"),
            pages_fetched=kwargs.get("max_pages"),
        )

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_created_desc)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
    )

    assert captured_max_pages["apache/kafka"] == 5
    assert captured_max_pages["apache/spark"] == 5


def test_peer_finishing_early_returns_budget_to_remaining_peers(tmp_path, monkeypatch):
    """issue #148 acceptance: "a peer finishing early returns budget to the
    rest" -- kafka's up-front fair share is 10 // 2 = 5 pages, but it only
    actually uses 1; spark's own fair share is then recomputed from
    whatever's genuinely left (9 pages, 1 peer not done), not the naive
    5-page split a fixed up-front allocation would have given it."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")],
        collection=CollectionConfig(max_pages_per_pass=20, github_page_budget_per_run=10),
    )
    captured_max_pages: dict[str, int] = {}

    def fake_created_desc(
        collector, repo_label, state, window_start, bot_patterns, snapshot_id, **kwargs
    ):
        captured_max_pages[repo_label] = kwargs.get("max_pages")
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
                pages_fetched=1,
            )
        return _empty_pass_result()

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_created_desc)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
    )

    assert captured_max_pages["apache/kafka"] == 5
    assert captured_max_pages["apache/spark"] == 9


def test_rotation_prevents_cross_run_starvation(tmp_path, monkeypatch):
    """Real-run regression (issue #148): with peers always processed in
    `projects/peers.yaml`'s fixed config order, apache/kafka's own
    `created_desc` pass hitting the real rate-limit floor set
    `budget_exhausted`, skipping every remaining (peer, pass) -- kafka
    going first *every single run* meant flink never got a single pass
    across several real runs. Rotating which peer starts each run means a
    huge peer's own floor-hit no longer blocks the same peers every time.
    """
    peers_config = PeersConfig(
        peers=[
            _peer("kafka", "apache/kafka"),
            _peer("spark", "apache/spark"),
            _peer("flink", "apache/flink"),
        ]
    )

    def fake_created_desc(
        collector, repo_label, state, window_start, bot_patterns, snapshot_id, **kwargs
    ):
        if repo_label == "apache/kafka":
            return PassResult(
                pr_rows=[],
                review_rows=[],
                comment_rows=[],
                bot_prs_excluded=0,
                bot_reviews_excluded=0,
                bot_comments_excluded=0,
                status="rate_limited",
                next_state=state,
                pages_fetched=kwargs.get("max_pages") or 1,
                error="rate limit budget floor reached",
            )
        return _empty_pass_result()

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_created_desc)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    # Run 1: un-rotated (today's real bug) -- kafka goes first and starves
    # spark/flink entirely.
    report1 = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x", rotation_start_index=0
    )
    statuses1 = {(o.peer_id, o.pass_name): o.status for o in report1.pass_outcomes}
    assert statuses1[("spark", "created_desc")] == "skipped"
    assert statuses1[("flink", "created_desc")] == "skipped"

    # Run 2: rotation has advanced (spark starts first this time) -- spark
    # and flink each get their own turn before kafka's floor-hit triggers.
    report2 = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run2", date(2026, 1, 1), token="x", rotation_start_index=1
    )
    statuses2 = {(o.peer_id, o.pass_name): o.status for o in report2.pass_outcomes}
    assert statuses2[("spark", "created_desc")] == "completed"
    assert statuses2[("flink", "created_desc")] == "completed"
    assert statuses2[("kafka", "created_desc")] == "rate_limited"


def test_settled_peer_gets_minimal_page_cap_and_is_excluded_from_fair_share(
    tmp_path, monkeypatch
):
    """issue #148 acceptance: "peers whose passes are all complete
    (watermarks current) are skipped cheaply" -- kafka's persisted
    watermarks show no resumable work outstanding on any of its three
    passes, so it gets only a 1-page freshness check, and the whole
    10-page run budget goes to spark, the one peer still genuinely not
    done, instead of being split evenly with an already-settled kafka."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")],
        collection=CollectionConfig(max_pages_per_pass=20, github_page_budget_per_run=10),
    )

    kafka_source = peer_collect.peer_source("kafka", "github")
    storage.write_watermark(
        tmp_path,
        kafka_source,
        PassState(high_watermark="2024-01-01T00:00:00+00:00").to_json(),
        table="pr_pass_created_desc",
    )
    storage.write_watermark(
        tmp_path, kafka_source, PassState().to_json(), table="pr_pass_open_prs"
    )
    storage.write_watermark(
        tmp_path, kafka_source, PassState().to_json(), table="pr_pass_closed_search"
    )

    captured_max_pages: dict[str, int] = {}

    def fake_created_desc(
        collector, repo_label, state, window_start, bot_patterns, snapshot_id, **kwargs
    ):
        captured_max_pages[repo_label] = kwargs.get("max_pages")
        return _empty_pass_result()

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", fake_created_desc)
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    report = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
    )

    assert report.settled_peers == ["kafka"]
    assert captured_max_pages["apache/kafka"] == 1
    assert captured_max_pages["apache/spark"] == 10


# --- issue #150: per-pass settlement + GraphQL reserve ---------------------


def test_peer_pass_settlement_never_run_is_unsettled(tmp_path):
    """A pass with no watermark at all (never even attempted this peer's
    turn, e.g. an earlier pass hit the shared budget floor first) must
    never be read as settled -- for every pass, not only `created_desc`."""
    peer = _peer("flink", "apache/flink")
    settlement = peer_collect.peer_pass_settlement(tmp_path, peer)
    assert settlement == {
        "created_desc": False,
        "open_prs": False,
        "closed_search": False,
    }


def test_peer_pass_settlement_created_desc_needs_high_watermark(tmp_path):
    """`created_desc`'s own `cursor is None` alone is ambiguous between
    "never run" and "completed its first full backfill" -- only
    `high_watermark` set distinguishes the two."""
    peer = _peer("kafka", "apache/kafka")
    source = peer_collect.peer_source("kafka", "github")
    storage.write_watermark(
        tmp_path, source, PassState(cursor="resume-here").to_json(), table="pr_pass_created_desc"
    )
    settlement = peer_collect.peer_pass_settlement(tmp_path, peer)
    assert settlement["created_desc"] is False

    storage.write_watermark(
        tmp_path,
        source,
        PassState(high_watermark="2024-01-01T00:00:00+00:00").to_json(),
        table="pr_pass_created_desc",
    )
    settlement = peer_collect.peer_pass_settlement(tmp_path, peer)
    assert settlement["created_desc"] is True


def test_peer_pass_settlement_open_prs_and_closed_search_need_a_completed_run(tmp_path):
    """Unlike `created_desc`, `open_prs`/`closed_search` are settled once a
    run has completed and cleared `cursor` -- but only if a run actually
    happened (a written watermark), never merely because the default
    `PassState()` also has `cursor is None`."""
    peer = _peer("kafka", "apache/kafka")
    source = peer_collect.peer_source("kafka", "github")

    # Mid-walk, cut off by the budget floor: resumable, not settled.
    storage.write_watermark(
        tmp_path, source, PassState(cursor="resume-here").to_json(), table="pr_pass_open_prs"
    )
    assert peer_collect.peer_pass_settlement(tmp_path, peer)["open_prs"] is False

    # Completed a full run: cursor cleared, watermark actually written.
    storage.write_watermark(tmp_path, source, PassState().to_json(), table="pr_pass_open_prs")
    assert peer_collect.peer_pass_settlement(tmp_path, peer)["open_prs"] is True


def test_peer_is_settled_matches_peer_pass_settlement(tmp_path):
    peer = _peer("kafka", "apache/kafka")
    source = peer_collect.peer_source("kafka", "github")
    storage.write_watermark(
        tmp_path,
        source,
        PassState(high_watermark="2024-01-01T00:00:00+00:00").to_json(),
        table="pr_pass_created_desc",
    )
    storage.write_watermark(tmp_path, source, PassState().to_json(), table="pr_pass_open_prs")
    storage.write_watermark(tmp_path, source, PassState().to_json(), table="pr_pass_closed_search")

    assert peer_collect._peer_is_settled(tmp_path, peer) is True
    assert all(peer_collect.peer_pass_settlement(tmp_path, peer).values())


class _FloorCapturingCollector:
    """Stand-in for `collectors.github.GitHubCollector` that only records
    the `rate_limit_floor` it was constructed with -- the one place
    `collect_and_write_github`'s own GraphQL-reserve wiring
    (`peers_config.collection.github_rate_limit_floor`) is observable from
    outside `peer_collect`."""

    seen_fractions: list = []
    seen_floors: list[int] = []

    def __init__(
        self,
        config,
        token=None,
        rate_limit_floor=500,
        transport=None,
        rate_limit_floor_fraction=None,
    ):
        type(self).seen_floors.append(rate_limit_floor)
        type(self).seen_fractions.append(rate_limit_floor_fraction)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


def test_collect_and_write_github_passes_configured_reserve_as_rate_limit_floor(
    tmp_path, monkeypatch
):
    """Issue #150's own "Budget reserve" -- whatever `github_rate_limit_
    floor` this run's config carries is exactly the `rate_limit_floor`
    `GitHubCollector` stops paginating at, with no mechanism to change it
    (nothing new to introduce) besides the default this issue raises."""
    peers_config = PeersConfig(
        peers=[_peer("kafka", "apache/kafka")],
        collection=CollectionConfig(github_rate_limit_floor=2500),
    )
    seen_floors: list[int] = []
    seen_fractions: list = []
    fake_collector = type(
        "_FakeCollector",
        (_FloorCapturingCollector,),
        {"seen_floors": seen_floors, "seen_fractions": seen_fractions},
    )
    monkeypatch.setattr(peer_collect, "GitHubCollector", fake_collector)
    monkeypatch.setattr(peer_collect, "run_created_desc_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
    )

    assert seen_floors == [2500]
    # the default fraction is passed through so a small-allowance token still progresses
    assert seen_fractions == [0.5]


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


# --- issue #150 fixup: sticky settlement ------------------------------------
#
# The bug: `open_prs`/`closed_search` are redone in full every run
# ("cheap to redo"), and a later run's own redo going partial/page-capped
# left `cursor` non-null, which the old rule read as "unsettled" even
# though a complete, usable snapshot from an earlier run already existed.
# Real backfill evidence (30 hourly passes): settlement for kafka/flink/
# pulsar/spark/datafusion's `open_prs` flipped between true/false on almost
# every single pass. These tests exercise `peer_pass_settlement`'s sticky
# fix directly against hand-built `PassState` JSON (the same shape a real
# watermark file holds), and `collect_and_write_github`'s own stamping/
# skip-rerun behavior end to end.


def test_sticky_settlement_survives_a_later_partial_refresh(tmp_path):
    """Once a pass has completed (`last_completed_at` set), a later run
    going partial/page-capped must not unsettle it -- the exact flip issue
    #150's fixup reports from the real backfill."""
    peer = _peer("kafka", "apache/kafka")
    source = peer_collect.peer_source("kafka", "github")
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)

    # open_prs completed a full run yesterday...
    yesterday = (now - timedelta(days=1)).isoformat()
    completed_state = PassState(cursor=None, last_completed_at=yesterday)
    storage.write_watermark(tmp_path, source, completed_state.to_json(), table="pr_pass_open_prs")
    assert peer_collect.peer_pass_settlement(tmp_path, peer, now=now)["open_prs"] is True

    # ...then today's redo gets page-capped mid-walk (cursor non-null again).
    partial_state = PassState(cursor="page-2-cursor", last_completed_at=yesterday)
    storage.write_watermark(tmp_path, source, partial_state.to_json(), table="pr_pass_open_prs")

    settlement = peer_collect.peer_pass_settlement(tmp_path, peer, now=now)
    assert settlement["open_prs"] is True, "refresh in progress must not unsettle a completed pass"


def test_sticky_settlement_bounded_by_freshness_days(tmp_path):
    """`last_completed_at` more than `freshness_days` old falls back to
    'Collecting' -- sticky is bounded, not permanent, so a pass that's
    truly gone stale doesn't show an arbitrarily old snapshot forever."""
    peer = _peer("kafka", "apache/kafka")
    source = peer_collect.peer_source("kafka", "github")
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)

    stale_at = (now - timedelta(days=20)).isoformat()
    stale_state = PassState(cursor="resuming", last_completed_at=stale_at)
    storage.write_watermark(tmp_path, source, stale_state.to_json(), table="pr_pass_closed_search")

    assert (
        peer_collect.peer_pass_settlement(tmp_path, peer, freshness_days=14, now=now)[
            "closed_search"
        ]
        is False
    )
    # No bound at all (freshness_days=None) -- sticky forever.
    assert (
        peer_collect.peer_pass_settlement(tmp_path, peer, freshness_days=None, now=now)[
            "closed_search"
        ]
        is True
    )
    # Still within the window -- settled.
    fresh_at = (now - timedelta(days=10)).isoformat()
    fresh_state = PassState(cursor="resuming", last_completed_at=fresh_at)
    storage.write_watermark(tmp_path, source, fresh_state.to_json(), table="pr_pass_closed_search")
    assert (
        peer_collect.peer_pass_settlement(tmp_path, peer, freshness_days=14, now=now)[
            "closed_search"
        ]
        is True
    )


def test_legacy_watermark_without_last_completed_at_still_settled(tmp_path):
    """Backward compatibility (issue #150 fixup's own acceptance): a
    watermark written before sticky settlement shipped has no
    `last_completed_at` field at all -- `absent field + cursor None =
    completed as today`, with no freshness bound applied (there's no
    timestamp to bound it by)."""
    peer = _peer("kafka", "apache/kafka")
    source = peer_collect.peer_source("kafka", "github")
    legacy_json = '{"cursor": null, "high_watermark": null, "newest_seen": null}'
    storage.write_watermark(tmp_path, source, legacy_json, table="pr_pass_open_prs")

    assert PassState.from_json(legacy_json).last_completed_at is None
    assert peer_collect.peer_pass_settlement(tmp_path, peer)["open_prs"] is True
    # Even far in the future -- no freshness bound can apply without a timestamp.
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert (
        peer_collect.peer_pass_settlement(tmp_path, peer, freshness_days=14, now=far_future)[
            "open_prs"
        ]
        is True
    )


def test_migration_from_legacy_watermark_stays_settled_through_a_partial_rerun(
    tmp_path, monkeypatch
):
    """The real migration scenario this fixup is built for: a peer already
    has a legacy-completed watermark (no `last_completed_at`) on the data
    branch. The first run after this fix ships gives that pass its turn
    again, and that turn itself goes `page_capped` (budget ran out before
    finishing the redo) -- without the migration shim in
    `_stamp_last_completed_at`, this would silently lose the "already
    completed once" fact the instant `cursor` stops being `None`, exactly
    reproducing the reported bug one run after the fix ships. With the
    shim, it must stay settled."""
    peer = _peer("kafka", "apache/kafka")
    peers_config = PeersConfig(peers=[peer])
    source = peer_collect.peer_source("kafka", "github")

    # Legacy: open_prs completed under the pre-fix code (no last_completed_at).
    storage.write_watermark(tmp_path, source, PassState().to_json(), table="pr_pass_open_prs")
    storage.write_watermark(
        tmp_path,
        source,
        PassState(high_watermark="2024-01-01T00:00:00+00:00").to_json(),
        table="pr_pass_created_desc",
    )
    storage.write_watermark(tmp_path, source, PassState().to_json(), table="pr_pass_closed_search")

    assert peer_collect.peer_pass_settlement(tmp_path, peer)["open_prs"] is True

    def fake_open_prs(collector, repo_label, state, bot_patterns, snapshot_id, **kwargs):
        # This run's own redo of open_prs gets page-capped mid-walk.
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

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", fake_open_prs)
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
    )

    stored = storage.read_watermark(tmp_path, source, table="pr_pass_open_prs")
    stored_state = PassState.from_json(stored)
    assert stored_state.cursor == "page-2"
    assert stored_state.last_completed_at is not None, (
        "the legacy 'completed' fact must be stamped into a real timestamp "
        "the moment this pass's own redo stops being a no-op cursor-None state"
    )

    # Settlement must still read 'settled' right after this partial rerun.
    assert peer_collect.peer_pass_settlement(tmp_path, peer)["open_prs"] is True


def test_first_ever_partial_run_is_not_falsely_sticky_settled(tmp_path, monkeypatch):
    """The scenario the migration shim must NOT cover: a pass that has
    *never* run before at all (no watermark key ever written) goes
    partial/page-capped on its very first attempt. An empty `PassState()`
    is indistinguishable from a legacy-completed one by its fields alone --
    only `raw_before is None` (this function's own signal) tells a
    genuinely first-ever run apart from a legacy-completed one, and a
    first-ever partial run must stay unsettled, never sticky-settled from a
    completion that never actually happened."""
    peer = _peer("kafka", "apache/kafka")
    peers_config = PeersConfig(peers=[peer])
    source = peer_collect.peer_source("kafka", "github")

    def fake_open_prs(collector, repo_label, state, bot_patterns, snapshot_id, **kwargs):
        assert state == PassState()  # genuinely never run before
        return PassResult(
            pr_rows=[],
            review_rows=[],
            comment_rows=[],
            bot_prs_excluded=0,
            bot_reviews_excluded=0,
            bot_comments_excluded=0,
            status="page_capped",
            next_state=PassState(cursor="page-1"),
        )

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", fake_open_prs)
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x"
    )

    stored_state = PassState.from_json(
        storage.read_watermark(tmp_path, source, table="pr_pass_open_prs")
    )
    assert stored_state.last_completed_at is None
    assert peer_collect.peer_pass_settlement(tmp_path, peer)["open_prs"] is False


def test_skip_rerun_within_hours_avoids_calling_a_recently_completed_pass(tmp_path, monkeypatch):
    """Issue #150 fixup's 'reduce waste' half: a pass that completed very
    recently is skipped outright this run -- no API call, no page spent --
    freeing this run's shared budget for whichever peer/pass hasn't caught
    up yet."""
    peer = _peer("kafka", "apache/kafka")
    peers_config = PeersConfig(
        peers=[peer], collection=CollectionConfig(skip_rerun_within_hours=20)
    )
    source = peer_collect.peer_source("kafka", "github")
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    recently_completed = PassState(
        cursor=None, last_completed_at=(now - timedelta(hours=2)).isoformat()
    )
    storage.write_watermark(
        tmp_path, source, recently_completed.to_json(), table="pr_pass_open_prs"
    )

    calls: list[str] = []

    def tracking_open_prs(*a, **k):
        calls.append("open_prs")
        return _empty_pass_result()

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", tracking_open_prs)
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    report = peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x", now=now
    )

    assert calls == [], "a pass that completed 2h ago (well within 20h) must be skipped outright"
    statuses = {(o.peer_id, o.pass_name): o.status for o in report.pass_outcomes}
    assert statuses[("kafka", "open_prs")] == "completed"
    assert statuses[("kafka", "open_prs")] != "skipped"  # not the 'budget exhausted' skip


def test_skip_rerun_does_not_apply_once_outside_the_window(tmp_path, monkeypatch):
    """The mirror of the previous test -- once `last_completed_at` is
    older than `skip_rerun_within_hours`, the pass is actually re-run."""
    peer = _peer("kafka", "apache/kafka")
    peers_config = PeersConfig(
        peers=[peer], collection=CollectionConfig(skip_rerun_within_hours=20)
    )
    source = peer_collect.peer_source("kafka", "github")
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    completed_yesterday = PassState(
        cursor=None, last_completed_at=(now - timedelta(hours=30)).isoformat()
    )
    storage.write_watermark(
        tmp_path, source, completed_yesterday.to_json(), table="pr_pass_open_prs"
    )

    calls: list[str] = []

    def tracking_open_prs(*a, **k):
        calls.append("open_prs")
        return _empty_pass_result()

    monkeypatch.setattr(peer_collect, "run_created_desc_pass", lambda *a, **k: _empty_pass_result())
    monkeypatch.setattr(peer_collect, "run_open_prs_pass", tracking_open_prs)
    monkeypatch.setattr(
        peer_collect, "run_closed_search_pass", lambda *a, **k: _empty_pass_result()
    )

    peer_collect.collect_and_write_github(
        peers_config, tmp_path, "run1", date(2026, 1, 1), token="x", now=now
    )

    assert calls == ["open_prs"]


def test_peer_pass_last_completed_at_reads_stamped_timestamps(tmp_path):
    peer = _peer("kafka", "apache/kafka")
    source = peer_collect.peer_source("kafka", "github")
    storage.write_watermark(
        tmp_path,
        source,
        PassState(last_completed_at="2026-09-01T00:00:00+00:00").to_json(),
        table="pr_pass_open_prs",
    )

    last_completed_at = peer_collect.peer_pass_last_completed_at(tmp_path, peer)
    assert last_completed_at["open_prs"] == "2026-09-01T00:00:00+00:00"
    assert last_completed_at["created_desc"] is None
    assert last_completed_at["closed_search"] is None
