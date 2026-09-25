"""Tests for project_health.collectors.git (issue #4).

All tests run against the deterministic #3 fixture repo
(tests/fixtures/git/build_repo.py) built fresh into `tmp_path` — never
against a network clone — so the suite has no network dependency.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from project_health.collectors.git import GitCollector
from project_health.config import load_project
from project_health.schema import validate
from tests.fixtures.git.build_repo import build_repo

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "git"
REPO_LABEL = "apache/cassandra"


@pytest.fixture
def built_repo(tmp_path):
    repo = tmp_path / "repo"
    build_repo(repo)
    return repo


@pytest.fixture
def expected():
    import json

    with open(FIXTURES_DIR / "expected.json") as f:
        return json.load(f)


@pytest.fixture
def cassandra_config():
    return load_project("projects/cassandra.yaml")


def _sha_to_subject(repo_path: Path) -> dict[str, str]:
    """Map every non-merge commit's SHA to its subject line."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--no-merges", "--format=%H\x1f%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    mapping = {}
    for line in result.stdout.strip("\n").split("\n"):
        if not line:
            continue
        sha, subject = line.split("\x1f", 1)
        mapping[sha] = subject
    return mapping


def _add_commit(
    repo_path: Path,
    *,
    author_name: str,
    author_email: str,
    message: str,
    filename: str,
) -> str:
    """Add one more commit on the currently checked-out branch; return its SHA."""
    file_path = repo_path / filename
    file_path.write_text(f"content for {filename}\n")
    subprocess.run(
        ["git", "-C", str(repo_path), "add", filename], check=True, capture_output=True
    )
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", message],
        env=env,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class TestGitCollectorAgainstFixture:
    """Acceptance criterion #1: emitted rows exactly match expected.json."""

    def _collect(self, built_repo, cassandra_config, watermark=None, snapshot_id="snap-1"):
        return GitCollector().collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=watermark,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id=snapshot_id,
        )

    def test_commit_count_excludes_merges_and_bots(self, built_repo, expected, cassandra_config):
        result = self._collect(built_repo, cassandra_config)

        expected_non_bot = expected["total_commits"] - expected["bot_commits"]
        assert result.commits_collected == expected_non_bot
        assert result.contribution_event.num_rows == expected_non_bot
        assert result.bot_commits_excluded == expected["bot_commits"]

    def test_contribution_event_rows_match_expected_commits(
        self, built_repo, expected, cassandra_config
    ):
        result = self._collect(built_repo, cassandra_config)
        sha_to_subject = _sha_to_subject(built_repo)
        by_subject = {c["subject"]: c for c in expected["commits"]}

        rows = result.contribution_event.to_pylist()
        assert len(rows) == len([c for c in expected["commits"] if not c["is_bot"]])

        for row in rows:
            subject = sha_to_subject[row["source_ref"]]
            exp = by_subject[subject]
            assert exp["is_bot"] is False
            assert row["author_display_name"] == exp["author_name"]
            assert row["author_raw_type"] == "git_email"
            assert row["author_raw_value"] == exp["author_email"].lower()
            assert row["event_type"] == "code_commit"
            assert row["repo"] == REPO_LABEL
            assert row["identity_id"] is None
            assert row["source_snapshot_id"] == "snap-1"
            assert row["occurred_at"].strftime("%Y-%m") == exp["month"]

    def test_no_bot_rows_emitted(self, built_repo, expected, cassandra_config):
        result = self._collect(built_repo, cassandra_config)
        bot_email = next(
            info["email"] for name, info in expected["authors"].items() if "bot" in name.lower()
        )
        contribution_emails = {
            row["author_raw_value"] for row in result.contribution_event.to_pylist()
        }
        review_emails = {row["author_raw_value"] for row in result.review_event.to_pylist()}
        assert bot_email.lower() not in contribution_emails
        assert bot_email.lower() not in review_emails

    def test_review_event_reviewer_attributions_match_expected(
        self, built_repo, expected, cassandra_config
    ):
        result = self._collect(built_repo, cassandra_config)

        actual_by_issue: dict[str, set[str]] = {}
        for row in result.review_event.to_pylist():
            actual_by_issue.setdefault(row["issue_key"], set()).add(row["reviewer_raw_value"])

        expected_by_issue = {k: set(v) for k, v in expected["reviewer_trailers"].items()}
        assert actual_by_issue == expected_by_issue

    def test_review_event_row_count_is_reviewer_by_issue_key_cross_product(
        self, built_repo, expected, cassandra_config
    ):
        result = self._collect(built_repo, cassandra_config)
        expected_row_count = sum(len(v) for v in expected["reviewer_trailers"].values())
        assert result.review_event.num_rows == expected_row_count

    def test_review_event_row_shape(self, built_repo, expected, cassandra_config):
        result = self._collect(built_repo, cassandra_config)
        rows = result.review_event.to_pylist()
        assert rows, "expected at least one review_event row from the fixture"
        for row in rows:
            assert row["source"] == "commit_trailer"
            assert row["reviewer_raw_type"] == "git_name"
            assert row["reviewer_identity_id"] is None
            assert row["author_raw_type"] == "git_email"
            assert row["author_identity_id"] is None
            assert row["repo"] == REPO_LABEL
            assert row["source_snapshot_id"] == "snap-1"
            # reviewer names are stored exactly as written (trimmed), never lowercased
            assert row["reviewer_raw_value"] == row["reviewer_raw_value"].strip()

    def test_commits_without_trailer_emit_no_review_rows(
        self, built_repo, expected, cassandra_config
    ):
        result = self._collect(built_repo, cassandra_config)
        subject_to_sha = {v: k for k, v in _sha_to_subject(built_repo).items()}
        no_trailer_shas = {
            subject_to_sha[c["subject"]] for c in expected["commits"] if not c["reviewers"]
        }
        review_shas = {row["event_id"].split(":")[2] for row in result.review_event.to_pylist()}
        assert no_trailer_shas.isdisjoint(review_shas)

    def test_rows_validate_against_schema(self, built_repo, cassandra_config):
        result = self._collect(built_repo, cassandra_config)
        # validate() raises SchemaValidationError on any mismatch; a clean
        # return is the assertion.
        validate("contribution_event", result.contribution_event)
        validate("review_event", result.review_event)

    def test_no_unparsed_reviewed_by_in_fixture(self, built_repo, cassandra_config):
        # Every fixture commit's trailer is designed to parse cleanly.
        result = self._collect(built_repo, cassandra_config)
        assert result.unparsed_reviewed_by_count == 0


