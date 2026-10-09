"""Tests for project_health.peers.github (issue #145).

Offline: a small `httpx.MockTransport` stands in for GitHub's GraphQL
endpoint, same pattern as `tests/test_github_collector.py`.
"""

from __future__ import annotations

import json

import httpx

from project_health.peers.config import BotPatternConfig, JiraReleaseVerification, PeerProject
from project_health.peers.github import build_peer_github_config, collect_peer_prs


def _peer(peer_id: str, repo: str) -> PeerProject:
    return PeerProject(
        id=peer_id,
        display_name=peer_id,
        repo=repo,
        default_branch="main",
        tag_prefix="",
        release_verification=JiraReleaseVerification(base_url="https://x", project_key="X"),
    )


def test_build_peer_github_config_covers_every_peer_repo():
    peers = [_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    config = build_peer_github_config(peers, [])
    assert config.pull_requests.repos == ["apache/kafka", "apache/spark"]


def test_build_peer_github_config_translates_bot_patterns():
    patterns = [BotPatternConfig(field="github_login", regex=r"^dependabot")]
    config = build_peer_github_config([], patterns)
    assert config.bot_patterns[0].field == "github_login"
    assert config.bot_patterns[0].regex == r"^dependabot"


def _empty_page_response(repo_name: str) -> dict:
    return {
        "data": {
            "rateLimit": {"remaining": 4999, "resetAt": "2026-10-09T00:00:00Z", "cost": 1},
            "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                }
            },
        }
    }


def test_collect_peer_prs_shares_one_budget_across_repos():
    """One `collect()` call, both peer repos represented in the result --
    the shared rate-limit-floor budgeting this module explicitly reuses
    rather than reimplementing (module docstring)."""
    seen_repos: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        name = payload["variables"]["name"]
        seen_repos.append(name)
        return httpx.Response(200, json=_empty_page_response(name))

    transport = httpx.MockTransport(handler)
    peers = [_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]

    result = collect_peer_prs(peers, [], token="x", transport=transport)

    assert set(result.repos) == {"apache/kafka", "apache/spark"}
    assert seen_repos == ["kafka", "spark"]
