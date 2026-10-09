"""GitHub PR/review/comment collection for peer repos (issue #145).

Reuses `collectors.github.GitHubCollector` **unmodified** -- it only needs
duck-typed `config.pull_requests.repos` (a list of `"owner/name"` strings)
and `config.bot_patterns` (a list of `project_health.config.BotPattern`),
per its own constructor. Rather than constructing a full
`project_health.config.ProjectConfig` (which requires a `reviewer_extraction`
block peers have no use for), this module builds a minimal object exposing
just those two attributes.

Every peer repo is passed to **one** `GitHubCollector.collect()` call, not
one call per peer: `GitHubCollector`'s own "Rate-limit budgeting" (module
docstring) already shares one run's GraphQL point budget across every
configured repo, stopping cleanly and marking the rest `'skipped'` once the
floor is reached -- exactly the "budgeted against the GraphQL rate limit"
behavior issue #145 asks `peers.yml` for, with no new budgeting code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from project_health.collectors.github import GitHubCollectionResult, GitHubCollector
from project_health.config import BotPattern
from project_health.peers.config import BotPatternConfig, PeerProject


@dataclass
class _PullRequestsSection:
    repos: list[str]


@dataclass
class _PeerGithubConfig:
    """Duck-typed stand-in for `project_health.config.ProjectConfig` --
    `GitHubCollector.__init__` only reads `.pull_requests.repos` and
    `.bot_patterns` off whatever it's given."""

    pull_requests: _PullRequestsSection
    bot_patterns: list[BotPattern] = field(default_factory=list)


def _to_bot_patterns(patterns: list[BotPatternConfig]) -> list[BotPattern]:
    return [BotPattern(field=p.field, regex=p.regex) for p in patterns]


def build_peer_github_config(
    peers: list[PeerProject], bot_patterns: list[BotPatternConfig]
) -> _PeerGithubConfig:
    """One duck-typed config covering every peer's repo -- see module
    docstring for why this is a single config/collect() call, not one per
    peer."""
    return _PeerGithubConfig(
        pull_requests=_PullRequestsSection(repos=[peer.repo for peer in peers]),
        bot_patterns=_to_bot_patterns(bot_patterns),
    )


def collect_peer_prs(
    peers: list[PeerProject],
    bot_patterns: list[BotPatternConfig],
    *,
    token: str | None = None,
    watermarks: dict[str, str | None] | None = None,
    rate_limit_floor: int = 500,
    max_prs_per_repo: int | None = None,
    transport=None,
) -> GitHubCollectionResult:
    """Collect PRs/reviews/comments for every peer repo in one shared-budget
    `GitHubCollector.collect()` call.

    `watermarks` maps `"owner/name" -> prior next_watermark` exactly like
    `GitHubCollector.collect` itself (a repo missing from the mapping starts
    a full backfill for that repo). Returns the collector's own
    `GitHubCollectionResult` unchanged -- callers (`peers.collect`) split its
    `prs`/`reviews`/`comments` tables by `repo` before writing each peer's
    own `raw/peers/<id>/...` partition. `transport` is test-only
    (`httpx.MockTransport`), forwarded straight to `GitHubCollector`.
    """
    config = build_peer_github_config(peers, bot_patterns)
    with GitHubCollector(
        config, token=token, rate_limit_floor=rate_limit_floor, transport=transport
    ) as collector:
        return collector.collect(watermarks=watermarks, max_prs_per_repo=max_prs_per_repo)
