"""Tests for project_health.collectors.security (issue #55).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from **real, live-fetched fixtures**
(`tests/fixtures/security/scorecard_cassandra.json`,
`.../nvd_cassandra.json` — recorded 2026-09-25 from
`api.securityscorecards.dev` and `services.nvd.nist.gov` respectively, see
`docs/spec/GOVERNANCE.md` §10). Retries use a no-op `sleep_fn` so tests run
instantly.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from project_health.collectors.security import (
    CollectionError,
    SecurityCollector,
    _summarize_versions,
    _version_sort_key,
)
from project_health.config import load_project

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "security"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


SCORECARD_RESPONSE = _load_fixture("scorecard_cassandra.json")
NVD_RESPONSE = _load_fixture("nvd_cassandra.json")


@pytest.fixture
def config():
    return load_project("projects/cassandra.yaml")


def _smart_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "securityscorecards.dev" in url_str:
            return httpx.Response(200, json=SCORECARD_RESPONSE)
        if "nvd.nist.gov" in url_str:
            return httpx.Response(200, json=NVD_RESPONSE)
        raise AssertionError(f"unexpected request: {url_str}")

    return httpx.MockTransport(handler)


def _offline_collector(config, transport: httpx.MockTransport) -> SecurityCollector:
    return SecurityCollector(
        config,
        transport=transport,
        max_retries=1,
        sleep_fn=lambda s: None,
    )


class TestConfigWiring:
    def test_urls_come_from_project_config(self, config):
        """projects/cassandra.yaml's `security:` block is read, not hardcoded."""
        collector = _offline_collector(config, _smart_transport())
        assert collector._scorecard_url == (
            "https://api.securityscorecards.dev/projects/github.com/apache/cassandra"
        )
        assert collector._nvd_url == "https://services.nvd.nist.gov/rest/json/cves/2.0"
        assert collector._cpe_match_string == "cpe:2.3:a:apache:cassandra:*:*:*:*:*:*:*:*"


