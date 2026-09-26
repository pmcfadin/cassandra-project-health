"""Tests for project_health.collectors.asf_roster (issue #50).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from test fixtures (synthetic, no real personal
data). Retries and timeout behavior use a no-op `sleep_fn` so tests run
instantly without real delays.

Per issue #50 requirements:
- Tests use offline fixtures (no live Whimsy access)
- Personal data replaced with synthetic IDs
- Join dates recorded where available, null otherwise
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from project_health.collectors.asf_roster import AsfRosterCollector
from project_health.config import ProjectConfig, load_project

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "asf_roster"


def _load_fixture(name: str) -> dict:
    """Load a JSON fixture file."""
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture
def config() -> ProjectConfig:
    return load_project("projects/cassandra.yaml")


def _offline_collector(
    config: ProjectConfig, transport: httpx.MockTransport
) -> AsfRosterCollector:
    """A `AsfRosterCollector` wired for offline tests: no retries needed,
    instant execution."""
    return AsfRosterCollector(
        config,
        transport=transport,
        max_retries=1,
        sleep_fn=lambda s: None,
    )


class TestRosterCollection:
    def test_collect_pmc_and_committers_from_both_sources(self, config):
        """Verify that collector fetches both PMC and committers from both sources."""
        committee_fixture = _load_fixture("committee_info_cassandra.json")
        ldap_fixture = _load_fixture("public_ldap_projects_cassandra.json")

        def handler(request: httpx.Request) -> httpx.Response:
            # The collector makes two requests: committee_info first, then public_ldap_projects
            # We can distinguish by checking the request or just return the right fixture each time
            # For simplicity with MockTransport, we'll return committee_info for the first request
            # and ldap for the second. This requires us to track state.
            return httpx.Response(200, json=committee_fixture)

        # Actually, we need to handle both URLs differently. Let me use a smarter handler.
        def smart_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "committee-info" in url_str or "committees" in request.url.path:
                return httpx.Response(200, json=committee_fixture)
            elif "public_ldap_projects" in url_str or "projects" in request.url.path:
                return httpx.Response(200, json=ldap_fixture)
            else:
                # Fallback: try to guess based on response content
                return httpx.Response(200, json=ldap_fixture)

        transport = httpx.MockTransport(smart_handler)
        collector = _offline_collector(config, transport)

        result = collector.collect()

        # Fixture has 49 PMC entries + 52 committers (101 total members - 49 PMC)
        # = 101 total roster entries
        assert result.entry_count == 101
        assert result.roster_entries.num_rows == 101

        # Check schema columns exist
        columns = set(result.roster_entries.column_names)
        expected = {
            "entry_id",
            "identity_id",
            "asf_id",
            "display_name",
            "role",
            "project",
            "effective_from",
            "effective_from_raw",
            "source_snapshot_id",
        }
        assert expected <= columns

    def test_pmc_entries_have_join_dates_committers_do_not(self, config):
        """Verify that PMC entries have join dates but committers don't."""
        from datetime import date

        committee_fixture = _load_fixture("committee_info_cassandra.json")
        ldap_fixture = _load_fixture("public_ldap_projects_cassandra.json")

        def smart_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "committee-info" in url_str or "committees" in request.url.path:
                return httpx.Response(200, json=committee_fixture)
            else:
                return httpx.Response(200, json=ldap_fixture)

        transport = httpx.MockTransport(smart_handler)
        collector = _offline_collector(config, transport)

        result = collector.collect()
        table = result.roster_entries

        # Get role, effective_from, and asf_id columns
        role = table.column("role").to_pylist()
        effective_from = table.column("effective_from").to_pylist()
        asf_id = table.column("asf_id").to_pylist()
        effective_from_raw = table.column("effective_from_raw").to_pylist()

        # Build a map for easier testing
        by_role = {"pmc": [], "committer": []}
        for i, r in enumerate(role):
            by_role[r].append({
                "asf_id": asf_id[i],
                "effective_from": effective_from[i],
                "effective_from_raw": effective_from_raw[i],
            })

        # PMC entries (from committee-info): 49 entries, all with join dates
        expected_pmc = 49
        assert len(by_role["pmc"]) == expected_pmc
        for entry in by_role["pmc"]:
            msg = f"PMC {entry['asf_id']} should have effective_from date"
            assert entry["effective_from"] is not None, msg
            msg_raw = f"PMC {entry['asf_id']} should have effective_from_raw"
            assert entry["effective_from_raw"] is not None, msg_raw

        # Committers (from public_ldap_projects, not in PMC): 52 entries with null dates
        expected_committers = 52
        assert len(by_role["committer"]) == expected_committers
        for entry in by_role["committer"]:
            msg = f"Committer {entry['asf_id']} should have null effective_from"
            assert entry["effective_from"] is None, msg
            msg_raw = f"Committer {entry['asf_id']} should have null effective_from_raw"
            assert entry["effective_from_raw"] is None, msg_raw

        # Verify date range for PMC
        pmc_dates = [e["effective_from"] for e in by_role["pmc"] if e["effective_from"] is not None]
        min_date = min(pmc_dates)
        max_date = max(pmc_dates)
        assert min_date < date(2020, 1, 1)  # Early date
        assert max_date > date(2025, 1, 1)  # Recent date

    def test_roles_are_pmc_or_committer(self, config):
        """Verify that all entries have either 'pmc' or 'committer' role."""
        committee_fixture = _load_fixture("committee_info_cassandra.json")
        ldap_fixture = _load_fixture("public_ldap_projects_cassandra.json")

        def smart_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "committee-info" in url_str or "committees" in request.url.path:
                return httpx.Response(200, json=committee_fixture)
            else:
                return httpx.Response(200, json=ldap_fixture)

        transport = httpx.MockTransport(smart_handler)
        collector = _offline_collector(config, transport)

        result = collector.collect()
        table = result.roster_entries

        roles = table.column("role").to_pylist()
        valid_roles = {"pmc", "committer"}
        role_set = set(roles)
        assert all(r in valid_roles for r in roles), (
            f"All roles should be 'pmc' or 'committer', got {role_set}"
        )
        # Both roles should be present
        assert "pmc" in role_set, "Should have at least one PMC entry"
        assert "committer" in role_set, "Should have at least one committer entry"

    def test_all_entries_have_cassandra_project(self, config):
        """Verify that all entries are tagged with project='cassandra'."""
        committee_fixture = _load_fixture("committee_info_cassandra.json")
        ldap_fixture = _load_fixture("public_ldap_projects_cassandra.json")

        def smart_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "committee-info" in url_str or "committees" in request.url.path:
                return httpx.Response(200, json=committee_fixture)
            else:
                return httpx.Response(200, json=ldap_fixture)

        transport = httpx.MockTransport(smart_handler)
        collector = _offline_collector(config, transport)

        result = collector.collect()
        table = result.roster_entries

        projects = table.column("project").to_pylist()
        assert all(p == "cassandra" for p in projects)

    def test_asf_ids_populated_from_both_sources(self, config):
        """Verify that ASF IDs are extracted from both committee-info and public_ldap_projects."""
        committee_fixture = _load_fixture("committee_info_cassandra.json")
        ldap_fixture = _load_fixture("public_ldap_projects_cassandra.json")

        def smart_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "committee-info" in url_str or "committees" in request.url.path:
                return httpx.Response(200, json=committee_fixture)
            else:
                return httpx.Response(200, json=ldap_fixture)

        transport = httpx.MockTransport(smart_handler)
        collector = _offline_collector(config, transport)

        result = collector.collect()
        table = result.roster_entries

        asf_ids = table.column("asf_id").to_pylist()
        # All should be non-empty strings (synthetic IDs in fixtures)
        assert all(isinstance(id, str) and len(id) > 0 for id in asf_ids)

        # Verify expected synthetic IDs from both fixtures
        asf_id_set = set(asf_ids)
        # PMC fixture should include user001-user049
        assert "user001" in asf_id_set
        assert "user049" in asf_id_set
        # Non-PMC committers should include user050-user101
        assert "user050" in asf_id_set
        assert "user101" in asf_id_set


