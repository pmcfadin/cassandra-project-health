"""Tests for project_health.collectors.jira_comments (issue #36).

All tests use an offline `httpx.MockTransport` -- never the network.
"""

from __future__ import annotations

import httpx
import pytest

from project_health.collectors.jira_comments import JiraCommentsCollector, _find_ci_evidence


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
