"""Tests for project_health.collectors.github_commit_authors (issue #52
fixup cycle 1, D6)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from project_health.collectors.github_commit_authors import GitHubCommitAuthorCollector
from project_health.config import load_project

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github_commit_authors"
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_project(REPO_ROOT / "projects" / "cassandra.yaml")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _collector(transport: httpx.MockTransport, **kwargs) -> GitHubCommitAuthorCollector:
    return GitHubCommitAuthorCollector(
        CONFIG,
        token="test-token",
        transport=transport,
        min_request_interval=0,
        sleep_fn=lambda s: None,
        max_retries=2,
        **kwargs,
    )


def test_collect_walks_pages_and_only_keeps_rows_with_a_linked_login():
    pages = [_load("history_page1.json"), _load("history_page2.json")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages.pop(0))

    with _collector(httpx.MockTransport(handler)) as collector:
        result = collector.collect(watermark=None, snapshot_id="snap-1")

    assert result.outcome.status == "ok"
    assert result.outcome.commits_seen == 3
    assert result.outcome.pages_fetched == 2
    # The no-github-account commit (page 2) contributes no row.
    assert result.outcome.associations_found == 2
    rows = {row["email"]: row for row in result.associations.to_pylist()}
    assert rows["octo-dev@apache.org"]["login"] == "octo-dev"
    assert rows["octo-dev@apache.org"]["sha"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert rows["octo-personal@example.net"]["login"] == "octo-personal-gh"
    assert "no-github-account@example.org" not in rows
    assert result.outcome.next_watermark == "cccccccccccccccccccccccccccccccccccccccc 3"


def test_collect_stops_at_max_pages_budget_and_reports_partial():
    pages = [_load("history_page1.json"), _load("history_page2.json")]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=pages[calls["n"] - 1])

    with _collector(httpx.MockTransport(handler)) as collector:
        result = collector.collect(watermark=None, snapshot_id="snap-1", max_pages=1)

    assert result.outcome.status == "partial"
    assert result.outcome.pages_fetched == 1
    assert calls["n"] == 1
    # Watermark still advances to the last successfully processed page, so
    # a follow-up run resumes cleanly instead of re-fetching page 1.
    assert result.outcome.next_watermark == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 2"


def test_collect_stops_at_proactive_rate_limit_floor_and_reports_partial():
    page1 = _load("history_page1.json")
    page1["data"]["rateLimit"]["remaining"] = 100  # <= a floor of 500

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page1)

    with _collector(httpx.MockTransport(handler), rate_limit_floor=500) as collector:
        result = collector.collect(watermark=None, snapshot_id="snap-1")

    assert result.outcome.status == "partial"
    assert result.outcome.pages_fetched == 1
    assert result.outcome.associations_found == 2


def test_collect_stops_cleanly_on_reactive_rate_limited_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("rate_limited_error.json"))

    with _collector(httpx.MockTransport(handler)) as collector:
        result = collector.collect(watermark="some-prior-cursor", snapshot_id="snap-1")

    assert result.outcome.status == "rate_limited"
    assert result.outcome.associations_found == 0
    # Watermark is unchanged -- no progress was made this call.
    assert result.outcome.next_watermark == "some-prior-cursor"


def test_collect_resumes_from_a_stored_watermark():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["variables"]["cursor"] == "prior-cursor"
        return httpx.Response(200, json=_load("history_page2.json"))

    with _collector(httpx.MockTransport(handler)) as collector:
        result = collector.collect(watermark="prior-cursor", snapshot_id="snap-1")

    assert result.outcome.status == "ok"
    assert result.outcome.pages_fetched == 1


def test_collect_marks_failed_after_exhausting_retries_on_server_error():
    """Mirrors collectors/github.py's own contract: a hard, non-rate-limit
    `CollectionError` after retries are exhausted is caught and reported as
    `status='failed'` in the outcome, never raised -- a single collector
    failure must not abort the whole pipeline run (ARCHITECTURE.md §7.3)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal error"})

    with _collector(httpx.MockTransport(handler)) as collector:
        result = collector.collect(watermark=None, snapshot_id="snap-1")

    assert result.outcome.status == "failed"
    assert result.outcome.error is not None
    assert result.associations.num_rows == 0


def test_collector_requires_a_configured_repo():
    config = CONFIG.model_copy(update={"repos": []})
    with pytest.raises(ValueError, match="config.repos"):
        GitHubCommitAuthorCollector(config)
