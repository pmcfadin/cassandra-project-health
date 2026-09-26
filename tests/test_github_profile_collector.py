"""Tests for project_health.collectors.github_profile (issue #52, D6)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from project_health.collectors.github_profile import (
    CollectionError,
    GitHubProfileCollector,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github_profile"
UTC = timezone.utc
NOW = datetime(2026, 9, 25, tzinfo=UTC)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _collector(transport: httpx.MockTransport) -> GitHubProfileCollector:
    return GitHubProfileCollector(
        transport=transport,
        min_request_interval=0,
        sleep_fn=lambda s: None,
        max_retries=2,
    )


def test_collect_fetches_company_for_each_login():
    profiles = {
        "octo-dev": _load("user_with_company.json"),
        "octo-nocorp": _load("user_no_company.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        login = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=profiles[login])

    with _collector(_transport(handler)) as collector:
        result = collector.collect(
            ["octo-dev", "octo-nocorp"], snapshot_id="snap-1", now_fn=lambda: NOW
        )

    assert result.profiles_collected == 2
    assert result.rate_limited is False
    rows = {row["login"]: row for row in result.profiles.to_pylist()}
    assert rows["octo-dev"]["company"] == "@examplecorp"
    assert rows["octo-nocorp"]["company"] is None
    assert rows["octo-dev"]["fetched_at"] == NOW
    assert rows["octo-dev"]["source_snapshot_id"] == "snap-1"


def test_collect_caches_a_404_as_company_none_instead_of_erroring():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=_load("user_not_found.json"))

    with _collector(_transport(handler)) as collector:
        result = collector.collect(["ghost-login"], snapshot_id="snap-1", now_fn=lambda: NOW)

    assert result.profiles_collected == 1
    row = result.profiles.to_pylist()[0]
    assert row["login"] == "ghost-login"
    assert row["company"] is None


def test_collect_stops_cleanly_on_rate_limit_keeping_partial_results():
    profiles = {"first-login": _load("user_with_company.json")}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        login = request.url.path.rsplit("/", 1)[-1]
        if login == "first-login":
            return httpx.Response(200, json=profiles[login])
        # Every other login hits GitHub's documented rate-limit response.
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            json=_load("rate_limit_error.json"),
        )

    with _collector(_transport(handler)) as collector:
        result = collector.collect(
            ["first-login", "second-login", "third-login"],
            snapshot_id="snap-1",
            now_fn=lambda: NOW,
        )

    # first-login fetched successfully; the run then stops cleanly at the
    # rate limit instead of raising or discarding what it already fetched.
    assert result.profiles_collected == 1
    assert result.rate_limited is True
    assert result.profiles.to_pylist()[0]["login"] == "first-login"


def test_collect_respects_max_profiles_budget():
    profiles = {"a": _load("user_with_company.json"), "b": _load("user_no_company.json")}

    def handler(request: httpx.Request) -> httpx.Response:
        login = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=profiles[login])

    with _collector(_transport(handler)) as collector:
        result = collector.collect(
            ["a", "b"], snapshot_id="snap-1", max_profiles=1, now_fn=lambda: NOW
        )

    assert result.profiles_collected == 1
    assert result.profiles.to_pylist()[0]["login"] == "a"
    assert result.rate_limited is False


def test_collect_empty_login_list_returns_empty_table():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("no request should be made for an empty login list")

    with _collector(_transport(handler)) as collector:
        result = collector.collect([], snapshot_id="snap-1")

    assert result.profiles_collected == 0
    assert result.profiles.num_rows == 0


def test_collect_raises_collection_error_after_exhausting_retries_on_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal error"})

    with _collector(_transport(handler)) as collector:
        with pytest.raises(CollectionError):
            collector.collect(["flaky-login"], snapshot_id="snap-1", now_fn=lambda: NOW)


def test_truncated_json_body_retries_like_a_5xx_then_succeeds():
    """issue #86: a truncated/undecodable profile body is retried exactly
    like a 5xx, not raised straight through."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(200, content=b'{"company": "Ac')
        return httpx.Response(200, json=_load("user_with_company.json"))

    with _collector(_transport(handler)) as collector:
        result = collector.collect(["a"], snapshot_id="snap-1", now_fn=lambda: NOW)

    assert attempts["count"] == 2
    assert result.profiles_collected == 1


def test_truncated_json_body_exhausted_raises_collection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"company": "Ac')

    with _collector(_transport(handler)) as collector:
        with pytest.raises(CollectionError):
            collector.collect(["flaky-login"], snapshot_id="snap-1", now_fn=lambda: NOW)
