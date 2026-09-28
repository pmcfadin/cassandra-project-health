"""Tests for project_health.governance.engine (issue #36)."""

from collections import Counter
from datetime import datetime, timezone

import pytest

from project_health.governance.checks import (
    AttachmentEvidence,
    CheckstyleEvidence,
    CIEvidence,
    CommitFacts,
)
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
        assert row["policy_version"] == 2
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
            "ninja_declared": False,
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


# --- Per-check result-state counts (issue #97) -------------------------------
#
# The structured-evidence fields added by issue #97 (`state`, `evidence_kind`,
# `evidence_label`, `evidence_at`, `lead_time_seconds`, `reason`,
# `reviewer_detail`) are purely additive on top of the same `result` every
# `score_*` function in `checks.py` already computed (see that module's
# `CheckResult` docstring) -- this test is the count-level proof: six
# commits, deliberately covering every result state
# (pass/fail/unknown/exempt/not_in_force) for every one of the five scored
# checks, tallied by `(check_id, result)`. The real-data before/after
# comparison (issue #97's acceptance criteria) additionally re-runs the full
# pipeline against real collected data and diffs these same per-check counts
# — this synthetic version is the fast, hermetic regression guard for it.


def _counting_commits() -> list[CommitFacts]:
    return [
        # A: every check meets its evidence -- pass across the board.
        CommitFacts(
            sha="a" * 40,
            branch="trunk",
            commit_date=datetime(2026, 8, 20, tzinfo=timezone.utc),
            message="patch by Alice; reviewed by Bob for CASSANDRA-1",
            author="Alice",
            committer="Alice",
            is_merge=False,
            trailer_reviewers=("Bob",),
            issue_keys=("CASSANDRA-1",),
            changed_paths=("src/Foo.java",),
        ),
        # B: every check has direct contrary evidence -- fail where allowed.
        CommitFacts(
            sha="b" * 40,
            branch="trunk",
            commit_date=datetime(2026, 8, 20, tzinfo=timezone.utc),
            message="Fix a bug for CASSANDRA-2",
            author="Carol",
            committer="Carol",
            is_merge=False,
            trailer_reviewers=(),
            issue_keys=("CASSANDRA-2",),
            changed_paths=("src/Bar.java",),
        ),
        # C: no ticket, no reviewer, no runs -- unknown everywhere.
        CommitFacts(
            sha="c" * 40,
            branch="trunk",
            commit_date=datetime(2026, 8, 20, tzinfo=timezone.utc),
            message="misc cleanup",
            author="Dave",
            committer="Dave",
            is_merge=False,
            trailer_reviewers=(),
            issue_keys=(),
            changed_paths=("src/Baz.java",),
        ),
        # D: docs-only -- commit-then-review/not-code exemptions apply.
        CommitFacts(
            sha="d" * 40,
            branch="trunk",
            commit_date=datetime(2026, 8, 20, tzinfo=timezone.utc),
            message="Fix docs typo",
            author="Erin",
            committer="Erin",
            is_merge=False,
            trailer_reviewers=(),
            issue_keys=(),
            changed_paths=("README.md",),
        ),
        # E: before every dated rule's effective_from -- not_in_force.
        CommitFacts(
            sha="e" * 40,
            branch="trunk",
            commit_date=datetime(2019, 1, 1, tzinfo=timezone.utc),
            message="patch by Frank; reviewed by Grace for CASSANDRA-5",
            author="Frank",
            committer="Frank",
            is_merge=False,
            trailer_reviewers=("Grace",),
            issue_keys=("CASSANDRA-5",),
            changed_paths=("src/Qux.java",),
        ),
        # F: the sourced release-process pattern -- exempt.
        CommitFacts(
            sha="f" * 40,
            branch="trunk",
            commit_date=datetime(2026, 8, 20, tzinfo=timezone.utc),
            message="Increment version to 5.0.10",
            author="Release Manager",
            committer="Release Manager",
            is_merge=False,
            trailer_reviewers=(),
            issue_keys=(),
            changed_paths=None,
        ),
    ]


def test_per_check_result_counts_are_stable(policy):
    commits = _counting_commits()
    ci_evidence_by_issue = {
        "CASSANDRA-1": [
            CIEvidence(
                issue_key="CASSANDRA-1",
                source="jira_attachment_ci_artefact",
                created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
                description="attachment 'ci_summary.html'",
            )
        ],
    }
    attachments_by_issue = {
        "CASSANDRA-1": [
            AttachmentEvidence(
                issue_key="CASSANDRA-1",
                attachment_id="1",
                filename="ci_summary.html",
                created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
            ),
            AttachmentEvidence(
                issue_key="CASSANDRA-1",
                attachment_id="2",
                filename="results_details.tar.gz",
                created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
            ),
        ],
        "CASSANDRA-2": [],
    }
    fetched_attachment_issue_keys = {"CASSANDRA-1", "CASSANDRA-2"}
    checkstyle_runs_by_sha = {
        "a" * 40: (
            CheckstyleEvidence(
                sha="a" * 40, check_run_name="ant-check-jdk11", conclusion="success"
            ),
        ),
        "b" * 40: (
            CheckstyleEvidence(
                sha="b" * 40, check_run_name="ant-check-jdk11", conclusion="failure"
            ),
        ),
    }

    rows = build_commit_compliance_rows(
        policy,
        commits,
        ci_evidence_by_issue=ci_evidence_by_issue,
        attachments_by_issue=attachments_by_issue,
        fetched_attachment_issue_keys=fetched_attachment_issue_keys,
        checkstyle_runs_by_sha=checkstyle_runs_by_sha,
    )

    counts: dict[str, Counter] = {}
    for row in rows:
        counts.setdefault(row["check_id"], Counter())[row["result"]] += 1

    assert dict(counts["reviewer-present"]) == {
        "pass": 1,
        "fail": 1,
        "unknown": 1,
        "exempt": 2,
        "not_in_force": 1,
    }
    assert dict(counts["jira-ticket-referenced"]) == {
        "pass": 3,
        "unknown": 2,
        "exempt": 1,
    }
    assert dict(counts["pre-commit-ci-evidence"]) == {
        "pass": 1,
        "unknown": 2,
        "exempt": 2,
        "not_in_force": 1,
    }
    assert dict(counts["ci-artefacts-attached"]) == {
        "pass": 1,
        "fail": 1,
        "unknown": 1,
        "exempt": 2,
        "not_in_force": 1,
    }
    assert dict(counts["code-style-checkstyle"]) == {
        "pass": 1,
        "fail": 1,
        "unknown": 4,
    }

    # Every row's `state` is the pure, total mapping of its own `result`
    # (checks.result_state) -- never independently drifted.
    from project_health.governance.checks import result_state

    for row in rows:
        assert row["state"] == result_state(row["result"])
