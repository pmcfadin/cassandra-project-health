"""Tests for project_health.collectors.github (issue #51).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from `tests/fixtures/github/*.json` (recorded
GraphQL response shapes, with synthetic logins -- `alice-dev`,
`bob-reviewer`, `carol-committer`, plus `dependabot[bot]` /
`github-actions[bot]` for bot-exclusion coverage), and retry/rate-limit
tests inject a no-op `sleep_fn` so nothing here actually sleeps or touches
the network.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from project_health.collectors.github import (
    CollectionError,
    GitHubCollector,
    resolve_github_token,
)
from project_health.config import load_project

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "github"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


PAGE_1 = _load_fixture("pr_page1.json")
PAGE_2 = _load_fixture("pr_page2.json")
EMPTY_PAGE = _load_fixture("pr_empty.json")
RATE_LIMITED_ERROR = _load_fixture("rate_limited_error.json")


def _build_config(tmp_path: Path, repos: list[str], bot_patterns: list[dict] | None = None):
    bot_patterns = bot_patterns or [
        {"field": "github_login", "regex": r"(?i)\[bot\]$|^dependabot"},
    ]
    config_path = tmp_path / "test-project.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {"id": "test-project", "display_name": "Test Project"},
                "pull_requests": {"type": "github", "repos": repos},
                "bot_patterns": bot_patterns,
                "reviewer_extraction": {
                    "commit_trailer": {"type": "commit_message_regex", "pattern": ".*"},
                    "jira_fields": {"type": "jira_custom_field"},
                },
            }
        )
    )
    return load_project(config_path)


def _cursor_transport(pages_by_cursor: dict[str | None, dict]) -> httpx.MockTransport:
    """A `MockTransport` that serves `pages_by_cursor[variables['cursor']]`."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body["variables"].get("cursor")
        if cursor not in pages_by_cursor:
            raise AssertionError(f"unexpected cursor={cursor!r}")
        return httpx.Response(200, json=pages_by_cursor[cursor])

    return httpx.MockTransport(handler)


def _offline_collector(
    config, transport: httpx.MockTransport, **kwargs
) -> GitHubCollector:
    kwargs.setdefault("min_request_interval", 0)
    kwargs.setdefault("sleep_fn", lambda s: None)
    kwargs.setdefault("token", "test-token")
    return GitHubCollector(config, transport=transport, **kwargs)


@pytest.fixture
def single_repo_config(tmp_path):
    return _build_config(tmp_path, ["synthtest/repo-a"])


