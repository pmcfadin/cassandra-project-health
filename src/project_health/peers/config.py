"""`projects/peers.yaml` loader (issue #145).

Deliberately a separate, much smaller model than
`project_health.config.ProjectConfig` -- a peer needs only enough to drive
`collectors.github.GitHubCollector` (reused unmodified), a disk-safe git
clone, and GA-tag discovery; it has no JIRA/mailing-list/roster/affiliation
config at all (DECISIONS.md D30: "No issue-tracker metrics in v1").

`model_config = ConfigDict(extra="allow")` at every level, matching
`project_health.config`'s own convention, so an unknown/future key never
breaks a load.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_COMMIT_LOOKBACK_MONTHS = 48
# Issue #150 (live, 2026-10-10): the nightly pipeline (`nightly.yml`) shares
# this same GitHub token's one hourly GraphQL point budget with this
# workflow -- real-run finding: a Sunday peers run that drove `remaining`
# down to the old default (500) left too little headroom, and the
# *following* nightly run came back `github: partial` / `github_commit_
# authors: partial` purely because this workflow had already spent nearly
# the whole budget. `github_rate_limit_floor` (passed straight through to
# `collectors.github.GitHubCollector`'s own `remaining <= floor` stop,
# `peers/collect.py::collect_and_write_github`) is this run's *reserve*:
# once GraphQL `remaining` drops to or below it, every further (peer, pass)
# this run stops cleanly (same budget-floor discipline that module's own
# docstring already documents), leaving at least this many points for
# whatever runs next on the same token. 2,500 -- half of GitHub's 5,000-
# point hourly GraphQL allowance -- is comfortably more than a nightly run
# has ever needed (`DATA-SOURCES.md`/real-run history), never the 500 this
# workflow used before switching its own schedule off the nightly's
# observed run window too (`.github/workflows/peers.yml`'s own module
# comment).
DEFAULT_GITHUB_RATE_LIMIT_FLOOR = 2500
# Real-run finding (2026-10-09, fixup round 1): apache/kafka's own
# `created_desc` pass alone consumed the whole shared GraphQL budget (96
# pages, ~4,800 PRs) before reaching its window-start stop condition,
# leaving every other peer's every pass `'skipped'` for the entire run.
# Capping each (peer, pass) at this many pages per run means a single
# high-volume repo can no longer starve the other four of any progress at
# all -- every peer gets *some* movement on *some* pass each run, even
# though a repo this size still needs several runs to finish its own
# `created_desc` backfill (same "it may span multiple runs" acceptance
# this issue's own "Build" section anticipated).
DEFAULT_MAX_PAGES_PER_PASS = 20

# Issue #148: after 3-4 real weekly runs, kafka/spark's own `created_desc`
# backfill alone consumed the whole shared GraphQL budget every single run
# (same finding as `DEFAULT_MAX_PAGES_PER_PASS` above -- 96 pages before
# apache/kafka's own `created_desc` pass hit the real rate-limit floor),
# starving flink/pulsar/datafusion of *any* progress across every run so
# far, since `peers:` config order never changes. `96` here is that same
# real-run page count, used as this run's total shared page budget across
# every peer+pass combination -- `peers.collect.collect_and_write_github`
# divides whatever of this budget remains among the peers not yet "done"
# this run (`peers.rotation`'s fair-share split), on top of (never
# replacing) the per-(peer, pass) `DEFAULT_MAX_PAGES_PER_PASS` ceiling and
# the real `github_rate_limit_floor` stop below.
DEFAULT_GITHUB_PAGE_BUDGET_PER_RUN = 96

# Issue #150 fixup (this issue, live backfill finding 2026-10-13): a peer's
# `open_prs`/`closed_search` pass genuinely does complete, in full, from
# time to time -- but `peer_pass_settlement`'s old rule (`PassState.cursor
# is not None` -> unsettled) treated *any* later partial/page-capped run of
# that same pass as if it had never completed at all, even though a
# perfectly good, complete snapshot already existed. On CI's page-capped
# budget, some peer is mid-refresh on essentially every run, so this flipped
# `/peers/`'s Change Request Closure Ratio (gated on `closed_search`) and
# Open PR backlog (gated on `open_prs`) between visible and hidden on
# almost every single run -- see `peers/collect.py::peer_pass_settlement`'s
# own module comment for the full mechanism and fix.
#
# `settlement_freshness_days` makes that fix's "sticky" settlement bounded
# rather than unconditionally permanent: once a pass completes, it stays
# settled through later partial runs for up to this many days since its own
# `last_completed_at` -- long enough to ride out several page-capped runs in
# a row (this workflow's own cadence, `.github/workflows/peers.yml`), short
# enough that a pass that's gone genuinely stale (no completed run in two
# weeks) eventually falls back to "Collecting" rather than silently showing
# an arbitrarily old snapshot forever. `None` disables the freshness bound
# entirely (sticky forever, once completed).
DEFAULT_SETTLEMENT_FRESHNESS_DAYS = 14

# Same fixup: a pass that completed within this many hours is skipped
# outright on this run's own turn -- no API call, no page spent -- instead
# of redoing a "cheap to redo in full" pass (`peers/github.py`'s own
# module docstring) that just finished. This is the "reduce waste" half of
# the fix: a peer whose `open_prs`/`closed_search` already completed today
# needs none of this run's shared page budget, freeing it for whichever
# peer/pass hasn't caught up yet (`created_desc` backfills in particular).
# Deliberately just under 24h, not exactly 24h, so a daily cron run a few
# minutes early two days running still re-checks rather than drifting later
# and later each day.
DEFAULT_SKIP_RERUN_WITHIN_HOURS = 20


class BotPatternConfig(BaseModel):
    """One entry in `collection.bot_patterns` -- same shape as
    `project_health.config.BotPattern`, duplicated here rather than
    imported so peer config never depends on the Cassandra-specific
    `ProjectConfig` module."""

    model_config = ConfigDict(extra="allow")

    field: str
    regex: str


class JiraReleaseVerification(BaseModel):
    """`release_verification: {type: jira, ...}` -- cross-checks a peer's
    git-tag-derived GA release count against that JIRA project's own
    released versions (`/rest/api/2/project/<key>/versions`)."""

    model_config = ConfigDict(extra="allow")

    type: str = "jira"
    base_url: str
    project_key: str


class GithubReleasesVerification(BaseModel):
    """`release_verification: {type: github_releases}` -- cross-checks
    against the peer repo's own published GitHub Releases."""

    model_config = ConfigDict(extra="allow")

    type: str = "github_releases"


