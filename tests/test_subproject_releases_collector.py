"""Tests for project_health.collectors.subproject_releases (issue #175).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from **real, live-fetched fixtures**
(`tests/fixtures/subproject_releases/*.json` -- recorded 2026-10-12 from
`issues.apache.org/jira` and `api.github.com`, see the issue's own
verified-sources findings). Retries use a no-op `sleep_fn` so tests run
instantly, same offline pattern as `test_release_lines_collector.py`.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

from project_health.collectors.subproject_releases import (
    SubprojectReleasesCollector,
)
from project_health.config import load_project

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "subproject_releases"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


CASSJAVA_VERSIONS = _load_fixture("cassjava_versions.json")

# fixVersion name -> (open fixture, total fixture), for CASSJAVA's own
# unreleased versions (verified live 2026-10-12).
_CASSJAVA_COUNTS = {
    "4.18.0": ("cassjava_search_4_18_0_open.json", "cassjava_search_4_18_0_total.json"),
    "3.13.0": ("cassjava_search_3_13_0_open.json", "cassjava_search_3_13_0_total.json"),
    "5.0.0": ("cassjava_search_5_0_0_open.json", "cassjava_search_5_0_0_total.json"),
    "4.19.4": ("cassjava_search_4_19_4_open.json", "cassjava_search_4_19_4_total.json"),
}

GOCQL_RUNS = _load_fixture("gocql_actions_runs.json")
JAVA_DRIVER_RUNS = _load_fixture("java_driver_actions_runs.json")
ACCORD_RUNS = _load_fixture("accord_actions_runs.json")


def _extract_fix_version(url: httpx.URL) -> str | None:
    jql = url.params.get("jql", "")
    marker = 'fixVersion="'
    start = jql.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = jql.find('"', start)
    return urllib.parse.unquote(jql[start:end])


def _jira_handler(request: httpx.Request) -> httpx.Response:
    url_str = str(request.url)
    if "/project/CASSJAVA/versions" in url_str:
        return httpx.Response(200, json=CASSJAVA_VERSIONS)
    if "/rest/api/2/search" in url_str:
        fix_version = _extract_fix_version(request.url)
        jql = request.url.params.get("jql", "")
        unresolved = "resolution=Unresolved" in jql
        counts = _CASSJAVA_COUNTS.get(fix_version or "")
        if counts:
            fixture = counts[0] if unresolved else counts[1]
            return httpx.Response(200, json=_load_fixture(fixture))
        return httpx.Response(200, json={"startAt": 0, "maxResults": 0, "total": 0, "issues": []})
    raise AssertionError(f"unexpected JIRA request: {url_str}")


def _github_handler(runs_payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/actions/runs" in url_str:
            return httpx.Response(200, json=runs_payload)
        raise AssertionError(f"unexpected GitHub request: {url_str}")

    return handler


def _combined_transport(github_runs_payload: dict) -> httpx.MockTransport:
    github_handler = _github_handler(github_runs_payload)

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "api.github.com" in url_str:
            return github_handler(request)
        if "issues.apache.org" in url_str:
            return _jira_handler(request)
        raise AssertionError(f"unexpected request: {url_str}")

    return httpx.MockTransport(handler)


def _offline_collector(config, transport: httpx.MockTransport) -> SubprojectReleasesCollector:
    return SubprojectReleasesCollector(
        config,
        transport=transport,
        github_token="test-token",
        max_retries=1,
        min_request_interval=0,
        sleep_fn=lambda s: None,
    )


@pytest.fixture
def java_driver_config():
    return load_project("projects/java-driver.yaml")


@pytest.fixture
def accord_config(tmp_path):
    import yaml

    config_path = tmp_path / "accord.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {"id": "accord", "display_name": "Accord"},
                "repos": [
                    {"owner": "apache", "name": "cassandra-accord", "default_branch": "trunk"}
                ],
                "issue_tracker": {
                    "type": "jira",
                    "base_url": "https://issues.apache.org/jira",
                    "project_key": "CASSANDRA",
                    "jql_filter": "component = Accord",
                },
            }
        )
    )
    return load_project(config_path)


@pytest.fixture
def easy_stress_config(tmp_path):
    import yaml

    config_path = tmp_path / "easy-stress.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {"id": "easy-stress", "display_name": "easy-stress"},
                "repos": [
                    {"owner": "apache", "name": "cassandra-easy-stress", "default_branch": "main"}
                ],
            }
        )
    )
    return load_project(config_path)


class TestUnreleasedVersions:
    def test_only_unreleased_ticketed_versions_are_returned(self, java_driver_config):
        transport = _combined_transport(JAVA_DRIVER_RUNS)
        collector = _offline_collector(java_driver_config, transport)
        versions = collector.fetch_unreleased_versions()
        names = {v.name for v in versions}
        # CASSJAVA has 11 versions total, 4 unreleased (4.18.0, 3.13.0,
        # 5.0.0, 4.19.4) -- every one of those four has at least one
        # ticket filed against it (verified live), so all four appear.
        assert names == {"4.18.0", "3.13.0", "5.0.0", "4.19.4"}

    def test_open_and_total_counts_are_real_live_values(self, java_driver_config):
        transport = _combined_transport(JAVA_DRIVER_RUNS)
        collector = _offline_collector(java_driver_config, transport)
        by_name = {v.name: v for v in collector.fetch_unreleased_versions()}
        assert by_name["5.0.0"].open_count == 5
        assert by_name["5.0.0"].total_count == 10
        assert by_name["4.18.0"].open_count == 0
        assert by_name["4.18.0"].total_count == 1

    def test_issues_url_links_to_jira_search_for_this_version(self, java_driver_config):
        transport = _combined_transport(JAVA_DRIVER_RUNS)
        collector = _offline_collector(java_driver_config, transport)
        by_name = {v.name: v for v in collector.fetch_unreleased_versions()}
        url = by_name["5.0.0"].issues_url
        assert url.startswith("https://issues.apache.org/jira/issues/?jql=")
        assert urllib.parse.unquote(url.split("jql=")[1]) == (
            'project=CASSJAVA AND fixVersion="5.0.0"'
        )

    def test_zero_ticket_version_is_excluded(self):
        """Every live CASSJAVA unreleased version happens to have >=1
        ticket, so this exercises the exclusion rule directly against a
        synthetic all-zero search response instead."""
        versions_payload = [
            {"id": "1", "name": "9.9.9", "archived": False, "released": False},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/versions" in url_str:
                return httpx.Response(200, json=versions_payload)
            if "/search" in url_str:
                return httpx.Response(
                    200, json={"startAt": 0, "maxResults": 0, "total": 0, "issues": []}
                )
            raise AssertionError(url_str)

        config = load_project("projects/java-driver.yaml")
        collector = _offline_collector(config, httpx.MockTransport(handler))
        assert collector.fetch_unreleased_versions() == []

    def test_archived_version_is_excluded(self):
        """CASSANALYTICS's own "1.0" (archived=True, 42 live tickets) is
        exactly this shape -- synthesized here since it's project-specific,
        not CASSJAVA's own data."""
        versions_payload = [
            {"id": "1", "name": "1.0", "archived": True, "released": False},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/versions" in url_str:
                return httpx.Response(200, json=versions_payload)
            if "/search" in url_str:
                return httpx.Response(
                    200, json={"startAt": 0, "maxResults": 0, "total": 42, "issues": []}
                )
            raise AssertionError(url_str)

        config = load_project("projects/java-driver.yaml")
        collector = _offline_collector(config, httpx.MockTransport(handler))
        assert collector.fetch_unreleased_versions() == []

    def test_released_version_is_excluded(self):
        versions_payload = [
            {
                "id": "1",
                "name": "4.17.0",
                "archived": False,
                "released": True,
                "releaseDate": "2024-01-01",
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/versions" in str(request.url):
                return httpx.Response(200, json=versions_payload)
            raise AssertionError(f"unexpected request: {request.url}")

        config = load_project("projects/java-driver.yaml")
        collector = _offline_collector(config, httpx.MockTransport(handler))
        assert collector.fetch_unreleased_versions() == []

    def test_no_issue_tracker_returns_empty_without_any_jira_call(self, easy_stress_config):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected request (no JIRA configured): {request.url}")

        collector = _offline_collector(easy_stress_config, httpx.MockTransport(handler))
        assert collector.fetch_unreleased_versions() == []


class TestJqlFilterAnding:
    """issue #175 (same discipline as issue #181 for `release_lines.py`):
    accord shares CASSANDRA's JIRA project -- every fixVersion search must
    AND in `issue_tracker.jql_filter`, never span the whole shared
    project's backlog."""

    def test_filter_is_anded_into_version_count_queries(self, accord_config):
        seen_jql: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/project/CASSANDRA/versions" in url_str:
                return httpx.Response(
                    200,
                    json=[
                        {"id": "1", "name": "6.0", "archived": False, "released": False},
                    ],
                )
            if "/search" in url_str:
                seen_jql.append(request.url.params.get("jql", ""))
                return httpx.Response(
                    200, json={"startAt": 0, "maxResults": 0, "total": 1, "issues": []}
                )
            raise AssertionError(url_str)

        collector = _offline_collector(accord_config, httpx.MockTransport(handler))
        collector.fetch_unreleased_versions()
        assert seen_jql, "expected at least one JIRA search call"
        for jql in seen_jql:
            assert "component = Accord" in jql


class TestWorkflowRuns:
    def test_latest_run_per_real_workflow_is_returned(self, java_driver_config):
        transport = _combined_transport(GOCQL_RUNS)
        collector = _offline_collector(java_driver_config, transport)
        runs = collector.fetch_latest_workflow_runs("apache", "cassandra-gocql-driver", "trunk")
        assert len(runs) == 1
        run = runs[0]
        assert run.workflow_name == "Build"
        assert run.workflow_path == ".github/workflows/main.yml"
        assert run.conclusion == "success"
        assert run.run_number == 1310

    def test_dynamic_pseudo_workflows_are_excluded(self, java_driver_config):
        """java-driver's own default-branch runs are ALL GitHub's
        auto-generated `dynamic/...`-path pseudo-workflows (Copilot code
        review) -- verified live 2026-10-12 -- so none should surface as
        this project's own CI."""
        transport = _combined_transport(JAVA_DRIVER_RUNS)
        collector = _offline_collector(java_driver_config, transport)
        runs = collector.fetch_latest_workflow_runs("apache", "cassandra-java-driver", "trunk")
        assert runs == []

    def test_no_workflows_at_all_returns_empty(self, java_driver_config):
        transport = _combined_transport(ACCORD_RUNS)
        collector = _offline_collector(java_driver_config, transport)
        runs = collector.fetch_latest_workflow_runs("apache", "cassandra-accord", "trunk")
        assert runs == []


class TestCollect:
    def test_collect_builds_both_tables_independently(self, java_driver_config):
        transport = _combined_transport(JAVA_DRIVER_RUNS)
        collector = _offline_collector(java_driver_config, transport)
        collector._config.repos  # sanity: java-driver config has repos
        result = collector.collect(snapshot_id="test-snap")
        assert result.jira_ok is True
        assert result.actions_ok is True
        assert result.jira_version.num_rows == 4
        # java-driver's own runs are all excluded (dynamic pseudo-workflows).
        assert result.ci_workflow_run.num_rows == 0
        assert result.errors == {}

    def test_collect_degrades_jira_independently_of_actions(self, java_driver_config):
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "issues.apache.org" in url_str:
                return httpx.Response(500, json={"error": "boom"})
            if "api.github.com" in url_str:
                return httpx.Response(200, json=GOCQL_RUNS)
            raise AssertionError(url_str)

        collector = SubprojectReleasesCollector(
            java_driver_config,
            transport=httpx.MockTransport(handler),
            github_token="test-token",
            max_retries=1,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )
        result = collector.collect(snapshot_id="test-snap")
        assert result.jira_ok is False
        assert result.actions_ok is True
        assert "jira" in result.errors
        assert result.ci_workflow_run.num_rows == 1
