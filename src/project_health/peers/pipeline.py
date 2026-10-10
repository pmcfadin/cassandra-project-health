"""Peer-context top-level orchestration (issue #145).

`run_peers_collection` is what `.github/workflows/peers.yml` and a local
real-data run both call: collect every peer's raw tables
(`peers.collect`), then compute the five metrics for Cassandra itself
*and* every peer (`metrics.peer_metrics.compute_peer_metrics`), writing
each project's computed output to its own
`snapshots/peers/<run_id>/<project_id>/{metric_value,pr_backlog}.parquet`
file. Cassandra's own raw tables (`raw/git/...`, `raw/github/...`,
`raw/release/...`) are only ever *read* here, never written -- this module
writes exclusively under `raw/peers/...` and `snapshots/peers/...`.

Site generation (`site/peers_page.py`) reads the latest
`snapshots/peers/<run_id>/` directory; this module's own `PeersRunReport`
is also what the real-data run (issue #145 acceptance) reports from.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from project_health.collectors.github import resolve_github_token
from project_health.metrics.peer_metrics import compute_peer_metrics
from project_health.peers.collect import (
    PeerGitCollectionReport,
    PeerGithubCollectionReport,
    PeerReleaseCollectionReport,
    collect_and_write_git,
    collect_and_write_releases,
    collect_and_write_github,
)
from project_health.peers.config import PeersConfig
from project_health.peers import collect as peer_collect
from project_health.peers import rotation as peer_rotation
from project_health import storage

CASSANDRA_REPO = "apache/cassandra"
CASSANDRA_PROJECT_ID = "cassandra"

# Issue #145's own disk-budget rule for a constrained collection machine:
# "check df -h / between peers and stop if free space drops below 2 GB."
# Enforced here (not just a manual real-run habit) so a resource-constrained
# collection host is always protected, not only when someone remembers to
# watch `df` by hand. `None` disables the check entirely (the default for
# every call that doesn't pass it, e.g. every test in this suite, and any
# collection host that isn't disk-constrained).
DEFAULT_MIN_FREE_DISK_BYTES: int | None = None


@dataclass(frozen=True)
class PeersRunReport:
    run_id: str
    started_at: datetime
    completed_at: datetime
    github: PeerGithubCollectionReport
    git_reports: list[PeerGitCollectionReport]
    release_reports: list[PeerReleaseCollectionReport]
    peak_clone_bytes: int
    metrics_by_project: dict[str, dict[str, int]] = field(default_factory=dict)
    # Peer ids whose git/release collection was skipped because free disk
    # had already dropped below `min_free_disk_bytes` before their turn --
    # empty unless that budget was ever actually hit this run.
    disk_budget_skipped_peers: list[str] = field(default_factory=list)
    # Issue #150: each peer's own per-pass settlement as of the *end* of
    # this run (`peers.collect.peer_pass_settlement`, read fresh from the
    # watermarks this run's own `collect_and_write_github` just persisted)
    # -- also written into that peer's own snapshot directory
    # (`_write_settlement`) so `site/peers_page.py` can gate its
    # GitHub-derived metrics with no API calls of its own.
    peer_settlement: dict[str, dict[str, bool]] = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        """A plain-dict summary safe to `json.dumps` -- used by the real
        run's own report and `manifests/peers-<run_id>.json`."""
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "wall_time_seconds": (self.completed_at - self.started_at).total_seconds(),
            "per_peer_pr_counts": self.github.per_peer_pr_counts,
            "github_overall_status": self.github.overall_status,
            "disk_budget_skipped_peers": self.disk_budget_skipped_peers,
            # Issue #148: this run's rotation offset/order, recorded here
            # so the run manifest is a deterministic, inspectable record of
            # which peer went first and which peers were already "done"
            # (settled watermarks) going into this run -- never only
            # inferable after the fact from `pass_outcomes`.
            "github_rotation_start_index": self.github.rotation_start_index,
            "github_peer_order": self.github.peer_order,
            "github_settled_peers": self.github.settled_peers,
            "github_pass_outcomes": [
                {
                    "peer_id": o.peer_id,
                    "pass_name": o.pass_name,
                    "status": o.status,
                    "pr_count": o.pr_count,
                    "review_count": o.review_count,
                    "comment_count": o.comment_count,
                    "pages_fetched": o.pages_fetched,
                    "issue_count": o.issue_count,
                    "error": o.error,
                }
                for o in self.github.pass_outcomes
            ],
            "peer_settlement": self.peer_settlement,
            "git_reports": [asdict(r) for r in self.git_reports],
            "release_reports": [
                {
                    "peer_id": r.peer_id,
                    "release_count": r.release_count,
                    "verification": {
                        "source_type": r.verification.source_type,
                        "tag_derived_by_year": r.verification.tag_derived_by_year,
                        "independent_by_year": r.verification.independent_by_year,
                        "fetch_error": r.verification.fetch_error,
                    },
                }
                for r in self.release_reports
            ],
            "peak_clone_bytes": self.peak_clone_bytes,
            "metrics_by_project": self.metrics_by_project,
        }


