"""Tests for project_health.collectors.github_checks (issue #36).

All tests use an offline `httpx.MockTransport` -- never the network.
"""

from __future__ import annotations

import httpx
import pytest

from project_health.collectors.github_checks import GitHubChecksCollector


def _check_runs_response(runs: list[dict]) -> dict:
    return {"total_count": len(runs), "check_runs": runs}


def _run(name: str, conclusion: str | None, status: str = "completed") -> dict:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/apache/cassandra/runs/{name}",
    }


def _transport(runs_by_sha: dict[str, list[dict]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        sha = parts[5]
        if sha not in runs_by_sha:
            return httpx.Response(422, json={"message": "No commit found"})
        return httpx.Response(200, json=_check_runs_response(runs_by_sha[sha]))

    return httpx.MockTransport(handler)


def _collector(runs_by_sha: dict[str, list[dict]], **kwargs) -> GitHubChecksCollector:
    return GitHubChecksCollector(
        "apache",
        "cassandra",
        transport=_transport(runs_by_sha),
        min_request_interval=0,
        sleep_fn=lambda _seconds: None,
        **kwargs,
    )


class TestGitHubChecksCollector:
    def test_fetch_check_runs_returns_all_runs(self):
        sha = "a" * 40
        runs = [_run("ant-check-jdk11", "success"), _run("some-other-check", "neutral")]
        with _collector({sha: runs}) as collector:
            fetched = collector.fetch_check_runs(sha)
        assert len(fetched) == 2
        assert {r.name for r in fetched} == {"ant-check-jdk11", "some-other-check"}

    def test_fetch_checkstyle_evidence_filters_to_checkstyle_runs_only(self):
        sha = "a" * 40
        runs = [_run("ant-check-jdk11", "success"), _run("some-other-check", "neutral")]
        with _collector({sha: runs}) as collector:
            evidence = collector.fetch_checkstyle_evidence(sha)
        assert len(evidence) == 1
        assert evidence[0].check_run_name == "ant-check-jdk11"
        assert evidence[0].conclusion == "success"

    def test_unknown_sha_returns_empty_tuple_not_an_error(self):
        with _collector({}) as collector:
            assert collector.fetch_checkstyle_evidence("f" * 40) == ()

    def test_fetch_checkstyle_evidence_for_shas_omits_shas_with_no_runs(self):
        sha1 = "a" * 40
        sha2 = "b" * 40
        runs = {sha1: [_run("ant-check-jdk11", "success")]}
        with _collector(runs) as collector:
            result = collector.fetch_checkstyle_evidence_for_shas([sha1, sha2])
        assert set(result) == {sha1}

    def test_uses_token_from_explicit_arg(self):
        collector = GitHubChecksCollector(
            "apache", "cassandra", token="explicit-token", transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=_check_runs_response([]))
            )
        )
        assert collector.authenticated is True
        collector.close()

    def test_uses_token_from_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        collector = GitHubChecksCollector(
            "apache", "cassandra", transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=_check_runs_response([]))
            )
        )
        assert collector.authenticated is True
        collector.close()

    def test_unauthenticated_when_no_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        collector = GitHubChecksCollector(
            "apache", "cassandra", transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=_check_runs_response([]))
            )
        )
        assert collector.authenticated is False
        collector.close()

    @pytest.mark.parametrize("status_code", [500, 429])
    def test_retries_on_5xx_and_429_then_succeeds(self, status_code):
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(status_code)
            return httpx.Response(200, json=_check_runs_response([]))

        collector = GitHubChecksCollector(
            "apache",
            "cassandra",
            transport=httpx.MockTransport(handler),
            min_request_interval=0,
            sleep_fn=lambda _seconds: None,
        )
        with collector:
            assert collector.fetch_check_runs("a" * 40) == ()
        assert attempts["count"] == 2
