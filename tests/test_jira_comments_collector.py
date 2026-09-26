"""Tests for project_health.collectors.jira_comments (issue #36).

All tests use an offline `httpx.MockTransport` -- never the network.
"""

from __future__ import annotations

import httpx
import pytest

from project_health.collectors.jira_comments import (
    CollectionError,
    JiraCommentsCollector,
    _find_ci_evidence,
)


def _transport(comments_by_issue: dict[str, list[dict]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        # path: <base_url path prefix>/rest/api/2/issue/{key}/comment -- the
        # issue key is always the second-to-last segment, regardless of
        # whether base_url itself has a path prefix (e.g. "/jira").
        key = request.url.path.split("/")[-2]
        if key not in comments_by_issue:
            return httpx.Response(404, json={"errorMessages": ["not found"]})
        return httpx.Response(
            200,
            json={
                "comments": comments_by_issue[key],
                "total": len(comments_by_issue[key]),
                "startAt": 0,
                "maxResults": 50,
            },
        )

    return httpx.MockTransport(handler)


def _collector(comments_by_issue: dict[str, list[dict]]) -> JiraCommentsCollector:
    return JiraCommentsCollector(
        "https://issues.apache.org/jira",
        transport=_transport(comments_by_issue),
        min_request_interval=0,
        sleep_fn=lambda _seconds: None,
    )


def _comment(comment_id: str, author: str, created: str, body: str) -> dict:
    return {"id": comment_id, "author": {"name": author}, "created": created, "body": body}


class TestFindCiEvidence:
    def test_matches_a_term_and_extracts_matching_url(self):
        body = "Ran the patch through CircleCI: https://circleci.com/gh/apache/cassandra/1234"
        result = _find_ci_evidence(body, ("circleci",))
        assert result == ("circleci", "https://circleci.com/gh/apache/cassandra/1234")

    def test_matches_case_insensitively(self):
        result = _find_ci_evidence("Jenkins run attached", ("jenkins",))
        assert result is not None
        assert result[0] == "jenkins"

    def test_no_match_returns_none(self):
        assert _find_ci_evidence("looks good to me, +1", ("jenkins", "circleci")) is None

    def test_term_present_but_no_url_returns_term_with_none_url(self):
        result = _find_ci_evidence("ran butler for this", ("butler",))
        assert result == ("butler", None)


class TestJiraCommentsCollector:
    def test_fetch_ci_evidence_finds_first_matching_comment(self):
        comments = {
            "CASSANDRA-100": [
                _comment("1", "alice", "2024-06-01T00:00:00.000+0000", "no CI mention here"),
                _comment(
                    "2",
                    "bob",
                    "2024-06-02T00:00:00.000+0000",
                    "ci_summary: https://ci-cassandra.apache.org/job/x/1",
                ),
            ]
        }
        with _collector(comments) as collector:
            evidence = collector.fetch_ci_evidence("CASSANDRA-100")
        assert evidence is not None
        assert evidence.comment_id == "2"
        assert evidence.comment_author == "bob"
        assert evidence.matched_term == "ci_summary"
        assert evidence.matched_url == "https://ci-cassandra.apache.org/job/x/1"

    def test_fetch_ci_evidence_none_when_no_comment_matches(self):
        comments = {"CASSANDRA-100": [_comment("1", "a", "x", "lgtm")]}
        with _collector(comments) as collector:
            assert collector.fetch_ci_evidence("CASSANDRA-100") is None

    def test_fetch_ci_evidence_none_for_unknown_issue(self):
        with _collector({}) as collector:
            assert collector.fetch_ci_evidence("CASSANDRA-999") is None

    def test_fetch_ci_evidence_for_issues_only_includes_matches(self):
        comments = {
            "CASSANDRA-1": [_comment("1", "a", "x", "jenkins run ok")],
            "CASSANDRA-2": [_comment("2", "b", "x", "no mention")],
        }
        with _collector(comments) as collector:
            found = collector.fetch_ci_evidence_for_issues(
                ["CASSANDRA-1", "CASSANDRA-2", "CASSANDRA-3"]
            )
        assert set(found) == {"CASSANDRA-1"}

    def test_never_stores_comment_body(self):
        """CommentCIEvidence must never carry the raw body text (issue #36
        scope: 'store comment metadata plus the matched CI URL only, never
        comment bodies')."""
        body_text = "a very specific secret-looking sentence about jenkins"
        comments = {"CASSANDRA-1": [_comment("1", "a", "x", body_text)]}
        with _collector(comments) as collector:
            evidence = collector.fetch_ci_evidence("CASSANDRA-1")
        assert evidence is not None
        for value in vars(evidence).values():
            assert body_text != value

    @pytest.mark.parametrize("retries", [1])
    def test_retries_on_500_then_succeeds(self, retries):
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"comments": [], "total": 0})

        collector = JiraCommentsCollector(
            "https://issues.apache.org/jira",
            transport=httpx.MockTransport(handler),
            min_request_interval=0,
            sleep_fn=lambda _seconds: None,
        )
        with collector:
            assert collector.fetch_ci_evidence("CASSANDRA-1") is None
        assert attempts["count"] == 2

    def test_truncated_json_body_retries_like_a_5xx_then_succeeds(self):
        """issue #86: a truncated/undecodable JSON body is retried exactly
        like a 5xx, not raised straight through."""
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(200, content=b'{"comments": [')
            return httpx.Response(200, json={"comments": [], "total": 0})

        collector = JiraCommentsCollector(
            "https://issues.apache.org/jira",
            transport=httpx.MockTransport(handler),
            min_request_interval=0,
            sleep_fn=lambda _seconds: None,
        )
        with collector:
            assert collector.fetch_ci_evidence("CASSANDRA-1") is None
        assert attempts["count"] == 2

    def test_truncated_json_body_exhausted_raises_collection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b'{"comments": [')

        collector = JiraCommentsCollector(
            "https://issues.apache.org/jira",
            transport=httpx.MockTransport(handler),
            min_request_interval=0,
            sleep_fn=lambda _seconds: None,
            max_retries=3,
        )
        with collector, pytest.raises(CollectionError):
            collector.fetch_ci_evidence("CASSANDRA-1")


