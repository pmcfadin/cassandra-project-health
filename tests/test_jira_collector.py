"""Tests for project_health.collectors.jira (issue #5).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from `tests/fixtures/jira/*.json` (issue #3), and
retry/backoff tests inject a no-op `sleep_fn` so a 5x-retry test doesn't
actually sleep. Nothing here touches the network (per issue #5's "Tests
never hit the network").
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from project_health.collectors.jira import (
    CollectionError,
    JiraCollector,
    build_jql,
)
from project_health.config import ProjectConfig, load_project

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "jira"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


PAGE_1 = _load_fixture("search_with_reviewers.json")
PAGE_2 = _load_fixture("search_with_reviewers_page2.json")
UNRESOLVED = _load_fixture("search_unresolved.json")
EMPTY_PAGE = {
    "expand": "schema,names",
    "startAt": 10,
    "maxResults": 5,
    "total": 11125,
    "issues": [],
}


@pytest.fixture
def config() -> ProjectConfig:
    return load_project("projects/cassandra.yaml")


def _offline_collector(
    config: ProjectConfig, transport: httpx.MockTransport, page_size: int = 5
) -> JiraCollector:
    """A `JiraCollector` wired for fast, sleep-free offline tests: zero
    rate-limit interval and a no-op `sleep_fn` so retry/backoff tests don't
    actually wait."""
    return JiraCollector(
        config,
        transport=transport,
        page_size=page_size,
        min_request_interval=0,
        sleep_fn=lambda s: None,
    )


def _paginated_transport(pages: dict[int, dict]) -> httpx.MockTransport:
    """Build a MockTransport that serves `pages[startAt]` based on the
    request's `startAt` query param."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        start_at = int(query.get("startAt", ["0"])[0])
        if start_at not in pages:
            raise AssertionError(f"unexpected startAt={start_at}")
        return httpx.Response(200, json=pages[start_at])

    return httpx.MockTransport(handler)