class TestGitCollectorIncremental:
    """Acceptance criterion #2: a second collect from the returned watermark
    emits only commits added after it."""

    def test_second_collect_only_returns_new_commit(self, built_repo, cassandra_config):
        collector = GitCollector()
        first = collector.collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=None,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id="snap-1",
        )
        assert first.commits_collected > 0
        assert first.next_watermark == collector.next_watermark(first)

        new_sha = _add_commit(
            built_repo,
            author_name="Eve New",
            author_email="eve@cassandra.apache.org",
            message=(
                "Add a brand new feature\n\n"
                "Patch by Eve New; reviewed by Alice Author for CASSANDRA-200"
            ),
            filename="new_feature.txt",
        )

        second = collector.collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=first.next_watermark,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id="snap-2",
        )

        assert second.commits_collected == 1
        assert second.contribution_event.num_rows == 1
        row = second.contribution_event.to_pylist()[0]
        assert row["source_ref"] == new_sha
        assert row["author_raw_value"] == "eve@cassandra.apache.org"
        assert second.next_watermark == new_sha

        review_rows = second.review_event.to_pylist()
        assert len(review_rows) == 1
        assert review_rows[0]["reviewer_raw_value"] == "Alice Author"
        assert review_rows[0]["issue_key"] == "CASSANDRA-200"

    def test_second_collect_with_no_new_commits_is_empty(self, built_repo, cassandra_config):
        collector = GitCollector()
        first = collector.collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=None,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id="snap-1",
        )

        second = collector.collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=first.next_watermark,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id="snap-2",
        )

        assert second.commits_collected == 0
        assert second.contribution_event.num_rows == 0
        assert second.review_event.num_rows == 0
        assert second.next_watermark == first.next_watermark


class TestPlaceholderReviewerCounts:
    """Test counting of commits with placeholder reviewers (issue #18)."""

    def _collect(self, built_repo, cassandra_config, watermark=None, snapshot_id="snap-1"):
        return GitCollector().collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=watermark,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id=snapshot_id,
        )

    def test_placeholder_only_commits_have_count_but_no_review_rows(
        self, built_repo, cassandra_config
    ):
        collector = GitCollector()
        first = collector.collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=None,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id="snap-1",
        )

        # Add a commit with only placeholder reviewers
        _add_commit(
            built_repo,
            author_name="Test Author",
            author_email="test@example.com",
            message="patch by Test Author; reviewed by TBD for CASSANDRA-999",
            filename="placeholder_test.txt",
        )

        second = collector.collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=first.next_watermark,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id="snap-2",
        )

        assert second.commits_collected == 1
        assert second.placeholder_reviewer_commits == 1
        # No review rows should be emitted for placeholder-only reviewers
        assert second.review_event.num_rows == 0

    def test_mixed_reviewers_commit_counted_and_has_review_rows(
        self, built_repo, cassandra_config
    ):
        collector = GitCollector()
        first = collector.collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=None,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id="snap-1",
        )

        # Add a commit with mixed reviewers (real + placeholder)
        _add_commit(
            built_repo,
            author_name="Test Author",
            author_email="test@example.com",
            message="patch by Test Author; reviewed by Bob Author and TBD for CASSANDRA-888",
            filename="mixed_test.txt",
        )

        second = collector.collect(
            repo_path=built_repo,
            repo_label=REPO_LABEL,
            default_branch="trunk",
            watermark=first.next_watermark,
            bot_patterns=cassandra_config.bot_patterns,
            source_snapshot_id="snap-2",
        )

        assert second.commits_collected == 1
        assert second.placeholder_reviewer_commits == 1
        # Review row should be emitted only for non-placeholder reviewers
        assert second.review_event.num_rows == 1
        review_row = second.review_event.to_pylist()[0]
        assert review_row["reviewer_raw_value"] == "Bob Author"
        assert review_row["issue_key"] == "CASSANDRA-888"