class TestPaginationAndNormalization:
    def test_pagination_across_both_pages_yields_expected_pr_count(self, single_repo_config):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()

        # 3 PR nodes fetched (#100, #101 bot, #102), #101 is dependabot -> excluded.
        assert result.prs.num_rows == 2
        assert set(result.prs.column("number").to_pylist()) == {100, 102}
        assert result.status == "ok"
        assert result.repos["synthtest/repo-a"].status == "ok"
        assert result.repos["synthtest/repo-a"].bot_prs_excluded == 1

    def test_pr_rows_populate_raw_columns_and_repo_label_from_config(self, single_repo_config):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        rows = {row["number"]: row for row in result.prs.to_pylist()}

        row = rows[100]
        assert row["repo"] == "synthtest/repo-a"
        assert row["author_raw_type"] == "github_login"
        assert row["author_raw_value"] == "alice-dev"
        assert row["author_identity_id"] is None
        assert row["state"] == "MERGED"
        assert row["merged"] is True
        assert row["is_draft"] is False
        assert row["additions"] == 10
        assert row["deletions"] == 2
        assert row["changed_files"] == 3
        assert row["created_at"] == datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        assert row["updated_at"] == datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc)
        assert row["merged_at"] == datetime(2026, 1, 2, 9, 0, 0, tzinfo=timezone.utc)

        draft_row = rows[102]
        assert draft_row["is_draft"] is True
        assert draft_row["closed_at"] is None
        assert draft_row["merged_at"] is None

    def test_title_is_never_stored_only_its_sha256_hash(self, single_repo_config):
        """Acceptance criterion: metadata only -- no PR title text persisted."""
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        rows = {row["number"]: row for row in result.prs.to_pylist()}

        expected_hash = hashlib.sha256(
            b"Fix bug in synthetic module X"
        ).hexdigest()
        assert rows[100]["title_hash"] == expected_hash
        # The raw title string never appears anywhere in the row.
        for row in rows.values():
            for value in row.values():
                assert value != "Fix bug in synthetic module X"
                assert value != "Add synthetic feature Y"

    def test_reviews_normalized_with_reviewer_raw_value(self, single_repo_config):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        reviews = result.reviews.to_pylist()

        # PR #100's review (bob-reviewer) is kept; PR #102's review is by
        # github-actions[bot] and excluded.
        assert len(reviews) == 1
        review = reviews[0]
        assert review["reviewer_raw_type"] == "github_login"
        assert review["reviewer_raw_value"] == "bob-reviewer"
        assert review["pr_number"] == 100
        assert review["repo"] == "synthtest/repo-a"
        assert review["state"] == "APPROVED"
        assert review["submitted_at"] == datetime(2026, 1, 2, 8, 0, 0, tzinfo=timezone.utc)
        assert review["reviewer_identity_id"] is None
        assert result.repos["synthtest/repo-a"].bot_reviews_excluded == 1

    def test_comments_normalized_issue_and_review_types_with_review_id_link(
        self, single_repo_config
    ):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        comments = {row["comment_id"]: row for row in result.comments.to_pylist()}

        review_comment = comments["PRRC_kwDOtest0001"]
        assert review_comment["comment_type"] == "review_comment"
        assert review_comment["review_id"] == "PRR_kwDOtest0001"
        assert review_comment["author_raw_value"] == "bob-reviewer"
        assert review_comment["pr_number"] == 100

        issue_comment = comments["IC_kwDOtest0001"]
        assert issue_comment["comment_type"] == "issue_comment"
        assert issue_comment["review_id"] is None
        assert issue_comment["author_raw_value"] == "carol-committer"

    def test_null_author_on_comment_yields_null_raw_value_not_a_crash(self, single_repo_config):
        """PR #102's issue comment has `author: null` (deleted account)."""
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        comments = {row["comment_id"]: row for row in result.comments.to_pylist()}

        orphaned = comments["IC_kwDOtest0002"]
        assert orphaned["author_raw_type"] == "github_login"
        assert orphaned["author_raw_value"] is None

    def test_rows_validate_against_schemas(self, single_repo_config):
        from project_health.schema import get_schema, validate

        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)
        result = collector.collect()

        assert validate("pr", result.prs).schema.equals(get_schema("pr"))
        assert validate("pr_review", result.reviews).schema.equals(get_schema("pr_review"))
        assert validate("pr_comment", result.comments).schema.equals(get_schema("pr_comment"))


class TestBotExclusion:
    def test_bot_pr_excluded_but_human_review_on_it_would_still_be_kept(self, tmp_path):
        """A bot-authored PR is dropped from `pr`, but a human review/comment
        on that same PR is still collected independently (module docstring)."""
        page = json.loads(json.dumps(PAGE_1))
        # Give the dependabot PR (#101) a human review.
        page["data"]["repository"]["pullRequests"]["nodes"][1]["reviews"]["nodes"] = [
            {
                "id": "PRR_kwDOtest0099",
                "state": "APPROVED",
                "submittedAt": "2026-01-03T02:00:00Z",
                "author": {"login": "bob-reviewer"},
                "comments": {"nodes": []},
            }
        ]
        page["data"]["repository"]["pullRequests"]["pageInfo"] = {
            "hasNextPage": False,
            "endCursor": "cursorAAA",
        }
        config = _build_config(tmp_path, ["synthtest/repo-a"])
        transport = _cursor_transport({None: page})
        collector = _offline_collector(config, transport)

        result = collector.collect()

        assert result.prs.num_rows == 1  # only #100; #101 (dependabot) excluded
        # bob-reviewer's reviews on both #100 (fixture) and #101 (added above) are kept.
        assert result.reviews.num_rows == 2
        assert set(result.reviews.column("pr_number").to_pylist()) == {100, 101}


