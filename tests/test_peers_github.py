"""Tests for project_health.peers.github (issue #145; fixup, orchestrator
review of PR #147: recent-window PR collection, not oldest-first).

Offline: a small `httpx.MockTransport` stands in for GitHub's GraphQL
endpoint, same pattern as `tests/test_github_collector.py`.
"""

from __future__ import annotations

import json
from datetime import date

import httpx

from project_health.collectors.github import GitHubCollector
from project_health.peers.config import BotPatternConfig, JiraReleaseVerification, PeerProject
from project_health.peers.github import (
    PassState,
    build_peer_github_config,
    run_closed_search_pass,
    run_created_desc_pass,
    run_open_prs_pass,
)

REPO = "apache/kafka"
_RATE_LIMIT_OK = {"remaining": 4999, "resetAt": "x", "cost": 1}
_RATE_LIMIT_FLOOR = {"remaining": 400, "resetAt": "x", "cost": 1}


def _peer(peer_id: str, repo: str) -> PeerProject:
    return PeerProject(
        id=peer_id,
        display_name=peer_id,
        repo=repo,
        default_branch="main",
        tag_prefix="",
        release_verification=JiraReleaseVerification(base_url="https://x", project_key="X"),
    )


def _pr_node(number: int, created_at: str, author: str = "alice") -> dict:
    return {
        "id": f"PR_{number}",
        "number": number,
        "state": "OPEN",
        "title": f"pr {number}",
        "isDraft": False,
        "merged": False,
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
        "author": {"login": author},
        "createdAt": created_at,
        "updatedAt": created_at,
        "closedAt": None,
        "mergedAt": None,
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
    }


def _collector(transport: httpx.MockTransport) -> GitHubCollector:
    peers = [_peer("kafka", REPO)]
    config = build_peer_github_config(peers, [])
    return GitHubCollector(config, token="x", transport=transport)


def test_build_peer_github_config_covers_every_peer_repo():
    peers = [_peer("kafka", "apache/kafka"), _peer("spark", "apache/spark")]
    config = build_peer_github_config(peers, [])
    assert config.pull_requests.repos == ["apache/kafka", "apache/spark"]


def test_build_peer_github_config_translates_bot_patterns():
    patterns = [BotPatternConfig(field="github_login", regex=r"^dependabot")]
    config = build_peer_github_config([], patterns)
    assert config.bot_patterns[0].field == "github_login"
    assert config.bot_patterns[0].regex == r"^dependabot"


