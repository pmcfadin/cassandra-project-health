"""Per-peer raw-table collection (issue #145).

Writes every peer's raw tables under `raw/peers/<id>/<table>/...` on the
data branch -- a namespace entirely separate from Cassandra's own
`raw/git/...`/`raw/github/...`/`raw/release/...` (issue #145's own
requirement: "Cassandra's own tables are untouched"). Achieved for free:
`project_health.storage.raw_table_dir`/`raw_partition_path` just join
`data_dir / "raw" / source / table` -- passing `source=f"peers/{peer.id}"`
needs no change to `storage.py` at all, since `pathlib.Path` handles the
embedded `/` the same as any other path segment. Watermarks use the same
trick against `storage.read_watermark`/`write_watermark`'s `source`
argument.

One GitHub GraphQL call (`peers.github.collect_peer_prs`) covers every
peer's PRs/reviews/comments in a single shared rate-limit budget (see that
module's docstring); this module splits the combined result by `repo`
before writing each peer's own partition. Git-commit collection and
release collection are genuinely per-peer (a separate clone/REST call
each), so `collect_peer_git`/`collect_peer_releases` loop one peer at a
time, with the git clone deleted immediately after each peer
(`peers.git_clone.cleanup`) -- issue #145's disk budget never holds more
than one peer's clone at a time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from project_health.collectors.git import GitCollectionResult, GitCollector
from project_health.collectors.github import GitHubCollectionResult
from project_health.collectors.reviewer_trailer import ReviewerExtractor
from project_health.config import BotPattern
from project_health.peers import git_clone
from project_health.peers.config import BotPatternConfig, PeerProject, PeersConfig
from project_health.peers.github import collect_peer_prs
from project_health.peers.release import (
    GaTag,
    VerificationResult,
    fetch_ga_tags,
    verify_release_counts,
)
from project_health.schema import get_schema, validate
from project_health import storage


def peer_source(peer_id: str, table_group: str) -> str:
    """The `storage.py` `source` argument for one of a peer's raw table
    groups, e.g. `peer_source("kafka", "git")` -> `"peers/kafka/git"`."""
    return f"peers/{peer_id}/{table_group}"


# --- GitHub PR/review/comment ------------------------------------------


@dataclass(frozen=True)
class PeerGithubCollectionReport:
    result: GitHubCollectionResult
    per_peer_pr_counts: dict[str, int]


def _filter_by_repo(table: pa.Table, repo: str) -> pa.Table:
    if table.num_rows == 0:
        return table
    mask = pc.equal(table.column("repo"), repo)
    return table.filter(mask)


def collect_and_write_github(
    peers_config: PeersConfig,
    data_dir: str | Path,
    run_id: str,
    partition_date: date,
    *,
    token: str | None = None,
    max_prs_per_repo: int | None = None,
) -> PeerGithubCollectionReport:
    """Collect every peer's PRs/reviews/comments in one shared-budget call,
    then write each peer's own `raw/peers/<id>/github/{pr,pr_review,
    pr_comment}` partition and advance that peer's own GraphQL cursor
    watermark (`storage.write_watermark`, `source=peer_source(id,
    "github")`)."""
    watermarks = {
        peer.repo: storage.read_watermark(data_dir, peer_source(peer.id, "github"))
        for peer in peers_config.peers
    }
    result = collect_peer_prs(
        peers_config.peers,
        peers_config.collection.bot_patterns,
        token=token,
        watermarks=watermarks,
        rate_limit_floor=peers_config.collection.github_rate_limit_floor,
        max_prs_per_repo=max_prs_per_repo,
    )

    per_peer_pr_counts: dict[str, int] = {}
    for peer in peers_config.peers:
        outcome = result.repos.get(peer.repo)
        pr_table = _filter_by_repo(result.prs, peer.repo)
        review_table = _filter_by_repo(result.reviews, peer.repo)
        comment_table = _filter_by_repo(result.comments, peer.repo)
        per_peer_pr_counts[peer.id] = pr_table.num_rows

        source = peer_source(peer.id, "github")
        if pr_table.num_rows:
            storage.write_partition(data_dir, source, "pr", partition_date, run_id, pr_table)
        if review_table.num_rows:
            storage.write_partition(
                data_dir, source, "pr_review", partition_date, run_id, review_table
            )
        if comment_table.num_rows:
            storage.write_partition(
                data_dir, source, "pr_comment", partition_date, run_id, comment_table
            )
        if outcome is not None and outcome.next_watermark:
            storage.write_watermark(data_dir, source, outcome.next_watermark)

    return PeerGithubCollectionReport(result=result, per_peer_pr_counts=per_peer_pr_counts)


# --- git commits ---------------------------------------------------------


@dataclass(frozen=True)
class PeerGitCollectionReport:
    peer_id: str
    commits_collected: int
    clone_bytes: int
    error: str | None = None


def collect_and_write_git(
    peer: PeerProject,
    commit_lookback_months: int,
    data_dir: str | Path,
    workdir: str | Path,
    run_id: str,
    partition_date: date,
    bot_patterns: list[BotPatternConfig],
) -> PeerGitCollectionReport:
    """Shallow bare clone -> `GitCollector.collect()` -> write
    `raw/peers/<id>/git/contribution_event` -> delete the clone. Never
    raises on a clone/collection failure -- returns a report with `error`
    set so one peer's failure doesn't abort the others (`pipeline.py`'s
    per-peer loop)."""
    clone_path = Path(workdir) / f"{peer.id}-clone"
    git_clone.cleanup(clone_path)  # defensive: never start atop a stale clone

    now = git_clone.utc_now()
    shallow_since = git_clone.shallow_since_date(now, commit_lookback_months)
    try:
        git_clone.clone_shallow_bare(
            git_clone.github_clone_url(peer.owner, peer.name),
            clone_path,
            branch=peer.default_branch,
            shallow_since=shallow_since,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never propagated
        git_clone.cleanup(clone_path)
        return PeerGitCollectionReport(
            peer_id=peer.id, commits_collected=0, clone_bytes=0, error=str(exc)
        )

    clone_bytes = git_clone.du_bytes(clone_path)
    snapshot_id = str(uuid.uuid4())
    source = peer_source(peer.id, "git")
    watermark = storage.read_watermark(data_dir, source)

    try:
        collector = GitCollector(ReviewerExtractor())
        bot_pattern_objs = [BotPattern(field=p.field, regex=p.regex) for p in bot_patterns]
        result: GitCollectionResult = collector.collect(
            repo_path=clone_path,
            repo_label=peer.repo,
            default_branch=peer.default_branch,
            watermark=watermark,
            bot_patterns=bot_pattern_objs,
            source_snapshot_id=snapshot_id,
        )
    except Exception as exc:  # noqa: BLE001
        git_clone.cleanup(clone_path)
        return PeerGitCollectionReport(
            peer_id=peer.id, commits_collected=0, clone_bytes=clone_bytes, error=str(exc)
        )

    git_clone.cleanup(clone_path)

    if result.contribution_event.num_rows:
        storage.write_partition(
            data_dir,
            source,
            "contribution_event",
            partition_date,
            run_id,
            result.contribution_event,
        )
    storage.write_watermark(data_dir, source, result.next_watermark)

    return PeerGitCollectionReport(
        peer_id=peer.id,
        commits_collected=result.commits_collected,
        clone_bytes=clone_bytes,
    )


# --- releases --------------------------------------------------------------


@dataclass(frozen=True)
class PeerReleaseCollectionReport:
    peer_id: str
    release_count: int
    verification: VerificationResult


_RELEASE_SCHEMA = get_schema("release")


def _ga_tags_to_release_rows(tags: list[GaTag], repo: str, snapshot_id: str) -> pa.Table:
    collected_at = datetime.now(timezone.utc)
    rows = [
        {
            "release_id": tag.tag_name,
            "tag_name": tag.tag_name,
            "version": tag.version,
            "major_minor": tag.major_minor,
            "release_date": tag.release_date,
            "release_date_source": "git_tag",
            # Peer release discovery has no archive-existence cross-check
            # of its own (module docstring: `verify_release_counts` is a
            # separate per-year count comparison, not a per-release flag).
            "archive_verified": None,
            "archive_date": None,
            "repo": repo,
            "source_snapshot_id": snapshot_id,
            "collected_at": collected_at,
        }
        for tag in tags
    ]
    if not rows:
        return _RELEASE_SCHEMA.empty_table()
    return validate("release", pa.Table.from_pylist(rows, schema=_RELEASE_SCHEMA))


def collect_and_write_releases(
    peer: PeerProject,
    data_dir: str | Path,
    run_id: str,
    partition_date: date,
    *,
    token: str | None = None,
) -> PeerReleaseCollectionReport:
    """GA tags via the REST Tags API (`peers.release.fetch_ga_tags`, no
    clone needed) -> write `raw/peers/<id>/release/release` -> verify the
    resulting per-year counts against `peer.release_verification`'s
    independent source."""
    tags = fetch_ga_tags(peer.owner, peer.name, peer.tag_prefix, token=token)
    snapshot_id = str(uuid.uuid4())
    table = _ga_tags_to_release_rows(tags, peer.repo, snapshot_id)

    source = peer_source(peer.id, "release")
    if table.num_rows:
        storage.write_partition(data_dir, source, "release", partition_date, run_id, table)

    verification = verify_release_counts(peer, [tag.release_date for tag in tags], token=token)
    return PeerReleaseCollectionReport(
        peer_id=peer.id, release_count=len(tags), verification=verification
    )