def _cassandra_tables_for_comparison(data_dir: str | Path) -> dict[str, pa.Table]:
    """Cassandra's own already-collected raw tables, filtered to
    `apache/cassandra` only (the peer comparison is single-repo for every
    project; Cassandra's production config pools several related repos
    into `raw/github/pr`, same "scoped to apache/cassandra" reasoning
    `metrics/pr_backlog.py`'s own module docstring documents for its
    `PRIMARY_REPO` constant). Read-only -- never written back.

    Dedupes `pr`/`pr_review`/`pr_comment`/`release` the same way
    `pipeline.run_pipeline` itself does before handing them to `metrics.
    engine.compute_all` -- `storage.read_table` concatenates every raw
    partition ever written, and both the GitHub PR collector's watermark
    (a re-fetched PR re-emits its whole reviews/comments connection) and
    the release collector's full-refresh-every-run design (`collectors.
    release`'s own module docstring) mean the *same* row legitimately
    appears in more than one partition by design -- skipping this step
    silently double- (or triple-, ...) counts every metric that sums rows
    (real-run finding, 2026-10-09: an undeduped `release` read inflated
    every peer's release count ~2x after a second collection run).
    """
    # Deferred import: `project_health.pipeline` imports `site.generate`,
    # which imports `site.peers_page`, which imports this module for
    # `CASSANDRA_PROJECT_ID`/`latest_peers_run_id` -- a module-level import
    # here would be circular (same "deferred import, not circular" pattern
    # `metrics.engine.compute_all` uses for its own `dev_metrics`/
    # `release_cadence` imports).
    from project_health.pipeline import (
        _dedupe_pr_comment_rows,
        _dedupe_pr_review_rows,
        _dedupe_pr_rows,
        _dedupe_release_rows,
    )

    def _filter(table: pa.Table) -> pa.Table:
        if table.num_rows == 0 or "repo" not in table.column_names:
            return table
        return table.filter(pc.equal(table.column("repo"), CASSANDRA_REPO))

    return {
        "contribution_event": _filter(storage.read_table(data_dir, "git", "contribution_event")),
        "pr": _dedupe_pr_rows(_filter(storage.read_table(data_dir, "github", "pr"))),
        "pr_review": _dedupe_pr_review_rows(
            _filter(storage.read_table(data_dir, "github", "pr_review"))
        ),
        "pr_comment": _dedupe_pr_comment_rows(
            _filter(storage.read_table(data_dir, "github", "pr_comment"))
        ),
        "release": _dedupe_release_rows(
            _filter(storage.read_table(data_dir, "release", "release"))
        ),
    }


def _peer_tables(data_dir: str | Path, peer_id: str) -> dict[str, pa.Table]:
    """One peer's own raw tables -- see `_cassandra_tables_for_comparison`'s
    docstring for why `pr`/`pr_review`/`pr_comment`/`release` are deduped
    here (not merely read) before any metric ever sees them."""
    from project_health.peers.collect import peer_source
    from project_health.pipeline import (
        _dedupe_pr_comment_rows,
        _dedupe_pr_review_rows,
        _dedupe_pr_rows,
        _dedupe_release_rows,
    )

    github_source = peer_source(peer_id, "github")
    return {
        "contribution_event": storage.read_table(
            data_dir, peer_source(peer_id, "git"), "contribution_event"
        ),
        "pr": _dedupe_pr_rows(storage.read_table(data_dir, github_source, "pr")),
        "pr_review": _dedupe_pr_review_rows(
            storage.read_table(data_dir, github_source, "pr_review")
        ),
        "pr_comment": _dedupe_pr_comment_rows(
            storage.read_table(data_dir, github_source, "pr_comment")
        ),
        "release": _dedupe_release_rows(
            storage.read_table(data_dir, peer_source(peer_id, "release"), "release")
        ),
    }