class TestWatermark:
    def test_first_run_sends_null_cursor(self, single_repo_config):
        seen_cursors = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_cursors.append(body["variables"].get("cursor"))
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport)

        collector.collect()

        assert seen_cursors == [None]

    def test_next_watermark_is_last_page_end_cursor(self, single_repo_config):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()

        assert result.repos["synthtest/repo-a"].next_watermark == "cursorBBB"

    def test_second_run_passes_stored_watermark_as_after_cursor(self, single_repo_config):
        seen_cursors = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_cursors.append(body["variables"].get("cursor"))
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport)

        collector.collect(watermarks={"synthtest/repo-a": "cursorBBB"})

        assert seen_cursors == ["cursorBBB"]

    def test_repo_with_no_prior_watermark_keeps_none_when_nothing_fetched(
        self, single_repo_config
    ):
        transport = _cursor_transport({None: EMPTY_PAGE})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()

        # endCursor is null on an empty page -> watermark stays unchanged (None).
        assert result.repos["synthtest/repo-a"].next_watermark is None


class TestMultiRepo:
    def test_watermark_and_data_are_independent_per_repo(self, tmp_path):
        config = _build_config(tmp_path, ["synthtest/repo-a", "synthtest/repo-b"])

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            name = body["variables"]["name"]
            cursor = body["variables"].get("cursor")
            if name == "repo-a":
                return httpx.Response(200, json=PAGE_1 if cursor is None else PAGE_2)
            assert name == "repo-b"
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)

        result = collector.collect(watermarks={"synthtest/repo-a": None})

        assert result.repos["synthtest/repo-a"].status == "ok"
        assert result.repos["synthtest/repo-a"].pr_count == 2
        assert result.repos["synthtest/repo-b"].status == "ok"
        assert result.repos["synthtest/repo-b"].pr_count == 0
        assert all(row["repo"] == "synthtest/repo-a" for row in result.prs.to_pylist())


class TestRateLimitBudgeting:
    def test_stops_cleanly_when_remaining_drops_to_floor_and_keeps_collected_data(
        self, single_repo_config
    ):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport, rate_limit_floor=4945)

        result = collector.collect()

        # Page 1 reports remaining=4950 (> floor) so page 2 is still fetched;
        # page 2 reports remaining=4940 (<= floor 4945) so pagination stops
        # there, but page 2's own data (already fetched) is kept.
        outcome = result.repos["synthtest/repo-a"]
        assert outcome.status == "rate_limited"
        assert outcome.pr_count == 2
        assert result.status == "partial"  # source-level: a clean, resumable partial backfill

    def test_subsequent_repos_marked_skipped_with_watermark_untouched(self, tmp_path):
        config = _build_config(tmp_path, ["synthtest/repo-a", "synthtest/repo-b"])

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            name = body["variables"]["name"]
            assert name == "repo-a", "repo-b must never be queried once budget is exhausted"
            return httpx.Response(200, json=PAGE_1)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport, rate_limit_floor=4999)

        result = collector.collect(watermarks={"synthtest/repo-b": "priorCursorB"})

        assert result.repos["synthtest/repo-a"].status == "rate_limited"
        assert result.repos["synthtest/repo-b"].status == "skipped"
        # Data kept, watermark untouched for the skipped repo.
        assert result.repos["synthtest/repo-b"].next_watermark == "priorCursorB"
        assert result.repos["synthtest/repo-b"].pr_count == 0
        # repo-a's page-1 data is kept (2 nodes fetched, #101 dependabot excluded).
        assert result.prs.num_rows == 1

    def test_inline_rate_limited_graphql_error_stops_cleanly_without_raising(
        self, single_repo_config
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=RATE_LIMITED_ERROR)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()  # must not raise

        assert result.repos["synthtest/repo-a"].status == "rate_limited"
        assert result.prs.num_rows == 0
        assert result.status == "partial"


