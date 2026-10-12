"""Tests for project_health.collectors.release_lines (issue #166).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from **real, live-fetched fixtures**
(`tests/fixtures/release_lines/*.json` -- recorded 2026-10-12 from
`issues.apache.org/jira`, `butler.cassandra.apache.org`, and
`ci-cassandra.apache.org`, see issue #166's own verified-sources comment).
Retries use a no-op `sleep_fn` so tests run instantly, same offline pattern
as `test_security_collector.py`/`test_release_collector.py`.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

from project_health.collectors.release_lines import (
    CollectionError,
    JiraVersionInfo,
    ReleaseLinesCollector,
    classify_versions,
    order_ladder,
)
from project_health.config import load_project

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "release_lines"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


JIRA_VERSIONS_RESPONSE = _load_fixture("jira_versions.json")
BUTLER_RESPONSE = _load_fixture("butler_trends.json")
JENKINS_RESPONSE = _load_fixture("jenkins_jobs.json")

# fixVersion -> fixture file, for the issues this test cares about having
# real counts for; every other tracked version in the fixture run returns
# an empty page (still real behavior: those lines/steps have 0 open issues
# in several cases, verified live -- see test_release_lines_model.py and
# the issue's own verified-sources snapshot).
_FIXVERSION_FIXTURES = {
    "6.0-alpha": "jira_fixver_6_0_alpha.json",
    "6.0": "jira_fixver_6_0.json",
    "6.0.x": "jira_fixver_6_0_x.json",
}


@pytest.fixture
def config():
    return load_project("projects/cassandra.yaml")


def _extract_fix_version(url: httpx.URL) -> str | None:
    jql = url.params.get("jql", "")
    # jql looks like: project=CASSANDRA AND fixVersion="6.0-alpha" AND ...
    marker = 'fixVersion="'
    start = jql.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = jql.find('"', start)
    return urllib.parse.unquote(jql[start:end])


def _smart_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "butler.cassandra.apache.org" in url_str:
            return httpx.Response(200, json=BUTLER_RESPONSE)
        if "ci-cassandra.apache.org" in url_str:
            return httpx.Response(200, json=JENKINS_RESPONSE)
        if "/project/CASSANDRA/versions" in url_str:
            return httpx.Response(200, json=JIRA_VERSIONS_RESPONSE)
        if "/rest/api/2/search" in url_str:
            fix_version = _extract_fix_version(request.url)
            fixture_name = _FIXVERSION_FIXTURES.get(fix_version or "")
            if fixture_name:
                return httpx.Response(200, json=_load_fixture(fixture_name))
            return httpx.Response(200, json={"issues": [], "total": 0})
        raise AssertionError(f"unexpected request: {url_str}")

    return httpx.MockTransport(handler)


def _offline_collector(config, transport: httpx.MockTransport) -> ReleaseLinesCollector:
    return ReleaseLinesCollector(
        config,
        transport=transport,
        max_retries=1,
        min_request_interval=0,
        sleep_fn=lambda s: None,
    )


class TestConfigWiring:
    def test_urls_and_lines_come_from_project_config(self, config):
        collector = _offline_collector(config, _smart_transport())
        assert collector._butler_url == "https://butler.cassandra.apache.org/api/upstream/trends"
        assert collector._jenkins_url == "https://ci-cassandra.apache.org/api/json"
        assert collector._active_lines == ["6.0", "5.0", "4.1", "4.0", "3.11", "3.0"]


class TestClassifyVersions:
    def test_6_0_line_real_data(self):
        """Live 6.0 line classification (verified 2026-10-12, issue #166's
        own verified-sources comment): three ladder placeholders, one
        series backlog, the bare GA target, one next (pre-release) version.
        """
        versions = [
            JiraVersionInfo(
                v["id"],
                v["name"],
                v.get("description"),
                v.get("released", False),
                v.get("releaseDate"),
            )
            for v in JIRA_VERSIONS_RESPONSE
        ]
        result = classify_versions(versions, "6.0")
        assert {v.name for v in result["ladder_placeholders"]} == {
            "6.0-alpha",
            "6.0-beta",
            "6.0-rc",
        }
        assert result["series_placeholder"].name == "6.0.x"
        assert result["ga_target"].name == "6.0"
        assert [v.name for v in result["next_versions"]] == ["6.0-alpha3"]

    def test_series_placeholder_detected_without_a_description(self):
        """"6.0.x" currently carries no `description` at all (verified
        live, 2026-10-12) -- detection must still work from the `<line>.x`
        name shape, not require the description text."""
        versions = [JiraVersionInfo("1", "6.0.x", None, False, None)]
        result = classify_versions(versions, "6.0")
        assert result["series_placeholder"] is not None
        assert result["series_placeholder"].name == "6.0.x"

    def test_malformed_trailing_dot_version_is_not_tracked(self):
        """"6.0." (a real, live, malformed JIRA version with no digits
        after the line prefix) must not be misclassified as a next-version
        candidate or a GA target."""
        versions = [JiraVersionInfo("1", "6.0.", None, False, None)]
        result = classify_versions(versions, "6.0")
        assert result["series_placeholder"] is None
        assert result["ga_target"] is None
        assert result["next_versions"] == []

    def test_bare_ga_target_requires_exact_name_match(self):
        versions = [JiraVersionInfo("1", "6.0", None, False, None)]
        result = classify_versions(versions, "6.0")
        assert result["ga_target"].name == "6.0"

    def test_other_lines_never_leak_in(self):
        versions = [
            JiraVersionInfo("1", "6.0-alpha", "Placeholder for blockers to 6.0-beta1", False, None),
            JiraVersionInfo("2", "5.0-alpha", "Placeholder for blockers to 5.0-beta1", False, None),
        ]
        result = classify_versions(versions, "6.0")
        assert [v.name for v in result["ladder_placeholders"]] == ["6.0-alpha"]


class TestOrderLadder:
    def test_chain_order_derived_from_descriptions_not_names(self):
        """Feed the placeholders in reverse/alphabetically-wrong order --
        the function must still recover alpha -> beta -> rc purely from
        each one's own description text, never from a hard-coded name
        list."""
        placeholders = [
            JiraVersionInfo("3", "6.0-rc", "Placeholder for blockers to 6.0.0 (GA)", False, None),
            JiraVersionInfo("1", "6.0-alpha", "Placeholder for blockers to 6.0-beta1", False, None),
            JiraVersionInfo("2", "6.0-beta", "Placeholder for blockers to 6.0-rc1", False, None),
        ]
        ordered = order_ladder(placeholders)
        assert [s.version.name for s in ordered] == ["6.0-alpha", "6.0-beta", "6.0-rc"]
        assert [s.stage for s in ordered] == ["alpha", "beta", "rc"]

    def test_gap_in_chain_does_not_crash_or_drop_steps(self):
        """Real live data: the 4.0 line has alpha and rc placeholders but
        no beta (verified 2026-10-12) -- every placeholder must still
        appear in the result."""
        placeholders = [
            JiraVersionInfo("1", "4.0-alpha", "Placeholder for blockers to 4.0-beta1", False, None),
            JiraVersionInfo("2", "4.0-rc", "Placeholder for blockers to 4.0.0 (GA)", False, None),
        ]
        ordered = order_ladder(placeholders)
        assert {s.version.name for s in ordered} == {"4.0-alpha", "4.0-rc"}

    def test_empty_ladder_returns_empty(self):
        assert order_ladder([]) == []

    def test_future_major_line_works_without_code_changes(self):
        """A hypothetical 7.0 ladder, never seen by this code before,
        orders correctly purely from its own descriptions -- demonstrating
        "derive the ladder from descriptions, not hard-coded names"."""
        placeholders = [
            JiraVersionInfo("1", "7.0-alpha", "Placeholder for blockers to 7.0-beta1", False, None),
            JiraVersionInfo("2", "7.0-beta", "Placeholder for blockers to 7.0-rc1", False, None),
            JiraVersionInfo("3", "7.0-rc", "Placeholder for blockers to 7.0.0 (GA)", False, None),
        ]
        ordered = order_ladder(placeholders)
        assert [s.version.name for s in ordered] == ["7.0-alpha", "7.0-beta", "7.0-rc"]


class TestCollectRealFixtures:
    def test_6_0_line_counts_match_live_snapshot(self, config):
        """Cross-checked live, 2026-10-12 (issue #166's verified-sources
        comment): 6.0-alpha has 2 open issues, 6.0 (bare GA target) has 3,
        6.0.x has 39 (24 of them Patch Available)."""
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect(snapshot_id="test")
        assert result.jira_ok is True

        rows = result.release_blocker_issue.to_pylist()
        by_version: dict[str, list[dict]] = {}
        for row in rows:
            by_version.setdefault(row["version_name"], []).append(row)

        assert len(by_version.get("6.0-alpha", [])) == 2
        assert len(by_version.get("6.0", [])) == 3
        six_x = by_version.get("6.0.x", [])
        assert len(six_x) == 39
        assert sum(1 for r in six_x if r["status"] == "Patch Available") == 24

    def test_6_0_x_quotes_live_jira_keys(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect(snapshot_id="test")
        rows = [
            r for r in result.release_blocker_issue.to_pylist() if r["version_name"] == "6.0-alpha"
        ]
        keys = {r["issue_key"] for r in rows}
        assert keys == {"CASSANDRA-11745", "CASSANDRA-21340"}

    def test_butler_and_jenkins_collected(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect(snapshot_id="test")
        assert result.butler_ok is True
        assert result.jenkins_ok is True
        job_names = {r["job_name"] for r in result.ci_job_status.to_pylist()}
        assert job_names == {
            "Cassandra-3.0",
            "Cassandra-3.11",
            "Cassandra-4.0",
            "Cassandra-4.1",
            "Cassandra-5.0",
            "Cassandra-6.0",
            "Cassandra-trunk",
        }
        trend_workflows = {r["workflow"] for r in result.ci_trend_point.to_pylist()}
        assert "Cassandra-6.0" in trend_workflows


class TestIndependentDegradation:
    """Issue #166: each of the three sources degrades independently; one
    failing never raises out of `collect()` and never blocks the other
    two."""

    def test_jira_outage_does_not_block_butler_or_jenkins(self, config):
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "butler.cassandra.apache.org" in url_str:
                return httpx.Response(200, json=BUTLER_RESPONSE)
            if "ci-cassandra.apache.org" in url_str:
                return httpx.Response(200, json=JENKINS_RESPONSE)
            return httpx.Response(503)

        collector = _offline_collector(config, httpx.MockTransport(handler))
        result = collector.collect(snapshot_id="test")

        assert result.jira_ok is False
        assert "jira" in result.errors
        assert result.jira_version.num_rows == 0
        assert result.release_blocker_issue.num_rows == 0

        assert result.butler_ok is True
        assert result.jenkins_ok is True
        assert result.ci_job_status.num_rows == 7
        assert result.ci_trend_point.num_rows > 0

    def test_butler_outage_does_not_block_jira_or_jenkins(self, config):
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "butler.cassandra.apache.org" in url_str:
                return httpx.Response(503)
            if "ci-cassandra.apache.org" in url_str:
                return httpx.Response(200, json=JENKINS_RESPONSE)
            if "/project/CASSANDRA/versions" in url_str:
                return httpx.Response(200, json=JIRA_VERSIONS_RESPONSE)
            if "/rest/api/2/search" in url_str:
                fix_version = _extract_fix_version(request.url)
                fixture_name = _FIXVERSION_FIXTURES.get(fix_version or "")
                if fixture_name:
                    return httpx.Response(200, json=_load_fixture(fixture_name))
                return httpx.Response(200, json={"issues": [], "total": 0})
            raise AssertionError(url_str)

        collector = _offline_collector(config, httpx.MockTransport(handler))
        result = collector.collect(snapshot_id="test")

        assert result.butler_ok is False
        assert "butler" in result.errors
        assert result.ci_trend_point.num_rows == 0

        assert result.jira_ok is True
        assert result.jenkins_ok is True
        assert result.jira_version.num_rows > 0
        assert result.ci_job_status.num_rows == 7

    def test_jenkins_outage_does_not_block_jira_or_butler(self, config):
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "butler.cassandra.apache.org" in url_str:
                return httpx.Response(200, json=BUTLER_RESPONSE)
            if "ci-cassandra.apache.org" in url_str:
                return httpx.Response(503)
            if "/project/CASSANDRA/versions" in url_str:
                return httpx.Response(200, json=JIRA_VERSIONS_RESPONSE)
            if "/rest/api/2/search" in url_str:
                fix_version = _extract_fix_version(request.url)
                fixture_name = _FIXVERSION_FIXTURES.get(fix_version or "")
                if fixture_name:
                    return httpx.Response(200, json=_load_fixture(fixture_name))
                return httpx.Response(200, json={"issues": [], "total": 0})
            raise AssertionError(url_str)

        collector = _offline_collector(config, httpx.MockTransport(handler))
        result = collector.collect(snapshot_id="test")

        assert result.jenkins_ok is False
        assert "jenkins" in result.errors
        assert result.ci_job_status.num_rows == 0

        assert result.jira_ok is True
        assert result.butler_ok is True
        assert result.ci_trend_point.num_rows > 0

    def test_all_three_failing_never_raises(self, config):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        collector = _offline_collector(config, httpx.MockTransport(handler))
        result = collector.collect(snapshot_id="test")
        assert result.jira_ok is False
        assert result.butler_ok is False
        assert result.jenkins_ok is False
        assert result.jira_version.num_rows == 0
        assert result.ci_job_status.num_rows == 0
        assert result.ci_trend_point.num_rows == 0


class TestRetryBehavior:
    def test_retries_then_raises_collection_error_surfaces_as_jira_failure(self, config):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "butler" in url_str or "ci-cassandra" in url_str:
                body = {"data": []} if "butler" in url_str else {"jobs": []}
                return httpx.Response(200, json=body)
            calls["n"] += 1
            return httpx.Response(500)

        collector = ReleaseLinesCollector(
            config,
            transport=httpx.MockTransport(handler),
            max_retries=2,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )
        result = collector.collect(snapshot_id="test")
        assert result.jira_ok is False
        assert calls["n"] >= 2

    def test_get_with_retry_raises_collection_error_type(self, config):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        collector = ReleaseLinesCollector(
            config,
            transport=httpx.MockTransport(handler),
            max_retries=1,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )
        with pytest.raises(CollectionError):
            collector.fetch_jira_versions()