class TestPaginationAndNormalization:
    def test_pagination_across_both_pages_yields_expected_issue_count(self, config):
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})
        collector = _offline_collector(config, transport)

        result = collector.collect()

        assert result.issue_count == 10
        assert result.issues.num_rows == 10
        expected_keys = {issue["key"] for issue in PAGE_1["issues"] + PAGE_2["issues"]}
        assert set(result.issues.column("issue_key").to_pylist()) == expected_keys

    def test_reviewer_rows_emitted_for_each_populated_field(self, config):
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})
        collector = _offline_collector(config, transport)

        result = collector.collect()

        # CASSANDRA-21712 has exactly one reviewer (maedhroz) in
        # customfield_12313420, customfield_10022 is null for every fixture
        # issue.
        reviewers = result.review_events.to_pylist()
        assert result.review_event_count == len(reviewers)
        assert result.review_event_count > 0

        by_issue: dict[str, list[dict]] = {}
        for row in reviewers:
            by_issue.setdefault(row["issue_key"], []).append(row)

        assert {r["reviewer_raw_value"] for r in by_issue["CASSANDRA-21712"]} == {"maedhroz"}
        assert {r["reviewer_raw_value"] for r in by_issue["CASSANDRA-21671"]} == {
            "maedhroz",
            "frankgh",
            "samueldlightfoot",
        }
        for row in reviewers:
            assert row["source"] == "jira_field"
            assert row["reviewer_raw_type"] == "jira_username"
            assert row["reviewer_identity_id"] is None

    def test_issue_with_no_populated_reviewer_field_emits_zero_review_rows(self, config):
        # search_unresolved.json (issue #3 fixture) mixes issues with and
        # without customfield_12313420 populated — CASSANDRA-21717 has
        # neither field populated.
        no_reviewer_issue = next(
            i for i in UNRESOLVED["issues"] if not i["fields"].get("customfield_12313420")
        )
        page = {
            "expand": "schema,names",
            "startAt": 0,
            "maxResults": 5,
            "total": 1,
            "issues": [no_reviewer_issue],
        }
        empty = {"expand": "schema,names", "startAt": 5, "maxResults": 5, "total": 1, "issues": []}
        transport = _paginated_transport({0: page, 5: empty})
        collector = _offline_collector(config, transport)

        result = collector.collect()

        assert result.issue_count == 1
        assert result.review_event_count == 0
        assert result.review_events.num_rows == 0

    def test_issue_rows_populate_raw_columns(self, config):
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})
        collector = _offline_collector(config, transport)

        result = collector.collect()
        rows = {row["issue_key"]: row for row in result.issues.to_pylist()}

        row = rows["CASSANDRA-21712"]
        assert row["reporter_raw"] == "frankgh"
        assert row["assignee_raw"] == "frankgh"
        assert row["status"] == "Review In Progress"
        assert row["status_category"] == "In Progress"
        assert row["reporter_identity_id"] is None
        assert row["assignee_identity_id"] is None
        assert row["created_at"] == datetime(2026, 9, 23, 16, 46, 45, tzinfo=timezone.utc)
        assert row["updated_at"] == datetime(2026, 9, 25, 21, 38, 41, tzinfo=timezone.utc)
        assert row["resolved_at"] is None

        resolved_row = rows["CASSANDRA-21671"]
        assert resolved_row["resolved_at"] == datetime(2026, 9, 25, 19, 1, 14, tzinfo=timezone.utc)

    def test_reviewer_rows_dedupe_user_named_in_both_fields(self, config):
        """When the same JIRA username is named in both the multi-user and
        single-user reviewer fields for one issue, exactly one review_event
        row is emitted, with evidence recording the dedupe (issue #5)."""
        issue = json.loads(json.dumps(PAGE_1["issues"][0]))  # deep copy
        issue["fields"]["customfield_10022"] = {"name": "maedhroz"}
        page = {
            "expand": "schema,names",
            "startAt": 0,
            "maxResults": 5,
            "total": 1,
            "issues": [issue],
        }
        empty = {"expand": "schema,names", "startAt": 5, "maxResults": 5, "total": 1, "issues": []}

        transport = _paginated_transport({0: page, 5: empty})
        collector = _offline_collector(config, transport)

        result = collector.collect()
        rows = result.review_events.to_pylist()

        maedhroz_rows = [r for r in rows if r["reviewer_raw_value"] == "maedhroz"]
        assert len(maedhroz_rows) == 1
        assert "customfield_12313420" in maedhroz_rows[0]["evidence"]
        assert "customfield_10022" in maedhroz_rows[0]["evidence"]
        assert "dedup" in maedhroz_rows[0]["evidence"].lower()

    def test_rows_validate_against_schemas(self, config):
        """Acceptance criterion: rows validate against schemas — collect()
        already runs both tables through project_health.schema.validate, so
        this asserts the columns/types/nullability actually match by hand
        too (belt and suspenders against a future refactor bypassing
        validate())."""
        from project_health.schema import get_schema, validate

        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})
        collector = _offline_collector(config, transport)
        result = collector.collect()

        assert validate("issue", result.issues).schema.equals(get_schema("issue"))
        assert validate("review_event", result.review_events).schema.equals(
            get_schema("review_event")
        )


class TestRetry:
    def test_two_503s_then_200_succeeds(self, config):
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return httpx.Response(503, text="Service Unavailable")
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = JiraCollector(
            config,
            transport=transport,
            page_size=5,
            max_retries=5,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )

        result = collector.collect()

        assert call_count["n"] == 3
        assert result.issue_count == 0

    def test_five_503s_raises_collection_error(self, config):
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(503, text="Service Unavailable")

        transport = httpx.MockTransport(handler)
        collector = JiraCollector(
            config,
            transport=transport,
            page_size=5,
            max_retries=5,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )

        with pytest.raises(CollectionError):
            collector.collect()

        assert call_count["n"] == 5

    def test_retry_honors_retry_after_header(self, config):
        sleeps: list[float] = []
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, text="slow down")
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = JiraCollector(
            config,
            transport=transport,
            page_size=5,
            max_retries=5,
            min_request_interval=0,
            sleep_fn=sleeps.append,
        )

        collector.collect()

        assert 3.0 in sleeps


