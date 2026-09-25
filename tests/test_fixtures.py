"""Tests for test fixtures."""

import json
import re
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir():
    """Return the fixtures directory."""
    return Path(__file__).parent / "fixtures"


class TestGitRepoFixture:
    """Test the deterministic git repository fixture."""

    def test_build_repo_is_deterministic(self, tmp_path):
        """Test that build_repo produces identical repos on two runs."""
        from tests.fixtures.git.build_repo import build_repo

        # Build repo twice
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"

        build_repo(repo1)
        build_repo(repo2)

        # Get the HEAD commit hash from each repo
        result1 = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo1,
            capture_output=True,
            text=True,
            check=True,
        )
        result2 = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo2,
            capture_output=True,
            text=True,
            check=True,
        )

        hash1 = result1.stdout.strip()
        hash2 = result2.stdout.strip()

        assert hash1 == hash2, f"Repos are not deterministic: {hash1} != {hash2}"

    def test_expected_json_matches_git_log(self, tmp_path):
        """Test that expected.json matches actual git log output."""
        from tests.fixtures.git.build_repo import build_repo

        repo = tmp_path / "repo"
        build_repo(repo)

        # Read expected.json
        expected_file = Path(__file__).parent / "fixtures" / "git" / "expected.json"
        assert expected_file.exists(), f"expected.json not found at {expected_file}"

        with open(expected_file) as f:
            expected = json.load(f)

        # Count non-merge commits
        result = subprocess.run(
            ["git", "log", "--no-merges", "--format=%H"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        commit_hashes = [h.strip() for h in result.stdout.strip().split("\n") if h.strip()]
        actual_commit_count = len(commit_hashes)

        assert actual_commit_count == expected["total_commits"], (
            f"Expected {expected['total_commits']} commits, "
            f"got {actual_commit_count}"
        )

        # Count merge commits
        result = subprocess.run(
            ["git", "log", "--oneline", "--all"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().split("\n")
        # A merge commit has more than one parent
        merge_count = 0
        for line in lines:
            result = subprocess.run(
                ["git", "rev-list", "--parents", "-n", "1", line.split()[0]],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            parents = result.stdout.strip().split()
            if len(parents) > 2:  # First is commit hash, rest are parents
                merge_count += 1

        assert merge_count == expected["total_merges"], (
            f"Expected {expected['total_merges']} merge commits, "
            f"got {merge_count}"
        )

        # Verify authors
        result = subprocess.run(
            ["git", "log", "--no-merges", "--format=%an%n%ae"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )

        # Parse author data
        lines = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        authors_found = {}
        for i in range(0, len(lines), 2):
            if i + 1 < len(lines):
                name = lines[i]
                email = lines[i + 1]
                if name not in authors_found:
                    authors_found[name] = {"email": email, "count": 0}
                authors_found[name]["count"] += 1

        # Verify expected authors exist
        for author_name, author_info in expected["authors"].items():
            assert author_name in authors_found, f"Author {author_name} not found in commits"
            assert authors_found[author_name]["count"] == author_info["commits"], (
                f"Author {author_name} has {authors_found[author_name]['count']} commits, "
                f"expected {author_info['commits']}"
            )

    def test_repo_has_multiple_authors(self, tmp_path):
        """Test that the repo has >= 3 authors."""
        from tests.fixtures.git.build_repo import build_repo

        repo = tmp_path / "repo"
        build_repo(repo)

        result = subprocess.run(
            ["git", "log", "--format=%an"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )

        authors = set(line.strip() for line in result.stdout.strip().split("\n") if line.strip())
        # Filter out merge commits which might have duplicates
        result_no_merge = subprocess.run(
            ["git", "log", "--no-merges", "--format=%an"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        authors = set(
            line.strip()
            for line in result_no_merge.stdout.strip().split("\n")
            if line.strip()
        )

        assert len(authors) >= 3, f"Expected >= 3 authors, got {len(authors)}: {authors}"

    def test_repo_has_bot_author(self, tmp_path):
        """Test that the repo has a bot author with noreply email."""
        from tests.fixtures.git.build_repo import build_repo

        repo = tmp_path / "repo"
        build_repo(repo)

        result = subprocess.run(
            ["git", "log", "--format=%ae"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )

        emails = result.stdout.strip().split("\n")
        bot_emails = [e for e in emails if "noreply" in e.lower()]

        assert len(bot_emails) >= 1, "Expected at least one bot author with noreply in email"

    def test_repo_has_merge_commits(self, tmp_path):
        """Test that the repo has >= 2 merge commits."""
        from tests.fixtures.git.build_repo import build_repo

        repo = tmp_path / "repo"
        build_repo(repo)

        # Count merge commits
        result = subprocess.run(
            ["git", "log", "--oneline", "--all"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().split("\n")
        merge_count = 0
        for line in lines:
            result = subprocess.run(
                ["git", "rev-list", "--parents", "-n", "1", line.split()[0]],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            parents = result.stdout.strip().split()
            if len(parents) > 2:  # First is commit hash, rest are parents
                merge_count += 1

        assert merge_count >= 2, f"Expected >= 2 merge commits, got {merge_count}"

    def test_commits_have_cassandra_trailers(self, tmp_path):
        """Test that commits have CASSANDRA issue trailers."""
        from tests.fixtures.git.build_repo import build_repo

        repo = tmp_path / "repo"
        build_repo(repo)

        result = subprocess.run(
            ["git", "log", "--no-merges", "--format=%B"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )

        messages = result.stdout.strip()

        # Look for CASSANDRA issue references
        cassandra_issues = re.findall(r"CASSANDRA-\d+", messages)

        assert len(cassandra_issues) >= 10, (
            f"Expected at least 10 CASSANDRA issue references in commits, "
            f"got {len(cassandra_issues)}"
        )

        # Look for reviewer trailers
        reviewer_pattern = r"reviewed by ([^;]+)"
        reviewers = re.findall(reviewer_pattern, messages, re.IGNORECASE)

        assert len(reviewers) >= 8, (
            f"Expected at least 8 commits with reviewer trailers, "
            f"got {len(reviewers)}"
        )

    def test_expected_json_has_commits_array(self, fixtures_dir):
        """Test that expected.json includes a commits array with full metadata."""
        expected_file = fixtures_dir / "git" / "expected.json"
        assert expected_file.exists(), f"expected.json not found at {expected_file}"

        with open(expected_file) as f:
            expected = json.load(f)

        assert "commits" in expected, "expected.json missing 'commits' array"
        assert isinstance(expected["commits"], list), "'commits' should be a list"
        assert len(expected["commits"]) > 0, "commits array should not be empty"

        # Verify each commit has required fields
        required_fields = {
            "subject",
            "author_name",
            "author_email",
            "month",
            "is_merge",
            "is_bot",
            "reviewers",
            "issue_keys",
            "co_authors",
        }

        for i, commit in enumerate(expected["commits"]):
            assert isinstance(commit, dict), f"Commit {i} should be a dict"
            missing = required_fields - set(commit.keys())
            assert not missing, (
                f"Commit {i} missing fields: {missing}"
            )

    def test_commits_array_matches_git_log(self, tmp_path):
        """Test that commits array in expected.json matches actual git log."""
        from tests.fixtures.git.build_repo import build_repo

        repo = tmp_path / "repo"
        build_repo(repo)

        # Read expected.json
        expected_file = Path(__file__).parent / "fixtures" / "git" / "expected.json"
        with open(expected_file) as f:
            expected = json.load(f)

        expected_commits = expected["commits"]

        # Get actual commits from git log
        result = subprocess.run(
            ["git", "log", "--no-merges", "--format=%s%n%an%n%ae"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )

        lines = [
            line.strip()
            for line in result.stdout.strip().split("\n")
            if line.strip()
        ]

        # Parse git log output (groups of 3: subject, author_name, author_email)
        git_commits = []
        for i in range(0, len(lines), 3):
            if i + 2 < len(lines):
                git_commits.append(
                    {
                        "subject": lines[i],
                        "author_name": lines[i + 1],
                        "author_email": lines[i + 2],
                    }
                )

        # Verify count matches
        assert len(git_commits) == len(expected_commits), (
            f"Expected {len(expected_commits)} commits, "
            f"got {len(git_commits)}"
        )

        # Verify each commit matches
        for i, (git_commit, expected_commit) in enumerate(
            zip(git_commits, expected_commits)
        ):
            assert git_commit["subject"] == expected_commit["subject"], (
                f"Commit {i} subject mismatch: "
                f"{git_commit['subject']} != {expected_commit['subject']}"
            )
            assert git_commit["author_name"] == expected_commit["author_name"], (
                f"Commit {i} author name mismatch: "
                f"{git_commit['author_name']} != {expected_commit['author_name']}"
            )
            assert git_commit["author_email"] == expected_commit["author_email"], (
                f"Commit {i} author email mismatch: "
                f"{git_commit['author_email']} != {expected_commit['author_email']}"
            )

    def test_co_authored_by_trailer_exists(self, tmp_path):
        """Test that at least one commit has a Co-authored-by trailer."""
        from tests.fixtures.git.build_repo import build_repo

        repo = tmp_path / "repo"
        build_repo(repo)

        result = subprocess.run(
            ["git", "log", "--no-merges", "--format=%B"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )

        messages = result.stdout.strip()

        # Look for Co-authored-by trailer
        assert "Co-authored-by:" in messages, (
            "Expected at least one commit with Co-authored-by trailer"
        )

    def test_bot_email_is_realistic(self, fixtures_dir):
        """Test that bot email is realistic github-actions format."""
        expected_file = fixtures_dir / "git" / "expected.json"
        with open(expected_file) as f:
            expected = json.load(f)

        # Find bot author
        bot_found = False
        for author_name, author_info in expected["authors"].items():
            if "bot" in author_name.lower():
                bot_found = True
                email = author_info["email"]
                # Verify it's the realistic github-actions[bot] format
                assert email == "41898282+github-actions[bot]@users.noreply.github.com", (
                    f"Bot email should be realistic format, got {email}"
                )
                break

        assert bot_found, "No bot author found in expected.json"


class TestJiraFixtures:
    """Test JIRA fixture files."""

    def test_jira_fixtures_are_valid_json(self, fixtures_dir):
        """Test that all JIRA fixtures are valid JSON."""
        jira_dir = fixtures_dir / "jira"
        json_files = list(jira_dir.glob("*.json"))

        assert len(json_files) >= 3, f"Expected at least 3 JIRA JSON files, got {len(json_files)}"

        for json_file in json_files:
            with open(json_file) as f:
                data = json.load(f)
                assert "issues" in data, f"No 'issues' key in {json_file}"
                assert isinstance(data["issues"], list), f"'issues' is not a list in {json_file}"

    def test_jira_fixtures_have_reviewer_field(self, fixtures_dir):
        """Test that JIRA fixtures contain reviewer field data."""
        jira_dir = fixtures_dir / "jira"

        # Load the file with reviewer data
        reviewer_file = jira_dir / "search_with_reviewers.json"
        assert reviewer_file.exists(), f"File not found: {reviewer_file}"

        with open(reviewer_file) as f:
            data = json.load(f)

        # Count issues with reviewer field populated
        issues_with_reviewers = 0
        for issue in data["issues"]:
            if (
                issue.get("fields", {}).get("customfield_12313420")
                and len(issue["fields"]["customfield_12313420"]) > 0
            ):
                issues_with_reviewers += 1

        assert issues_with_reviewers >= 2, (
            f"Expected at least 2 issues with reviewer field populated, "
            f"got {issues_with_reviewers}"
        )

    def test_jira_fixtures_have_unresolved_issue(self, fixtures_dir):
        """Test that unresolved issues fixture contains unresolved issues."""
        jira_dir = fixtures_dir / "jira"

        unresolved_file = jira_dir / "search_unresolved.json"
        assert unresolved_file.exists(), f"File not found: {unresolved_file}"

        with open(unresolved_file) as f:
            data = json.load(f)

        # All issues should have unresolved status
        for issue in data["issues"]:
            # At least check that resolutiondate is null for unresolved
            assert (
                issue.get("fields", {}).get("resolutiondate") is None
            ), f"Issue {issue['key']} appears to be resolved but in unresolved query"

    def test_jira_fixtures_second_page(self, fixtures_dir):
        """Test that second page fixture has correct startAt value."""
        jira_dir = fixtures_dir / "jira"

        page2_file = jira_dir / "search_with_reviewers_page2.json"
        assert page2_file.exists(), f"File not found: {page2_file}"

        with open(page2_file) as f:
            data = json.load(f)

        assert data.get("startAt") == 5, f"Expected startAt=5 for page 2, got {data.get('startAt')}"

    def test_jira_fixtures_readme_exists(self, fixtures_dir):
        """Test that JIRA fixtures have a README."""
        jira_dir = fixtures_dir / "jira"
        readme_file = jira_dir / "README.md"

        assert readme_file.exists(), f"README.md not found in {jira_dir}"

        with open(readme_file) as f:
            content = f.read()

        # Verify README contains URLs and dates
        assert "https://issues.apache.org/jira/rest/api/2/search" in content, (
            "README should document the API URL"
        )
        assert "2026-09-25" in content, "README should document the recording date"