class TestScorecardCollection:
    def test_collect_returns_one_row_per_check(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()

        # Real fixture has 14 checks (verified live 2026-09-25 -- see
        # docs/spec/GOVERNANCE.md §11.1).
        assert result.scorecard_check_count == 14
        assert result.scorecard_checks.num_rows == 14

    def test_code_review_check_row_matches_live_evidence(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()

        rows = result.scorecard_checks.to_pylist()
        by_name = {row["check_name"]: row for row in rows}

        code_review = by_name["Code-Review"]
        assert code_review["check_score"] == 0
        assert "0/30 approved changesets" in code_review["check_reason"]
        assert code_review["overall_score"] == 4.6
        assert code_review["repo"] == "github.com/apache/cassandra"
        assert code_review["scorecard_version"].startswith("v5.5.1")
        assert code_review["scorecard_date"] == date(2026, 9, 21)

    def test_not_applicable_checks_keep_negative_one_score(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()

        rows = result.scorecard_checks.to_pylist()
        by_name = {row["check_name"]: row for row in rows}
        assert by_name["Signed-Releases"]["check_score"] == -1
        assert by_name["Packaging"]["check_score"] == -1

    def test_details_list_joined_into_summary(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()

        rows = result.scorecard_checks.to_pylist()
        by_name = {row["check_name"]: row for row in rows}
        branch_protection = by_name["Branch-Protection"]
        assert branch_protection["check_details_summary"] is not None
        assert "force pushes" in branch_protection["check_details_summary"]

    def test_every_row_shares_one_snapshot_id(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()
        snapshot_ids = {row["source_snapshot_id"] for row in result.scorecard_checks.to_pylist()}
        assert len(snapshot_ids) == 1


class TestAdvisoryCollection:
    def test_collect_returns_one_row_per_cve(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()

        # Real fixture has 16 CVEs against apache:cassandra's CPE (verified
        # live 2026-09-25, docs/spec/GOVERNANCE.md §11.2).
        assert result.advisory_count == 16
        assert result.advisories.num_rows == 16

    def test_recent_cve_has_cvss_and_version_range(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()

        rows = result.advisories.to_pylist()
        by_id = {row["cve_id"]: row for row in rows}
        cve = by_id["CVE-2025-26467"]

        assert cve["cvss_score"] == 8.8
        assert cve["cvss_version"] == "3.1"
        assert cve["severity"] == "HIGH"
        assert cve["source"] == "nvd"
        assert cve["advisory_url"] == "https://nvd.nist.gov/vuln/detail/CVE-2025-26467"
        assert "3.0.0" in cve["affected_versions"]
        assert "3.0.31" in cve["fixed_versions"]

    def test_old_enumerated_cve_summarizes_a_version_span(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()

        rows = result.advisories.to_pylist()
        by_id = {row["cve_id"]: row for row in rows}
        cve = by_id["CVE-2015-0225"]

        # Enumerated-CPE style (no versionStart/End fields) -- summarized as
        # a span, not left blank.
        assert cve["affected_versions"] is not None
        assert cve["published_date"] == date(2015, 4, 3)

    def test_mixed_product_cve_only_summarizes_cassandra_lines(self, config):
        """CVE-2016-3427 also lists Oracle JDK/JRE CPEs; only the
        apache:cassandra ranges should appear in affected_versions."""
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()

        rows = result.advisories.to_pylist()
        by_id = {row["cve_id"]: row for row in rows}
        cve = by_id["CVE-2016-3427"]
        assert "oracle" not in (cve["affected_versions"] or "").lower()
        assert "2.1.0" in cve["affected_versions"]

    def test_every_row_shares_one_snapshot_id_with_scorecard(self, config):
        collector = _offline_collector(config, _smart_transport())
        result = collector.collect()
        scorecard_ids = {r["source_snapshot_id"] for r in result.scorecard_checks.to_pylist()}
        advisory_ids = {r["source_snapshot_id"] for r in result.advisories.to_pylist()}
        assert scorecard_ids == advisory_ids


class TestVersionSummaryHelpers:
    def test_version_sort_key_orders_numerically_not_lexically(self):
        versions = ["1.2.19", "1.2.2", "1.2.9"]
        assert sorted(versions, key=_version_sort_key) == ["1.2.2", "1.2.9", "1.2.19"]

    def test_summarize_versions_range_style(self):
        configurations = [
            {
                "nodes": [
                    {
                        "cpeMatch": [
                            {
                                "criteria": "cpe:2.3:a:apache:cassandra:*:*:*:*:*:*:*:*",
                                "versionStartIncluding": "5.0.0",
                                "versionEndExcluding": "5.0.4",
                            }
                        ]
                    }
                ]
            }
        ]
        affected, fixed = _summarize_versions(configurations)
        assert affected == "5.0.0–5.0.4"
        assert fixed == "5.0.4"

    def test_summarize_versions_ignores_non_cassandra_cpes(self):
        configurations = [
            {
                "nodes": [
                    {
                        "cpeMatch": [
                            {"criteria": "cpe:2.3:a:oracle:jdk:1.8.0:update77:*:*:*:*:*:*"},
                        ]
                    }
                ]
            }
        ]
        affected, fixed = _summarize_versions(configurations)
        assert affected is None
        assert fixed is None


class TestRetryAndErrors:
    def test_5xx_exhausts_retries_and_raises(self, config):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        collector = SecurityCollector(
            config,
            transport=httpx.MockTransport(handler),
            max_retries=2,
            sleep_fn=lambda s: None,
        )
        with pytest.raises(CollectionError):
            collector.collect()

    def test_scorecard_failure_prevents_advisory_fetch(self, config):
        """A failed Scorecard fetch must not silently fall through to a
        partial (advisories-only) result -- the whole source fails."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(503, text="Service Unavailable")

        collector = SecurityCollector(
            config,
            transport=httpx.MockTransport(handler),
            max_retries=1,
            sleep_fn=lambda s: None,
        )
        with pytest.raises(CollectionError):
            collector.collect()
        assert all("securityscorecards.dev" in url for url in calls)
