"""Tests for project_health.collectors.github_issues (issue #174).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from `tests/fixtures/github_issues/*.json`
(synthetic GraphQL response shapes, mirroring `tests/test_github_collector.
py`'s own fixture convention for `collectors/github.py`), and retry/
rate-limit tests inject a no-op `sleep_fn` so nothing here actually sleeps
or touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from project_health.collectors.github_issues import (
    GitHubIssuesCollector,
)
from project_health.config import load_project

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "github_issues"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


PAGE_1 = _load_fixture("issues_page1.json")
PAGE_2 = _load_fixture("issues_page2.json")
EMPTY_PAGE = _load_fixture("issues_empty.json")
LOW_REMAINING_PAGE = _load_fixture("issues_page_low_remaining.json")
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
                "github_issues": {"repos": repos},
                "bot_patterns": bot_patterns,
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


def _offline_collector(config, transport: httpx.MockTransport, **kwargs) -> GitHubIssuesCollector:
    kwargs.setdefault("min_request_interval", 0)
    kwargs.setdefault("sleep_fn", lambda s: None)
    kwargs.setdefault("token", "test-token")
    return GitHubIssuesCollector(config, transport=transport, **kwargs)


@pytest.fixture
def single_repo_config(tmp_path):
    return _build_config(tmp_path, ["synthtest/repo-a"])


class TestPaginationAndNormalization:
    def test_pagination_across_both_pages_yields_expected_issue_count(self, single_repo_config):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()

        # 4 issue nodes fetched across both pages (#200, #201 bot, #202);
        # #201 is dependabot[bot] -> excluded.
        assert result.issues.num_rows == 2
        assert set(result.issues.column("number").to_pylist()) == {200, 202}
        assert result.status == "ok"
        assert result.repos["synthtest/repo-a"].status == "ok"
        assert result.repos["synthtest/repo-a"].bot_issues_excluded == 1

    def test_issue_rows_populate_raw_columns_labels_and_repo_label(self, single_repo_config):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        rows = {row["number"]: row for row in result.issues.to_pylist()}

        row = rows[200]
        assert row["repo"] == "synthtest/repo-a"
        assert row["author_raw_type"] == "github_login"
        assert row["author_raw_value"] == "alice-dev"
        assert row["author_identity_id"] is None
        assert row["state"] == "OPEN"
        assert sorted(row["labels"]) == ["bug", "help wanted"]
        assert row["closed_at"] is None

        closed_row = rows[202]
        assert closed_row["labels"] == ["question"]

    def test_comments_exclude_bots_but_keep_human_and_self_comments(self, single_repo_config):
        """Bot comment exclusion happens at collection time (module
        docstring); a comment by the issue's own author is still collected
        here -- it's the metrics layer (`metrics/github_issues.py`) that
        excludes self-comments when computing first response."""
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        comment_authors = sorted(result.comments.column("author_raw_value").to_pylist())

        # IC_kwDOtest1001 (bob-reviewer, on #200), IC_kwDOtest1002
        # (carol-committer's own self-comment, on #202), IC_kwDOtest1004
        # (bob-reviewer, on #202). IC_kwDOtest1003 (github-actions[bot]) is
        # excluded.
        assert comment_authors == ["bob-reviewer", "bob-reviewer", "carol-committer"]
        assert result.repos["synthtest/repo-a"].bot_comments_excluded == 1

    def test_no_comment_body_column_exists_anywhere(self, single_repo_config):
        """D1: metadata only, never a comment/issue body -- the schema
        itself has no such column at all."""
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        assert "body" not in result.issues.column_names
        assert "title" not in result.issues.column_names
        assert "body" not in result.comments.column_names

    def test_comment_rows_carry_issue_number_and_repo(self, single_repo_config):
        transport = _cursor_transport({None: PAGE_1, "cursorAAA": PAGE_2})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        by_id = {row["comment_id"]: row for row in result.comments.to_pylist()}
        row = by_id["IC_kwDOtest1001"]
        assert row["repo"] == "synthtest/repo-a"
        assert row["issue_number"] == 200
        assert row["author_raw_type"] == "github_login"

    def test_watermark_resumes_from_stored_cursor(self, single_repo_config):
        """A prior watermark is passed back in as `after` on the next run."""
        seen_cursors: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            cursor = body["variables"].get("cursor")
            seen_cursors.append(cursor)
            return httpx.Response(200, json=EMPTY_PAGE)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single_repo_config, transport)
        collector.collect(watermarks={"synthtest/repo-a": "cursorAAA"})

        assert seen_cursors == ["cursorAAA"]

    def test_empty_repo_yields_zero_rows_status_ok(self, single_repo_config):
        transport = _cursor_transport({None: EMPTY_PAGE})
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        assert result.issues.num_rows == 0
        assert result.comments.num_rows == 0
        assert result.status == "ok"


class TestRateLimitBudgeting:
    def test_floor_reached_stops_pagination_and_marks_rate_limited(self, single_repo_config):
        """Mirrors collectors/github.py's own floor behavior: a page whose
        `rateLimit.remaining` is at/below the floor stops that repo
        cleanly, keeping rows already collected, rather than paginating
        further."""
        transport = _cursor_transport({None: LOW_REMAINING_PAGE})
        collector = _offline_collector(single_repo_config, transport, rate_limit_floor=10)

        result = collector.collect()

        assert result.issues.num_rows == 1  # the one row on the low-remaining page is kept
        assert result.repos["synthtest/repo-a"].status == "rate_limited"
        assert result.repos["synthtest/repo-a"].next_watermark == "cursorLOW"
        assert result.status == "partial"

    def test_floor_reached_on_first_repo_skips_subsequent_repos(self, tmp_path):
        config = _build_config(tmp_path, ["synthtest/repo-a", "synthtest/repo-b"])
        transport = _cursor_transport({None: LOW_REMAINING_PAGE})
        collector = _offline_collector(config, transport, rate_limit_floor=10)

        result = collector.collect()

        assert result.repos["synthtest/repo-a"].status == "rate_limited"
        assert result.repos["synthtest/repo-b"].status == "skipped"
        assert result.repos["synthtest/repo-b"].issue_count == 0

    def test_effective_floor_capped_by_floor_fraction(self, single_repo_config):
        """Reuses `collectors.github.github_rate_limit_effective_floor`
        verbatim (module docstring "Rate-limit budgeting") -- this is a
        direct check that the imported helper behaves as documented
        through this collector's own `effective_floor` method."""
        collector = _offline_collector(
            single_repo_config,
            httpx.MockTransport(lambda r: httpx.Response(200, json=EMPTY_PAGE)),
            rate_limit_floor=500,
            rate_limit_floor_fraction=0.1,
        )
        # limit=1000 -> 10% = 100, capped below the fixed floor of 500.
        assert collector.effective_floor({"limit": 1000, "remaining": 50}) == 100
        assert collector.floor_reached({"limit": 1000, "remaining": 50}) is True
        assert collector.floor_reached({"limit": 1000, "remaining": 200}) is False

    def test_rate_limit_exhausted_graphql_error_marks_rate_limited(self, single_repo_config):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=RATE_LIMITED_ERROR))
        collector = _offline_collector(single_repo_config, transport)

        result = collector.collect()
        assert result.repos["synthtest/repo-a"].status == "rate_limited"
        assert result.issues.num_rows == 0


class TestConfigAndValidation:
    def test_missing_github_issues_repos_raises(self, tmp_path):
        config_path = tmp_path / "no-github-issues.yaml"
        config_path.write_text(
            yaml.safe_dump({"project": {"id": "no-gh", "display_name": "No GH"}})
        )
        config = load_project(config_path)
        with pytest.raises(ValueError, match="github_issues"):
            GitHubIssuesCollector(config)