class TestCreatedDescPass:
    def test_stops_once_below_window_start(self):
        """Newest-first page includes one PR before window_start -- that
        PR, and the implied rest of a DESC-ordered page, must be dropped,
        and the pass must report 'completed' (reached its stop condition,
        not a budget floor)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimit": _RATE_LIMIT_OK,
                        "repository": {
                            "pullRequests": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                "nodes": [
                                    _pr_node(3, "2026-09-15T00:00:00Z"),
                                    _pr_node(2, "2026-01-01T00:00:00Z"),  # before window_start
                                ],
                            }
                        },
                    }
                },
            )

        collector = _collector(httpx.MockTransport(handler))
        result = run_created_desc_pass(
            collector, REPO, PassState(), window_start=date(2026, 6, 1), bot_patterns=[],
            source_snapshot_id="s1",
        )
        assert result.status == "completed"
        assert [r["number"] for r in result.pr_rows] == [3]
        assert result.next_state.cursor is None
        assert result.next_state.high_watermark == "2026-09-15T00:00:00+00:00"

    def test_stops_at_rate_limit_floor_and_is_resumable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimit": _RATE_LIMIT_FLOOR,
                        "repository": {
                            "pullRequests": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                "nodes": [_pr_node(5, "2026-09-20T00:00:00Z")],
                            }
                        },
                    }
                },
            )

        collector = _collector(httpx.MockTransport(handler))
        result = run_created_desc_pass(
            collector, REPO, PassState(), window_start=date(2026, 1, 1), bot_patterns=[],
            source_snapshot_id="s1",
        )
        assert result.status == "partial"
        assert result.next_state.cursor == "c1"
        # newest_seen is carried forward for when the backfill eventually completes
        assert result.next_state.newest_seen == "2026-09-20T00:00:00+00:00"

    def test_stops_at_max_pages_with_distinct_status_from_rate_limit(self):
        """Fixup round 1 (real-run finding, 2026-10-09): a page cap is a
        per-(peer, pass) fairness limit, not genuine budget exhaustion --
        it must report a status distinct from `'partial'` (the real
        rate-limit floor) so `peers/collect.py` knows not to skip every
        other peer's turn because of it."""
        pages_seen = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal pages_seen
            pages_seen += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimit": _RATE_LIMIT_OK,  # plenty of budget left
                        "repository": {
                            "pullRequests": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                "nodes": [_pr_node(5, "2026-09-20T00:00:00Z")],
                            }
                        },
                    }
                },
            )

        collector = _collector(httpx.MockTransport(handler))
        result = run_created_desc_pass(
            collector, REPO, PassState(), window_start=date(2026, 1, 1), bot_patterns=[],
            source_snapshot_id="s1", max_pages=2,
        )
        assert result.status == "page_capped"
        assert pages_seen == 2
        assert result.next_state.cursor == "c1"

    def test_restarts_from_top_using_high_watermark_once_completed(self):
        """A pass that previously completed restarts from the top
        (cursor=None) and stops as soon as it reaches its own
        high_watermark -- never re-walks the whole window again."""
        seen_cursors = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read().decode())
            seen_cursors.append(payload["variables"]["cursor"])
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimit": _RATE_LIMIT_OK,
                        "repository": {
                            "pullRequests": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                "nodes": [
                                    _pr_node(10, "2026-10-01T00:00:00Z"),
                                    _pr_node(9, "2026-07-01T00:00:00Z"),  # below high_watermark
                                ],
                            }
                        },
                    }
                },
            )

        collector = _collector(httpx.MockTransport(handler))
        prior_state = PassState(high_watermark="2026-08-01T00:00:00+00:00")
        result = run_created_desc_pass(
            collector, REPO, prior_state, window_start=date(2025, 1, 1), bot_patterns=[],
            source_snapshot_id="s1",
        )
        assert seen_cursors == [None]  # restarted from the top, not from any stale cursor
        assert [r["number"] for r in result.pr_rows] == [10]
        assert result.status == "completed"
        assert result.next_state.high_watermark == "2026-10-01T00:00:00+00:00"


class TestOpenPrsPass:
    def test_pages_until_exhausted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read().decode())
            cursor = payload["variables"]["cursor"]
            if cursor is None:
                nodes = [_pr_node(1, "2020-01-01T00:00:00Z")]
                page_info = {"hasNextPage": True, "endCursor": "page2"}
            else:
                nodes = [_pr_node(2, "2021-01-01T00:00:00Z")]
                page_info = {"hasNextPage": False, "endCursor": None}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimit": _RATE_LIMIT_OK,
                        "repository": {"pullRequests": {"pageInfo": page_info, "nodes": nodes}},
                    }
                },
            )

        collector = _collector(httpx.MockTransport(handler))
        result = run_open_prs_pass(
            collector, REPO, PassState(), bot_patterns=[], source_snapshot_id="s1"
        )
        assert result.status == "completed"
        assert [r["number"] for r in result.pr_rows] == [1, 2]
        assert result.next_state.cursor is None


class TestClosedSearchPass:
    def test_builds_search_query_and_captures_issue_count(self):
        seen_queries = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read().decode())
            seen_queries.append(payload["variables"]["searchQuery"])
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimit": _RATE_LIMIT_OK,
                        "search": {
                            "issueCount": 42,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [_pr_node(7, "2026-01-01T00:00:00Z")],
                        },
                    }
                },
            )

        collector = _collector(httpx.MockTransport(handler))
        result = run_closed_search_pass(
            collector, REPO, PassState(), window_start=date(2025, 9, 1), bot_patterns=[],
            source_snapshot_id="s1",
        )
        assert seen_queries == ["repo:apache/kafka is:pr closed:>=2025-09-01"]
        assert result.issue_count == 42
        assert [r["number"] for r in result.pr_rows] == [7]
        assert result.status == "completed"


def test_pass_state_json_round_trip():
    state = PassState(cursor="abc", high_watermark="2026-01-01T00:00:00+00:00", newest_seen=None)
    restored = PassState.from_json(state.to_json())
    assert restored == state


def test_pass_state_from_none_is_empty():
    assert PassState.from_json(None) == PassState()
