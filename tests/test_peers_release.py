"""Tests for project_health.peers.release (issue #145).

Fully offline: every HTTP call goes through an injected `httpx.MockTransport`
with a no-op `sleep_fn`, same pattern as `tests/test_release_collector.py`.
"""

from __future__ import annotations

from datetime import date

import httpx

from project_health.peers.config import (
    GithubReleasesVerification,
    JiraReleaseVerification,
    PeerProject,
    PypiReleaseVerification,
)
from project_health.peers.release import fetch_ga_tags, verify_release_counts


def _noop_sleep(_seconds: float) -> None:
    pass


def _peer(verification) -> PeerProject:
    return PeerProject(
        id="kafka",
        display_name="Apache Kafka",
        repo="apache/kafka",
        default_branch="trunk",
        tag_prefix="",
        release_verification=verification,
    )


class TestFetchGaTags:
    def _transport(self, tags_pages: list[list[dict]], commit_dates: dict[str, str]):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/tags"):
                page = int(request.url.params.get("page", "1"))
                items = tags_pages[page - 1] if page <= len(tags_pages) else []
                return httpx.Response(200, json=items)
            for sha, when in commit_dates.items():
                if path.endswith(f"/commits/{sha}"):
                    return httpx.Response(
                        200,
                        json={"commit": {"committer": {"date": when}}},
                    )
            return httpx.Response(404, json={"message": "Not Found"})

        return httpx.MockTransport(handler)

    def test_filters_to_ga_tags_only(self):
        tags_pages = [
            [
                {"name": "4.3.1", "commit": {"sha": "sha1"}},
                {"name": "4.3.1-rc1", "commit": {"sha": "sha2"}},
                {"name": "kafka-0.7.2-incubating-candidate-5", "commit": {"sha": "sha3"}},
            ]
        ]
        commit_dates = {"sha1": "2026-05-01T00:00:00Z"}
        transport = self._transport(tags_pages, commit_dates)

        tags = fetch_ga_tags(
            "apache", "kafka", "", transport=transport, sleep_fn=_noop_sleep
        )
        assert [t.tag_name for t in tags] == ["4.3.1"]
        assert tags[0].version == "4.3.1"
        assert tags[0].major_minor == "4.3"
        assert tags[0].release_date == date(2026, 5, 1)

    def test_paginates_until_short_page(self):
        tags_pages = [
            [{"name": f"1.{i}.0", "commit": {"sha": f"sha{i}"}} for i in range(100)],
            [{"name": "2.0.0", "commit": {"sha": "sha_last"}}],
        ]
        commit_dates = {f"sha{i}": "2024-01-01T00:00:00Z" for i in range(100)}
        commit_dates["sha_last"] = "2025-01-01T00:00:00Z"
        transport = self._transport(tags_pages, commit_dates)

        tags = fetch_ga_tags(
            "apache", "kafka", "", transport=transport, sleep_fn=_noop_sleep, page_size=100
        )
        assert len(tags) == 101

    def test_sorted_by_release_date(self):
        tags_pages = [
            [
                {"name": "2.0.0", "commit": {"sha": "sha_new"}},
                {"name": "1.0.0", "commit": {"sha": "sha_old"}},
            ]
        ]
        commit_dates = {"sha_new": "2026-01-01T00:00:00Z", "sha_old": "2020-01-01T00:00:00Z"}
        transport = self._transport(tags_pages, commit_dates)

        tags = fetch_ga_tags("apache", "kafka", "", transport=transport, sleep_fn=_noop_sleep)
        assert [t.version for t in tags] == ["1.0.0", "2.0.0"]


class TestVerifyReleaseCounts:
    def test_jira_verification(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"released": True, "releaseDate": "2023-05-01", "name": "3.5.0"},
                    {"released": True, "releaseDate": "2023-11-01", "name": "3.6.0"},
                    {"released": True, "releaseDate": "2024-01-01", "name": "3.7.0"},
                    {"released": False, "name": "4.0.0"},
                ],
            )

        transport = httpx.MockTransport(handler)
        peer = _peer(
            JiraReleaseVerification(base_url="https://issues.apache.org/jira", project_key="KAFKA")
        )
        tag_dates = [date(2023, 5, 2), date(2023, 11, 2), date(2024, 1, 2)]

        result = verify_release_counts(peer, tag_dates, transport=transport, sleep_fn=_noop_sleep)
        assert result.source_type == "jira"
        assert result.tag_derived_by_year == {2023: 2, 2024: 1}
        assert result.independent_by_year == {2023: 2, 2024: 1}
        assert result.fetch_error is None

    def test_github_releases_verification(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"tag_name": "v5.0.0", "published_at": "2026-10-05T19:22:24Z"},
                    {"tag_name": "v4.2.5", "published_at": "2026-05-05T19:23:03Z"},
                ],
            )

        transport = httpx.MockTransport(handler)
        peer = _peer(GithubReleasesVerification())
        tag_dates = [date(2026, 10, 5), date(2026, 5, 5)]

        result = verify_release_counts(peer, tag_dates, transport=transport, sleep_fn=_noop_sleep)
        assert result.source_type == "github_releases"
        assert result.independent_by_year == {2026: 2}

    def test_pypi_verification(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "releases": {
                        "54.0.0": [{"upload_time_iso_8601": "2026-06-29T11:19:02.100713Z"}],
                        "54.1.0": [{"upload_time_iso_8601": "2026-10-05T12:21:06.308732Z"}],
                        "unreleased": [],
                    }
                },
            )

        transport = httpx.MockTransport(handler)
        peer = _peer(PypiReleaseVerification(package="datafusion"))
        tag_dates = [date(2026, 6, 29), date(2026, 10, 5)]

        result = verify_release_counts(peer, tag_dates, transport=transport, sleep_fn=_noop_sleep)
        assert result.source_type == "pypi"
        assert result.independent_by_year == {2026: 2}

    def test_github_releases_404_is_zero_not_error(self):
        """apache/datafusion's own real situation (peers.yaml's own comment):
        a 404/empty Releases list is zero items, not a fetch failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        transport = httpx.MockTransport(handler)
        peer = _peer(GithubReleasesVerification())

        result = verify_release_counts(
            peer, [date(2026, 1, 1)], transport=transport, sleep_fn=_noop_sleep
        )
        assert result.independent_by_year == {}
        assert result.fetch_error is None

    def test_fetch_error_is_disclosed_not_raised(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        transport = httpx.MockTransport(handler)
        peer = _peer(GithubReleasesVerification())

        result = verify_release_counts(
            peer, [date(2026, 1, 1)], transport=transport, sleep_fn=_noop_sleep, max_retries=1
        )
        assert result.fetch_error is not None
        assert result.independent_by_year == {}
        assert result.tag_derived_by_year == {2026: 1}
