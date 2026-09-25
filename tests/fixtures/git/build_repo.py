"""Build a deterministic test fixture git repository."""

import json
import os
import subprocess
from pathlib import Path


def build_repo(repo_path: Path) -> None:
    """
    Build a deterministic git repository with test fixture commits.

    Creates:
    - ≥ 12 non-merge commits across ≥ 3 authors over ≥ 4 distinct months
    - Various trailer formats (CASSANDRA issue references)
    - ≥ 2 merge commits
    - One bot author matching github-actions[bot]@users.noreply.github.com
    - One commit with Co-authored-by trailer
    - Writes expected.json with ground truth
    """
    repo_path = Path(repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)

    # Ensure clean git config for determinism
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test Author"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test Author"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"

    # Initialize repo
    subprocess.run(
        ["git", "init", "-b", "trunk"],
        cwd=repo_path,
        env=env,
        check=True,
        capture_output=True,
    )

    # Configure git to disable signing
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Commits with fixed dates and authors
    # Each commit has metadata for ground truth validation
    commits = [
        # January 2024
        {
            "date": "2024-01-15T10:00:00",
            "subject": "Initial commit",
            "author": ("Alice Author", "alice@cassandra.apache.org"),
            "message": (
                "Initial commit\n\n"
                "Patch by Alice Author; reviewed by Bob Reviewer for CASSANDRA-100"
            ),
            "reviewers": ["Bob Reviewer"],
            "issue_keys": ["CASSANDRA-100"],
            "co_authors": [],
        },
        {
            "date": "2024-01-20T14:30:00",
            "subject": "Add metrics collection",
            "author": ("Bob Reviewer", "bob@cassandra.apache.org"),
            "message": (
                "Add metrics collection\n\n"
                "Patch by Bob Reviewer; reviewed by Alice Author and Charlie "
                "Contributor for CASSANDRA-101"
            ),
            "reviewers": ["Alice Author", "Charlie Contributor"],
            "issue_keys": ["CASSANDRA-101"],
            "co_authors": [],
        },
        # February 2024
        {
            "date": "2024-02-05T09:15:00",
            "subject": "Fix collector bug",
            "author": ("Charlie Contributor", "charlie@cassandra.apache.org"),
            "message": (
                "Fix collector bug\n\n"
                "patch by Charlie Contributor, reviewed by Alice Author for "
                "CASSANDRA-102"
            ),
            "reviewers": ["Alice Author"],
            "issue_keys": ["CASSANDRA-102"],
            "co_authors": [],
        },
        {
            "date": "2024-02-18T16:45:00",
            "subject": "Refactor normalization",
            "author": ("Alice Author", "alice@cassandra.apache.org"),
            "message": (
                "Refactor normalization\n\n"
                "patch by Alice Author; reviewed by Bob Reviewer, Charlie "
                "Contributor for CASSANDRA-103, CASSANDRA-104"
            ),
            "reviewers": ["Bob Reviewer", "Charlie Contributor"],
            "issue_keys": ["CASSANDRA-103", "CASSANDRA-104"],
            "co_authors": [],
        },
        # March 2024
        {
            "date": "2024-03-10T11:20:00",
            "subject": "Automated update",
            "author": (
                "github-actions[bot]",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ),
            "message": "Automated update\n\nGenerated commit by CI bot",
            "reviewers": [],
            "issue_keys": [],
            "co_authors": [],
        },
        {
            "date": "2024-03-22T13:50:00",
            "subject": "Add test coverage",
            "author": ("Bob Reviewer", "bob@cassandra.apache.org"),
            "message": (
                "Add test coverage\n\n"
                "Patch by Bob Reviewer; reviewed by Charlie Contributor for "
                "CASSANDRA-105"
            ),
            "reviewers": ["Charlie Contributor"],
            "issue_keys": ["CASSANDRA-105"],
            "co_authors": [],
        },
        # April 2024
        {
            "date": "2024-04-08T10:30:00",
            "subject": "Document API endpoints",
            "author": ("Charlie Contributor", "charlie@cassandra.apache.org"),
            "message": (
                "Document API endpoints\n\n"
                "Patch by Charlie Contributor; reviewed by Alice Author for "
                "CASSANDRA-106"
            ),
            "reviewers": ["Alice Author"],
            "issue_keys": ["CASSANDRA-106"],
            "co_authors": [],
        },
        {
            "date": "2024-04-25T15:15:00",
            "subject": "Performance optimization",
            "author": ("Alice Author", "alice@cassandra.apache.org"),
            "message": "Performance optimization\n\nNo trailer on this commit",
            "reviewers": [],
            "issue_keys": [],
            "co_authors": [],
        },
        # May 2024
        {
            "date": "2024-05-12T09:45:00",
            "subject": "Schema improvements",
            "author": ("Bob Reviewer", "bob@cassandra.apache.org"),
            "message": (
                "Schema improvements\n\n"
                "No ticket reference in this commit either"
            ),
            "reviewers": [],
            "issue_keys": [],
            "co_authors": [],
        },
        {
            "date": "2024-05-28T14:20:00",
            "subject": "Add validation logic",
            "author": ("Charlie Contributor", "charlie@cassandra.apache.org"),
            "message": (
                "Add validation logic\n\n"
                "Patch by Charlie Contributor; reviewed by Bob Reviewer for "
                "CASSANDRA-107"
            ),
            "reviewers": ["Bob Reviewer"],
            "issue_keys": ["CASSANDRA-107"],
            "co_authors": [],
        },
        # June 2024 (12+ non-merge commits requirement met)
        {
            "date": "2024-06-10T10:00:00",
            "subject": "Extend metrics",
            "author": ("Alice Author", "alice@cassandra.apache.org"),
            "message": (
                "Extend metrics\n\n"
                "Patch by Alice Author; reviewed by Charlie Contributor for "
                "CASSANDRA-108"
            ),
            "reviewers": ["Charlie Contributor"],
            "issue_keys": ["CASSANDRA-108"],
            "co_authors": [],
        },
        {
            "date": "2024-06-20T12:30:00",
            "subject": "Fix edge cases",
            "author": ("Bob Reviewer", "bob@cassandra.apache.org"),
            "message": (
                "Fix edge cases\n\n"
                "Patch by Bob Reviewer; reviewed by Alice Author for "
                "CASSANDRA-109"
            ),
            "reviewers": ["Alice Author"],
            "issue_keys": ["CASSANDRA-109"],
            "co_authors": [],
        },
        # July 2024 - with Co-authored-by trailer
        {
            "date": "2024-07-08T14:00:00",
            "subject": "Implement caching layer",
            "author": ("Alice Author", "alice@cassandra.apache.org"),
            "message": (
                "Implement caching layer\n\n"
                "Patch by Alice Author; reviewed by Dana Dev for CASSANDRA-112\n"
                "Co-authored-by: Dana Dev <dana@example.org>"
            ),
            "reviewers": ["Dana Dev"],
            "issue_keys": ["CASSANDRA-112"],
            "co_authors": ["Dana Dev"],
        },
    ]

    # Create initial commits
    commits_log = []
    for commit_info in commits:
        date_str = commit_info["date"]
        author_name, author_email = commit_info["author"]
        message = commit_info["message"]
        subject = commit_info["subject"]

        # Create a dummy file to commit
        test_file = repo_path / f"file_{len(list(repo_path.glob('file_*'))) + 1}.txt"
        test_file.write_text(f"Content for {subject}\n")

        # Set git env with specific author
        commit_env = env.copy()
        commit_env["GIT_AUTHOR_NAME"] = author_name
        commit_env["GIT_AUTHOR_EMAIL"] = author_email
        commit_env["GIT_COMMITTER_NAME"] = author_name
        commit_env["GIT_COMMITTER_EMAIL"] = author_email
        commit_env["GIT_AUTHOR_DATE"] = date_str
        commit_env["GIT_COMMITTER_DATE"] = date_str

        # Stage and commit
        subprocess.run(
            ["git", "add", str(test_file)],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_path,
            env=commit_env,
            check=True,
            capture_output=True,
        )

        # Extract month
        month = date_str[:7]  # YYYY-MM

        # Track for expected.json
        is_bot = "noreply" in author_email or "bot" in author_name.lower()
        commits_log.append(
            {
                "subject": subject,
                "author_name": author_name,
                "author_email": author_email,
                "month": month,
                "date": date_str,
                "is_merge": False,
                "is_bot": is_bot,
                "reviewers": commit_info["reviewers"],
                "issue_keys": commit_info["issue_keys"],
                "co_authors": commit_info["co_authors"],
            }
        )

    # Create a side branch and merge it (first merge commit)
    merge_env = env.copy()
    merge_env["GIT_AUTHOR_NAME"] = "Alice Author"
    merge_env["GIT_AUTHOR_EMAIL"] = "alice@cassandra.apache.org"
    merge_env["GIT_COMMITTER_NAME"] = "Alice Author"
    merge_env["GIT_COMMITTER_EMAIL"] = "alice@cassandra.apache.org"
    merge_env["GIT_AUTHOR_DATE"] = "2024-07-01T10:00:00"
    merge_env["GIT_COMMITTER_DATE"] = "2024-07-01T10:00:00"

    # Create and commit on side branch
    subprocess.run(
        ["git", "checkout", "-b", "cassandra-5.0"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    branch_commit_env = env.copy()
    branch_commit_env["GIT_AUTHOR_NAME"] = "Bob Reviewer"
    branch_commit_env["GIT_AUTHOR_EMAIL"] = "bob@cassandra.apache.org"
    branch_commit_env["GIT_COMMITTER_NAME"] = "Bob Reviewer"
    branch_commit_env["GIT_COMMITTER_EMAIL"] = "bob@cassandra.apache.org"
    branch_commit_env["GIT_AUTHOR_DATE"] = "2024-07-05T11:00:00"
    branch_commit_env["GIT_COMMITTER_DATE"] = "2024-07-05T11:00:00"

    side_file = repo_path / "side_branch_feature.txt"
    side_file.write_text("Feature on side branch\n")
    subprocess.run(
        ["git", "add", str(side_file)],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            (
                "Feature on cassandra-5.0\n\n"
                "Patch by Bob Reviewer; reviewed by Alice Author for "
                "CASSANDRA-110"
            ),
        ],
        cwd=repo_path,
        env=branch_commit_env,
        check=True,
        capture_output=True,
    )

    # Track side branch commit
    commits_log.append(
        {
            "subject": "Feature on cassandra-5.0",
            "author_name": "Bob Reviewer",
            "author_email": "bob@cassandra.apache.org",
            "month": "2024-07",
            "date": "2024-07-05T11:00:00",
            "is_merge": False,
            "is_bot": False,
            "reviewers": ["Alice Author"],
            "issue_keys": ["CASSANDRA-110"],
            "co_authors": [],
        }
    )

    # Merge back to trunk
    subprocess.run(
        ["git", "checkout", "trunk"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [
            "git",
            "merge",
            "--no-ff",
            "cassandra-5.0",
            "-m",
            "Merge branch 'cassandra-5.0' into trunk",
        ],
        cwd=repo_path,
        env=merge_env,
        check=True,
        capture_output=True,
    )

    # Track merge commit
    commits_log.append(
        {
            "subject": "Merge branch 'cassandra-5.0' into trunk",
            "author_name": "Alice Author",
            "author_email": "alice@cassandra.apache.org",
            "month": "2024-07",
            "is_merge": True,
            "is_bot": False,
            "reviewers": [],
            "issue_keys": [],
            "co_authors": [],
        }
    )

    # Create another side branch and merge (second merge commit)
    merge_env["GIT_AUTHOR_DATE"] = "2024-08-10T10:00:00"
    merge_env["GIT_COMMITTER_DATE"] = "2024-08-10T10:00:00"

    subprocess.run(
        ["git", "checkout", "-b", "cassandra-4.1"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    branch_commit_env["GIT_AUTHOR_NAME"] = "Charlie Contributor"
    branch_commit_env["GIT_AUTHOR_EMAIL"] = "charlie@cassandra.apache.org"
    branch_commit_env["GIT_COMMITTER_NAME"] = "Charlie Contributor"
    branch_commit_env["GIT_COMMITTER_EMAIL"] = "charlie@cassandra.apache.org"
    branch_commit_env["GIT_AUTHOR_DATE"] = "2024-08-05T09:00:00"
    branch_commit_env["GIT_COMMITTER_DATE"] = "2024-08-05T09:00:00"

    backport_file = repo_path / "backport_fix.txt"
    backport_file.write_text("Backport fix\n")
    subprocess.run(
        ["git", "add", str(backport_file)],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            (
                "Backport fix for cassandra-4.1\n\n"
                "Patch by Charlie Contributor; reviewed by Bob Reviewer for "
                "CASSANDRA-111"
            ),
        ],
        cwd=repo_path,
        env=branch_commit_env,
        check=True,
        capture_output=True,
    )

    # Track side branch commit
    commits_log.append(
        {
            "subject": "Backport fix for cassandra-4.1",
            "author_name": "Charlie Contributor",
            "author_email": "charlie@cassandra.apache.org",
            "month": "2024-08",
            "date": "2024-08-05T09:00:00",
            "is_merge": False,
            "is_bot": False,
            "reviewers": ["Bob Reviewer"],
            "issue_keys": ["CASSANDRA-111"],
            "co_authors": [],
        }
    )

    # Merge back to trunk
    subprocess.run(
        ["git", "checkout", "trunk"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [
            "git",
            "merge",
            "--no-ff",
            "cassandra-4.1",
            "-m",
            "Merge branch 'cassandra-4.1' into trunk",
        ],
        cwd=repo_path,
        env=merge_env,
        check=True,
        capture_output=True,
    )

    # Track merge commit
    commits_log.append(
        {
            "subject": "Merge branch 'cassandra-4.1' into trunk",
            "author_name": "Alice Author",
            "author_email": "alice@cassandra.apache.org",
            "month": "2024-08",
            "is_merge": True,
            "is_bot": False,
            "reviewers": [],
            "issue_keys": [],
            "co_authors": [],
        }
    )

    # Generate expected.json with ground truth
    # Note: git log returns commits in reverse chronological order (newest first)
    # The dates in commits are in ISO format YYYY-MM-DDTHH:MM:SS
    non_merge_commits = [c for c in commits_log if not c["is_merge"]]
    # Sort by full date descending (newest first)
    non_merge_commits.sort(
        key=lambda c: c["date"],
        reverse=True,
    )
    expected = {
        "total_commits": len(non_merge_commits),
        "total_merges": sum(1 for c in commits_log if c["is_merge"]),
        "authors": {
            "Alice Author": {
                "email": "alice@cassandra.apache.org",
                "commits": sum(
                    1 for c in commits_log
                    if c["author_name"] == "Alice Author" and not c["is_merge"]
                ),
            },
            "Bob Reviewer": {
                "email": "bob@cassandra.apache.org",
                "commits": sum(
                    1 for c in commits_log
                    if c["author_name"] == "Bob Reviewer" and not c["is_merge"]
                ),
            },
            "Charlie Contributor": {
                "email": "charlie@cassandra.apache.org",
                "commits": sum(
                    1 for c in commits_log
                    if c["author_name"] == "Charlie Contributor" and not c["is_merge"]
                ),
            },
            "github-actions[bot]": {
                "email": "41898282+github-actions[bot]@users.noreply.github.com",
                "commits": sum(
                    1 for c in commits_log
                    if c["author_name"] == "github-actions[bot]" and not c["is_merge"]
                ),
            },
        },
        "months": [
            "2024-01",
            "2024-02",
            "2024-03",
            "2024-04",
            "2024-05",
            "2024-06",
            "2024-07",
            "2024-08",
        ],
        "reviewer_trailers": {
            "CASSANDRA-100": ["Bob Reviewer"],
            "CASSANDRA-101": ["Alice Author", "Charlie Contributor"],
            "CASSANDRA-102": ["Alice Author"],
            "CASSANDRA-103": ["Bob Reviewer", "Charlie Contributor"],
            "CASSANDRA-104": ["Bob Reviewer", "Charlie Contributor"],
            "CASSANDRA-105": ["Charlie Contributor"],
            "CASSANDRA-106": ["Alice Author"],
            "CASSANDRA-107": ["Bob Reviewer"],
            "CASSANDRA-108": ["Charlie Contributor"],
            "CASSANDRA-109": ["Alice Author"],
            "CASSANDRA-110": ["Alice Author"],
            "CASSANDRA-111": ["Bob Reviewer"],
            "CASSANDRA-112": ["Dana Dev"],
        },
        "commits_without_trailer": 2,
        "bot_commits": 1,
        "commits": non_merge_commits,
    }

    # Write to fixtures directory
    expected_file = Path(__file__).parent / "expected.json"
    with open(expected_file, "w") as f:
        json.dump(expected, f, indent=2)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = Path(".test_repo")

    build_repo(path)
    print(f"Repository built at {path}")