def _write_snapshot(
    data_dir: str | Path, run_id: str, project_id: str, computed: dict[str, pa.Table]
) -> None:
    out_dir = Path(data_dir) / "snapshots" / "peers" / run_id / project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(computed["metric_value"], out_dir / "metric_value.parquet")
    pq.write_table(computed["pr_backlog"], out_dir / "pr_backlog.parquet")


def _write_settlement(
    data_dir: str | Path, run_id: str, project_id: str, settlement: dict[str, bool]
) -> None:
    """Issue #150: `peer.collect.peer_pass_settlement`'s own per-pass dict,
    written alongside that peer's `metric_value`/`pr_backlog` snapshot
    files as `settlement.json` -- a plain dict, not a parquet table (three
    booleans, no schema worth the ceremony), so `site/peers_page.py` can
    read it with no `project_health.storage`/watermark access of its own
    (same "the site needs no API calls" acceptance this issue's own text
    states). Never written for Cassandra -- its own GitHub collection is
    `nightly.yml`'s ordinary incremental collector, not this module's
    three-pass peer backfill, so no watermark-based gating applies to it at
    all (`site/peers_page.py`'s own missing-file default treats this
    exactly like "fully settled")."""
    out_dir = Path(data_dir) / "snapshots" / "peers" / run_id / project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "settlement.json").write_text(json.dumps(settlement, indent=2, sort_keys=True))