class TestIdentityFields:
    def test_identity_id_initially_null(self, config):
        """Verify that identity_id is left null for identity resolution to fill."""
        fixture = _load_fixture("committee_info_cassandra.json")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=fixture)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)

        result = collector.collect()
        table = result.roster_entries

        identity_ids = table.column("identity_id").to_pylist()
        # Per issue #50 requirement: collectors leave identity_id null
        assert all(id is None for id in identity_ids)


class TestErrorHandling:
    def test_http_error_raises_collection_error(self, config):
        """Verify that HTTP errors are wrapped in CollectionError."""
        from project_health.collectors.asf_roster import CollectionError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)

        with pytest.raises(CollectionError, match="HTTP 500"):
            collector.collect()

    def test_missing_config_raises_value_error(self, config):
        """Verify that missing roster config raises ValueError."""
        # Create a config without roster (set it to None)
        config.roster = None
        with pytest.raises(ValueError, match="committee_info_url"):
            AsfRosterCollector(config)


class TestFixtureStructure:
    def test_fixture_member_entries_are_strings(self):
        """Verify that fixture's members/owners entries are strings, not dicts.

        Per issue #50 fixup cycle 3: the real Whimsy API returns members and owners
        as lists of ID strings, not objects. This test asserts the fixture matches
        the real structure.
        """
        ldap_fixture = _load_fixture("public_ldap_projects_cassandra.json")

        cassandra = ldap_fixture.get("projects", {}).get("cassandra", {})
        members = cassandra.get("members", [])
        owners = cassandra.get("owners", [])

        # All members should be strings
        assert len(members) > 0, "Fixture should have members"
        for member in members:
            assert isinstance(member, str), (
                f"Member entry should be string, got {type(member).__name__}: {member}"
            )

        # All owners should be strings
        assert len(owners) > 0, "Fixture should have owners"
        for owner in owners:
            assert isinstance(owner, str), (
                f"Owner entry should be string, got {type(owner).__name__}: {owner}"
            )
