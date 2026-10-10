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
DEFAULT_GITHUB_RATE_LIMIT_FLOOR = 500
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
    max_pages_per_pass: int = DEFAULT_MAX_PAGES_PER_PASS
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