def run_peers_collection(
    peers_config: PeersConfig,
    data_dir: str | Path,
    workdir: str | Path,
    run_id: str,
    *,
    token: str | None = None,
    as_of: date | None = None,
    min_free_disk_bytes: int | None = DEFAULT_MIN_FREE_DISK_BYTES,
) -> PeersRunReport:
    """Collect every peer's raw tables, then compute + snapshot metrics for
    Cassandra and every peer. Never raises on one peer's collection
    failure -- `PeerGitCollectionReport.error`/a `'failed'`/`'skipped'`
    GitHub repo outcome record the problem and the run continues with
    whatever other peers succeeded (same "a flaky repo shouldn't block the
    others" discipline `collectors/github.py`'s own module docstring
    documents).

    `min_free_disk_bytes` (issue #145's own disk-budget rule): checked via
    `shutil.disk_usage` before each peer's git clone; once free space on
    `data_dir`'s filesystem drops at or below this floor, every remaining
    peer's git/release collection is skipped (`disk_budget_skipped_peers`)
    rather than risking a clone that fills the disk -- resumable next run,
    same "stop cleanly, never a hard failure" discipline the GraphQL
    rate-limit floor already uses. `None` (the default) disables the check.
    """
    started_at = datetime.now(timezone.utc)
    as_of = as_of or started_at.date()
    partition_date = started_at.date()
    # `collectors.github.GitHubCollector` (used inside `collect_and_write_
    # github`) already resolves its own token (GH_PAT/GITHUB_TOKEN env, else
    # `gh auth token`) when given `None` -- but `collect_and_write_releases`'
    # plain REST calls (`peers/release.py`) do not, and a real run that
    # forgot to pass a token here hit GitHub's unauthenticated 60 req/hr
    # limit after only a few dozen GA-tag commit-date lookups (verified
    # live, 2026-10-09: apache/kafka's own tag history alone exceeded it).
    # Resolving once, here, means every call this function makes --
    # GitHub PR collection and every peer's release REST calls alike --
    # shares one authenticated token, the same convention `GitHubCollector`
    # already establishes for its own constructor.
    token = token if token is not None else resolve_github_token()

    # Issue #148: rotate which peer this run starts GitHub PR collection at
    # -- `peer_rotation.read_next_start_index` defaults to `0` (today's
    # un-rotated order) whenever `state/peers/rotation.json` is missing,
    # so this is a no-op on the real data branch's first run after this
    # change ships, and advances by exactly one peer every run after that.
    rotation_start_index = peer_rotation.read_next_start_index(data_dir)
    github_report = collect_and_write_github(
        peers_config,
        data_dir,
        run_id,
        partition_date,
        token=token,
        as_of=as_of,
        rotation_start_index=rotation_start_index,
    )
    peer_rotation.write_next_start_index(
        data_dir,
        peer_rotation.next_index(rotation_start_index, len(peers_config.peers)),
    )

    git_reports: list[PeerGitCollectionReport] = []
    release_reports: list[PeerReleaseCollectionReport] = []
    peak_clone_bytes = 0
    disk_budget_skipped_peers: list[str] = []
    for peer in peers_config.peers:
        if min_free_disk_bytes is not None:
            # "/" (not `data_dir`, which may not exist yet on a fresh
            # scratch run): issue #145's own rule is phrased as `df -h /`.
            free_bytes = shutil.disk_usage("/").free
            if free_bytes <= min_free_disk_bytes:
                disk_budget_skipped_peers.append(peer.id)
                continue

        git_report = collect_and_write_git(
            peer,
            peers_config.collection.commit_lookback_months,
            data_dir,
            workdir,
            run_id,
            partition_date,
            peers_config.collection.bot_patterns,
        )
        git_reports.append(git_report)
        peak_clone_bytes = max(peak_clone_bytes, git_report.clone_bytes)

        release_reports.append(
            collect_and_write_releases(peer, data_dir, run_id, partition_date, token=token)
        )

    metrics_by_project: dict[str, dict[str, int]] = {}

    cassandra_tables = _cassandra_tables_for_comparison(data_dir)
    cassandra_computed = compute_peer_metrics(
        cassandra_tables, as_of=as_of, run_id=run_id, computed_at=started_at
    )
    _write_snapshot(data_dir, run_id, CASSANDRA_PROJECT_ID, cassandra_computed)
    metrics_by_project[CASSANDRA_PROJECT_ID] = {
        "metric_value_rows": cassandra_computed["metric_value"].num_rows,
        "pr_backlog_rows": cassandra_computed["pr_backlog"].num_rows,
    }

    peer_settlement: dict[str, dict[str, bool]] = {}
    for peer in peers_config.peers:
        tables = _peer_tables(data_dir, peer.id)
        computed = compute_peer_metrics(tables, as_of=as_of, run_id=run_id, computed_at=started_at)
        _write_snapshot(data_dir, run_id, peer.id, computed)
        metrics_by_project[peer.id] = {
            "metric_value_rows": computed["metric_value"].num_rows,
            "pr_backlog_rows": computed["pr_backlog"].num_rows,
        }
        # Issue #150: read fresh from whatever this run's own
        # `collect_and_write_github` call just persisted -- the true,
        # up-to-the-end-of-this-run settlement state, not the pre-run
        # snapshot that call used for its own budget-allocation decisions.
        settlement = peer_collect.peer_pass_settlement(data_dir, peer)
        peer_settlement[peer.id] = settlement
        _write_settlement(data_dir, run_id, peer.id, settlement)

    completed_at = datetime.now(timezone.utc)
    report = PeersRunReport(
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        github=github_report,
        git_reports=git_reports,
        release_reports=release_reports,
        peak_clone_bytes=peak_clone_bytes,
        metrics_by_project=metrics_by_project,
        disk_budget_skipped_peers=disk_budget_skipped_peers,
        peer_settlement=peer_settlement,
    )

    manifest_path = Path(data_dir) / "manifests" / f"peers-{run_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(report.to_json_dict(), indent=2, sort_keys=True))

    return report


def latest_peers_run_id(data_dir: str | Path) -> str | None:
    """The most recently written `snapshots/peers/<run_id>/` directory
    name, or `None` if peers data has never been collected -- `site/
    peers_page.py`'s own "no snapshot -> no page" gate (same pattern
    `build_thread_explorer_context` uses for its own snapshot family)."""
    base = Path(data_dir) / "snapshots" / "peers"
    if not base.is_dir():
        return None
    candidates = [p for p in base.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).name