class TestMixedRepoStatusRollup:
    def test_one_ok_one_failed_repo_rolls_up_to_failed(self, tmp_path):
        """A hard failure anywhere always wins over 'ok'/'partial' (issue #54:
        `overall_status` is now three-valued -- 'ok' | 'partial' | 'failed')."""
        config = _build_config(tmp_path, ["synthtest/repo-a", "synthtest/repo-b"])

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["variables"]["name"] == "repo-a":
                return httpx.Response(200, json=EMPTY_PAGE)
            return httpx.Response(503, text="Service Unavailable")

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport, max_retries=1)

        result = collector.collect()

        assert result.repos["synthtest/repo-a"].status == "ok"
        assert result.repos["synthtest/repo-b"].status == "failed"
        assert result.status == "failed"


class TestRetry:
    def test_two_503s_then_200_succeeds(self, single_repo_config):
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return httpx.Response(503, text="Service Unavailable")
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport, max_retries=5)

        result = collector.collect()

        assert call_count["n"] == 3
        assert result.repos["synthtest/repo-a"].status == "ok"

    def test_retries_exhausted_marks_repo_failed_and_does_not_raise(self, single_repo_config):
        """Per issue #51: a hard failure stops cleanly (source `failed`, data
        kept) rather than propagating an exception out of `collect()`."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport, max_retries=3)

        result = collector.collect()  # must not raise CollectionError

        assert result.repos["synthtest/repo-a"].status == "failed"
        assert result.status == "failed"
        assert result.prs.num_rows == 0

    def test_retry_honors_retry_after_header(self, single_repo_config):
        sleeps: list[float] = []
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(403, headers={"Retry-After": "2"}, text="abuse detected")
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(
            single_repo_config, transport, max_retries=5, sleep_fn=sleeps.append
        )

        collector.collect()

        assert 2.0 in sleeps

    def test_truncated_json_body_retries_like_a_5xx_then_succeeds(self, single_repo_config):
        """issue #86: a truncated/undecodable GraphQL body (a real example
        seen live: 'Unterminated string ... char 219262') is retried
        exactly like a 5xx, not raised straight through with no retry."""
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(200, content=b'{"data": {"repository": {"pull')
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport, max_retries=5)

        result = collector.collect()

        assert call_count["n"] == 2
        assert result.repos["synthtest/repo-a"].status == "ok"

    def test_truncated_json_body_exhausted_marks_repo_failed(self, single_repo_config):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b'{"data": {"repository": {"pull')

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport, max_retries=3)

        result = collector.collect()  # must not raise

        assert result.repos["synthtest/repo-a"].status == "failed"
        assert result.status == "failed"


class TestTokenResolution:
    def test_gh_pat_takes_priority(self):
        token = resolve_github_token(
            env={"GH_PAT": "pat-token", "GITHUB_TOKEN": "actions-token"},
            gh_auth_token=lambda: "gh-cli-token",
        )
        assert token == "pat-token"

    def test_falls_back_to_github_token(self):
        token = resolve_github_token(
            env={"GITHUB_TOKEN": "actions-token"}, gh_auth_token=lambda: "gh-cli-token"
        )
        assert token == "actions-token"

    def test_falls_back_to_gh_auth_token_for_local_runs(self):
        token = resolve_github_token(env={}, gh_auth_token=lambda: "gh-cli-token")
        assert token == "gh-cli-token"

    def test_returns_none_when_nothing_available(self):
        token = resolve_github_token(env={}, gh_auth_token=lambda: None)
        assert token is None


class TestConfig:
    def test_missing_pull_requests_repos_raises(self, tmp_path):
        config = _build_config(tmp_path, [])
        with pytest.raises(ValueError, match="pull_requests.repos"):
            GitHubCollector(config, transport=httpx.MockTransport(lambda r: httpx.Response(200)))


class TestNoNetworkAccess:
    def test_default_transport_is_not_used_when_mock_supplied(self, single_repo_config):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            assert request.url.host == "api.github.com"
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport)
        collector.collect()
        assert calls["n"] == 1


# Sanity: CollectionError is importable and is the base for hard failures
# (exercised indirectly above; this just guards the public surface).
def test_collection_error_is_exported():
    assert issubclass(CollectionError, Exception)
