"""Tests for project_health.governance.engine (issue #36)."""

from datetime import datetime, timezone

import pytest

from project_health.governance.checks import CommitFacts
from project_health.governance.engine import (
    SCORED_CHECK_IDS,
    build_commit_compliance_rows,
    build_commit_facts_rows,
    score_commit,
)
from project_health.governance.overrides import Override
from project_health.governance.policy import DEFAULT_POLICY_PATH, load_policy


@pytest.fixture(scope="module")
def policy():
    return load_policy(DEFAULT_POLICY_PATH)


def _commit(**overrides) -> CommitFacts:
    defaults = dict(
        sha="a" * 40,
        branch="trunk",
        commit_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
        message="patch by Alice; reviewed by Bob for CASSANDRA-100",
        author="Alice",
        committer="Alice",
        is_merge=False,
        trailer_reviewers=("Bob",),
        issue_keys=("CASSANDRA-100",),
        changed_paths=None,
    )
    defaults.update(overrides)
    return CommitFacts(**defaults)


def test_score_commit_returns_all_four_scored_checks(policy):
    results = score_commit(policy, _commit())
    check_ids = {r.check_id for r in results}
    assert check_ids == set(SCORED_CHECK_IDS)


def test_score_commit_omits_checkstyle_on_pre_4_1_branch(policy):
    results = score_commit(policy, _commit(branch="cassandra-4.0"))
    check_ids = {r.check_id for r in results}
    assert "code-style-checkstyle" not in check_ids
    assert "reviewer-present" in check_ids


def test_build_commit_compliance_rows_shape(policy):
    rows = build_commit_compliance_rows(policy, [_commit()])
    assert len(rows) == len(SCORED_CHECK_IDS)
    for row in rows:
        assert row["sha"] == "a" * 40
        assert row["branch"] == "trunk"
        assert row["policy_version"] == 1
        assert row["reviewers"] == ["Bob"]
        assert row["jira_keys"] == ["CASSANDRA-100"]
        assert row["is_merge"] is False


def test_build_commit_compliance_rows_unions_trailer_and_jira_reviewers(policy):
    commit = _commit(trailer_reviewers=("Bob",))
    rows = build_commit_compliance_rows(
        policy, [commit], jira_reviewers_by_issue={"CASSANDRA-100": ("Carol",)}
    )
    reviewer_row = next(r for r in rows if r["check_id"] == "reviewer-present")
    assert reviewer_row["reviewers"] == ["Bob", "Carol"]
    assert reviewer_row["result"] == "pass"


def test_overrides_applied_last_and_recorded_in_evidence(policy):
    commit = _commit(trailer_reviewers=(), message="Fix a bug for CASSANDRA-100")
    override = Override(
        sha=commit.sha,
        check_id="reviewer-present",
        result="pass",
        reason="Confirmed reviewed out-of-band on the mailing list.",
        reviewer="pmcfadin",
    )
    rows = build_commit_compliance_rows(policy, [commit], overrides=[override])
    reviewer_row = next(r for r in rows if r["check_id"] == "reviewer-present")
    assert reviewer_row["result"] == "pass"
    assert "OVERRIDDEN by pmcfadin" in reviewer_row["evidence"]
    assert "fail" in reviewer_row["evidence"]  # records the original result


def test_overrides_only_affect_the_targeted_check(policy):
    commit = _commit()
    override = Override(
        sha=commit.sha,
        check_id="jira-ticket-referenced",
        result="unknown",
        reason="test",
        reviewer="pmcfadin",
    )
    rows = build_commit_compliance_rows(policy, [commit], overrides=[override])
    reviewer_row = next(r for r in rows if r["check_id"] == "reviewer-present")
    jira_row = next(r for r in rows if r["check_id"] == "jira-ticket-referenced")
    assert reviewer_row["result"] == "pass"  # untouched
    assert jira_row["result"] == "unknown"  # overridden from "pass"


def test_build_commit_facts_rows():
    commit = _commit(changed_paths=("CHANGES.txt",))
    rows = build_commit_facts_rows([commit])
    assert rows == [
        {
            "sha": commit.sha,
            "branch": "trunk",
            "commit_date": commit.commit_date,
            "changes_txt_touched": True,
            "news_txt_touched": False,
            "test_touched": False,
        }
    ]


def test_merge_commit_with_real_trailer_is_still_scored(policy):
    """GOVERNANCE.md §3: per-commit checks run on every commit, merges
    included, when a merge carries a real reviewer trailer."""
    commit = _commit(is_merge=True)
    rows = build_commit_compliance_rows(policy, [commit])
    reviewer_row = next(r for r in rows if r["check_id"] == "reviewer-present")
    assert reviewer_row["result"] == "pass"
    assert reviewer_row["is_merge"] is True