class TestWatermark:
    def test_first_run_omits_updated_clause(self):
        jql = build_jql("CASSANDRA", None)
        assert jql == "project=CASSANDRA ORDER BY updated ASC"
        assert "updated" not in jql.split("ORDER")[0]

    def test_second_run_builds_jql_from_stored_watermark(self):
        stored_watermark = "2026-09-25T21:38:41+00:00"
        jql = build_jql("CASSANDRA", stored_watermark)

        # Safety margin (2 minutes) subtracted and truncated to minute
        # precision — see collectors/jira.py module docstring.
        assert jql == 'project=CASSANDRA AND updated >= "2026-09-25 21:36" ORDER BY updated ASC'

    def test_collect_computes_next_watermark_as_max_updated_seen(self, config):
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})
        collector = _offline_collector(config, transport)

        result = collector.collect()

        all_updated = [
            issue["fields"]["updated"] for issue in PAGE_1["issues"] + PAGE_2["issues"]
        ]
        expected_max = max(all_updated)  # ISO strings sort lexicographically here
        assert result.next_watermark is not None
        assert result.next_watermark.startswith(expected_max[:19].replace("T", "T"))

    def test_second_collect_uses_stored_watermark_in_request(self, config):
        seen_jql: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            seen_jql.append(query["jql"][0])
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)

        collector.collect(watermark="2026-09-24T00:00:00+00:00")

        assert len(seen_jql) == 1
        assert 'updated >= "2026-09-23 23:58"' in seen_jql[0]


class TestFieldIdsFromConfig:
    def test_alternate_field_ids_are_read_from_config(self, tmp_path):
        import yaml

        config_path = tmp_path / "alt.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "project": {"id": "alt", "display_name": "Alt Project"},
                    "issue_tracker": {
                        "type": "jira",
                        "base_url": "https://issues.apache.org/jira",
                        "project_key": "CASSANDRA",
                    },
                    "reviewer_extraction": {
                        "commit_trailer": {"type": "commit_message_regex", "pattern": ".*"},
                        "jira_fields": {
                            "type": "jira_custom_field",
                            "reviewers_field": "customfield_99999",
                            "reviewer_field": "customfield_88888",
                        },
                    },
                }
            )
        )
        alt_config = load_project(config_path)

        issue = json.loads(json.dumps(PAGE_1["issues"][0]))
        issue["fields"]["customfield_99999"] = issue["fields"].pop("customfield_12313420")
        issue["fields"]["customfield_88888"] = None
        page = {
            "expand": "schema,names",
            "startAt": 0,
            "maxResults": 5,
            "total": 1,
            "issues": [issue],
        }
        empty = {"expand": "schema,names", "startAt": 5, "maxResults": 5, "total": 1, "issues": []}

        seen_fields_params: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            seen_fields_params.append(query["fields"][0])
            start_at = int(query.get("startAt", ["0"])[0])
            return httpx.Response(200, json=page if start_at == 0 else empty)

        transport = httpx.MockTransport(handler)
        collector = JiraCollector(
            alt_config,
            transport=transport,
            page_size=5,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )

        result = collector.collect()

        assert "customfield_99999" in seen_fields_params[0]
        assert "customfield_88888" in seen_fields_params[0]
        assert result.review_event_count == 1
        assert result.review_events.to_pylist()[0]["reviewer_raw_value"] == "maedhroz"


class TestNoNetworkAccess:
    def test_default_transport_is_not_used_when_mock_supplied(self, config):
        """Sanity check that supplying a transport fully replaces the real
        network path — if this ever silently fell back to a real HTTP
        client, MockTransport wouldn't be exercised and the fixture-based
        tests above would be lying about offline-safety."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            assert request.url.host == "issues.apache.org"
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)
        collector.collect()
        assert calls["n"] == 1