class TestFetchCommentMetadata:
    """Issue #79: `fetch_comment_metadata` returns *every* comment's
    metadata for the historical `issue_comment` backfill, not just a
    CI-evidence match."""

    def test_returns_metadata_for_every_comment(self):
        comments = {
            "CASSANDRA-100": [
                _comment("1", "alice", "2024-06-01T00:00:00.000+0000", "first response"),
                _comment("2", "bob", "2024-06-02T00:00:00.000+0000", "second response"),
            ]
        }
        with _collector(comments) as collector:
            rows = collector.fetch_comment_metadata("CASSANDRA-100")
        assert [r["comment_id"] for r in rows] == ["1", "2"]
        assert rows[0]["author_raw_value"] == "alice"
        assert rows[0]["author_raw_type"] == "jira_username"
        assert rows[0]["issue_key"] == "CASSANDRA-100"
        assert rows[0]["author_identity_id"] is None
        assert rows[0]["created_at"].isoformat() == "2024-06-01T00:00:00+00:00"

    def test_never_returns_comment_body(self):
        body_text = "a very specific secret-looking sentence"
        comments = {
            "CASSANDRA-1": [_comment("1", "alice", "2024-06-01T00:00:00.000+0000", body_text)]
        }
        with _collector(comments) as collector:
            rows = collector.fetch_comment_metadata("CASSANDRA-1")
        assert len(rows) == 1
        for value in rows[0].values():
            assert value != body_text

    def test_returns_empty_list_for_issue_with_no_comments(self):
        with _collector({"CASSANDRA-1": []}) as collector:
            assert collector.fetch_comment_metadata("CASSANDRA-1") == []

    def test_returns_empty_list_for_404_issue(self):
        with _collector({}) as collector:
            assert collector.fetch_comment_metadata("CASSANDRA-999") == []

    def test_capped_at_max_comments_per_issue_stored(self):
        from project_health.collectors.jira import MAX_COMMENTS_PER_ISSUE_STORED

        comments = {
            "CASSANDRA-1": [
                _comment(str(i), f"user{i}", "2024-06-01T00:00:00.000+0000", "x")
                for i in range(MAX_COMMENTS_PER_ISSUE_STORED + 10)
            ]
        }
        with _collector(comments) as collector:
            rows = collector.fetch_comment_metadata("CASSANDRA-1")
        assert len(rows) == MAX_COMMENTS_PER_ISSUE_STORED

    def test_paginates_across_multiple_pages(self):
        all_comments = [
            _comment(str(i), f"user{i}", "2024-06-01T00:00:00.000+0000", "x") for i in range(7)
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            from urllib.parse import parse_qs

            query = parse_qs(request.url.query.decode())
            start_at = int(query.get("startAt", ["0"])[0])
            max_results = int(query.get("maxResults", ["5"])[0])
            page = all_comments[start_at : start_at + max_results]
            return httpx.Response(
                200,
                json={
                    "comments": page,
                    "total": len(all_comments),
                    "startAt": start_at,
                    "maxResults": max_results,
                },
            )

        collector = JiraCommentsCollector(
            "https://issues.apache.org/jira",
            transport=httpx.MockTransport(handler),
            page_size=3,
            min_request_interval=0,
            sleep_fn=lambda _seconds: None,
        )
        with collector:
            rows = collector.fetch_comment_metadata("CASSANDRA-1")
        assert [r["comment_id"] for r in rows] == [str(i) for i in range(7)]

    def test_skips_a_comment_missing_created(self):
        comments = {
            "CASSANDRA-1": [
                {"id": "1", "author": {"name": "alice"}, "created": None, "body": "x"},
                _comment("2", "bob", "2024-06-02T00:00:00.000+0000", "y"),
            ]
        }
        with _collector(comments) as collector:
            rows = collector.fetch_comment_metadata("CASSANDRA-1")
        assert [r["comment_id"] for r in rows] == ["2"]