class PypiReleaseVerification(BaseModel):
    """`release_verification: {type: pypi, package: ...}` -- cross-checks
    against a PyPI package's own release history (used only where neither
    JIRA nor GitHub Releases is an available independent source -- see
    `projects/peers.yaml`'s datafusion entry for why)."""

    model_config = ConfigDict(extra="allow")

    type: str = "pypi"
    package: str


ReleaseVerification = (
    JiraReleaseVerification | GithubReleasesVerification | PypiReleaseVerification
)


def _parse_release_verification(raw: dict[str, Any]) -> ReleaseVerification:
    kind = raw.get("type")
    if kind == "jira":
        return JiraReleaseVerification.model_validate(raw)
    if kind == "github_releases":
        return GithubReleasesVerification.model_validate(raw)
    if kind == "pypi":
        return PypiReleaseVerification.model_validate(raw)
    raise ValueError(f"unknown release_verification.type {kind!r}")


class PeerProject(BaseModel):
    """One `peers:` entry."""

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str
    repo: str  # "owner/name"
    default_branch: str
    tag_prefix: str = ""
    release_verification: ReleaseVerification

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1]


class CollectionConfig(BaseModel):
    """`collection:` block -- shared knobs across every peer."""

    model_config = ConfigDict(extra="allow")

    commit_lookback_months: int = DEFAULT_COMMIT_LOOKBACK_MONTHS
    github_rate_limit_floor: int = DEFAULT_GITHUB_RATE_LIMIT_FLOOR
    # The effective reserve is min(floor, fraction x the token's hourly
    # GraphQL limit), so the Actions GITHUB_TOKEN's ~1,000-point allowance is
    # not swallowed whole by a 2,500-point floor (live stall, 2026-10-11).
    github_rate_limit_floor_fraction: float | None = 0.5
    max_pages_per_pass: int = DEFAULT_MAX_PAGES_PER_PASS
    # Issue #148: this run's total shared page budget, split fairly across
    # whichever peers aren't already "done" this run (see
    # `DEFAULT_GITHUB_PAGE_BUDGET_PER_RUN`'s own comment above).
    github_page_budget_per_run: int = DEFAULT_GITHUB_PAGE_BUDGET_PER_RUN
    # Issue #150 fixup: how long a completed `open_prs`/`closed_search`/
    # `created_desc` pass stays "settled" through later partial runs, and
    # how soon after completing it's skipped outright on its own next turn
    # -- see the two `DEFAULT_*` constants' own comments above.
    settlement_freshness_days: int | None = DEFAULT_SETTLEMENT_FRESHNESS_DAYS
    skip_rerun_within_hours: int | None = DEFAULT_SKIP_RERUN_WITHIN_HOURS
    bot_patterns: list[BotPatternConfig] = Field(default_factory=list)


class PeersConfig(BaseModel):
    """Root config loaded from `projects/peers.yaml`."""

    model_config = ConfigDict(extra="allow")

    peers: list[PeerProject]
    collection: CollectionConfig = Field(default_factory=CollectionConfig)

    def get(self, peer_id: str) -> PeerProject:
        for peer in self.peers:
            if peer.id == peer_id:
                return peer
        raise KeyError(f"no such peer: {peer_id!r}")


def load_peers(path: str | Path) -> PeersConfig:
    """Load and validate a `projects/peers.yaml`-shaped file.

    Raises `FileNotFoundError` if `path` doesn't exist, and
    `pydantic.ValidationError` if a required field is missing/malformed.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"peers config not found: {path}")

    raw: Any = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}

    peers_raw = raw.get("peers", [])
    peers = []
    for entry in peers_raw:
        entry = dict(entry)
        entry["release_verification"] = _parse_release_verification(
            entry.get("release_verification", {})
        )
        peers.append(PeerProject.model_validate(entry))

    collection = CollectionConfig.model_validate(raw.get("collection", {}) or {})
    return PeersConfig(peers=peers, collection=collection)
