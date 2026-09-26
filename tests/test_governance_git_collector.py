"""Tests for project_health.collectors.governance_git (issue #36).

Runs against the deterministic #3 fixture repo
(tests/fixtures/git/build_repo.py, also used by test_git_collector.py) —
never against the network. That fixture already has trunk + two
forward-merged release branches (cassandra-5.0, cassandra-4.1) with two
merge commits, which is exactly the shape this module needs to prove:
merges included, changed paths collected, multi-branch dedup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_health.collectors.governance_git import collect_commits, collect_multi_branch
from tests.fixtures.git.build_repo import build_repo


@pytest.fixture
def built_repo(tmp_path):
    repo = tmp_path / "repo"
    build_repo(repo)
    return repo


def test_walks_every_commit_including_merges(built_repo):
    records = collect_commits(built_repo, "trunk")
    merges = [r for r in records if r.is_merge]
    non_merges = [r for r in records if not r.is_merge]
    assert len(merges) == 2
    assert len(non_merges) == 15  # 13 trunk-authored + 2 branch commits merged in
    assert all(r.branch == "trunk" for r in records)


def test_merge_commit_carries_no_changed_paths(built_repo):
    records = collect_commits(built_repo, "trunk")
    merges = [r for r in records if r.is_merge]
    assert all(r.changed_paths is None for r in merges)


def test_non_merge_commit_carries_changed_paths(built_repo):
    records = collect_commits(built_repo, "trunk")
    backport = next(r for r in records if "Backport fix" in r.message)
    assert backport.changed_paths == ("backport_fix.txt",)


def test_reviewer_trailer_parsed_per_commit(built_repo):
    records = collect_commits(built_repo, "trunk")
    initial = next(r for r in records if r.message.startswith("Initial commit"))
    assert initial.trailer_reviewers == ("Bob Reviewer",)
    assert initial.issue_keys == ("CASSANDRA-100",)


def test_no_trailer_commit_has_empty_reviewers(built_repo):
    records = collect_commits(built_repo, "trunk")
    perf = next(r for r in records if r.message.startswith("Performance optimization"))
    assert perf.trailer_reviewers == ()
    assert perf.issue_keys == ()


def test_committer_and_author_captured(built_repo):
    records = collect_commits(built_repo, "trunk")
    initial = next(r for r in records if r.message.startswith("Initial commit"))
    assert initial.author == "Alice Author"
    assert initial.committer == "Alice Author"
    assert initial.author_email == "alice@cassandra.apache.org"


def test_since_filters_by_commit_date(built_repo):
    all_records = collect_commits(built_repo, "trunk")
    filtered = collect_commits(built_repo, "trunk", since="2024-06-01")
    assert len(filtered) < len(all_records)
    assert all(r.commit_date.date().isoformat() >= "2024-06-01" for r in filtered)


def test_multi_branch_attributes_each_commit_to_exactly_one_branch(built_repo):
    trunk_only = collect_commits(built_repo, "trunk")
    multi = collect_multi_branch(built_repo, ["cassandra-5.0", "cassandra-4.1", "trunk"])
    # Same total commit set as a single trunk walk (trunk is a descendant of
    # both release branches in this fixture) -- multi-branch walking must
    # never duplicate a shared-ancestor commit.
    assert len(multi) == len(trunk_only)
    assert len({r.sha for r in multi}) == len(multi)


def test_multi_branch_labels_commits_by_the_branch_they_were_first_committed_on(built_repo):
    multi = collect_multi_branch(built_repo, ["cassandra-5.0", "cassandra-4.1", "trunk"])
    backport = next(r for r in multi if "Backport fix" in r.message)
    side_feature = next(r for r in multi if r.message.startswith("Feature on cassandra-5.0"))
    # Each side branch's own unique commit is attributed to that branch.
    assert backport.branch == "cassandra-4.1"
    assert side_feature.branch == "cassandra-5.0"
    # In this fixture, `cassandra-5.0` and `cassandra-4.1` are sibling forks
    # of trunk created *after* all 13 shared commits already existed, so
    # whichever branch is listed first claims that shared ancestry (per
    # `collect_multi_branch`'s documented "first branch in the list whose
    # tip reaches it" rule) -- here that's `cassandra-5.0`, since it's
    # listed before `cassandra-4.1`.
    initial = next(r for r in multi if r.message.startswith("Initial commit"))
    assert initial.branch == "cassandra-5.0"
    by_branch: dict[str, int] = {}
    for record in multi:
        by_branch[record.branch] = by_branch.get(record.branch, 0) + 1
    assert by_branch == {"cassandra-5.0": 14, "cassandra-4.1": 2, "trunk": 1}


def test_resolve_ref_raises_for_unknown_branch(built_repo):
    from project_health.collectors.governance_git import resolve_ref

    with pytest.raises(ValueError):
        resolve_ref(Path(built_repo), "does-not-exist")
