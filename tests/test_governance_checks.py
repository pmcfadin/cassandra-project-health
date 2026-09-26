"""Tests for project_health.governance.checks (issue #36).

Runs against the real repo-root `governance-policy.yaml` via `load_policy`
so the scoring logic is proven against the actual binding policy, not a
simplified fixture copy of it.
"""

from datetime import datetime, timezone

import pytest

from project_health.governance.checks import (
    CheckstyleEvidence,
    CIEvidence,
    CommitFacts,
    build_commit_facts_row,
    score_code_style_checkstyle,
    score_jira_ticket_referenced,
    score_pre_commit_ci_evidence,
    score_reviewer_present,
)
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


class TestReviewerPresent:
    def test_pass_from_trailer(self, policy):
        rule = policy.rule("reviewer-present")
        result = score_reviewer_present(rule, _commit())
        assert result.result == "pass"
        assert "Bob" in result.evidence

    def test_pass_from_jira_field_only(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(trailer_reviewers=())
        result = score_reviewer_present(rule, commit, jira_reviewers=("carol",))
        assert result.result == "pass"
        assert "carol" in result.evidence

    def test_fail_when_issue_key_and_no_reviewer_and_no_review_wording(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(
            trailer_reviewers=(),
            message="Fix a bug for CASSANDRA-100",
            issue_keys=("CASSANDRA-100",),
        )
        result = score_reviewer_present(rule, commit)
        assert result.result == "fail"

    def test_unknown_when_review_wording_present_but_unparsed(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(
            trailer_reviewers=(),
            message="Author: X; Reviewed by Y - no ticket for CASSANDRA-100",
            issue_keys=("CASSANDRA-100",),
        )
        result = score_reviewer_present(rule, commit)
        assert result.result == "unknown"
        assert "unparsed" in result.evidence

    def test_unknown_when_no_issue_key_and_no_reviewer(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(trailer_reviewers=(), issue_keys=(), message="Docs typo fix")
        result = score_reviewer_present(rule, commit)
        assert result.result == "unknown"

    def test_exempt_ninja_never_fails_even_with_issue_key_and_no_reviewer(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(
            trailer_reviewers=(), message="ninjafix quick change for CASSANDRA-100"
        )
        result = score_reviewer_present(rule, commit)
        assert result.result == "exempt"
        assert "ninja" in result.evidence

    def test_exempt_release_housekeeping(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(trailer_reviewers=(), issue_keys=(), message="increment to version 5.0.10")
        result = score_reviewer_present(rule, commit)
        assert result.result == "exempt"

    def test_not_in_force_before_2020_06_25(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(
            commit_date=datetime(2015, 1, 1, tzinfo=timezone.utc), trailer_reviewers=()
        )
        result = score_reviewer_present(rule, commit)
        assert result.result == "not_in_force"

    def test_none_when_branch_not_in_scope(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(branch="some-feature-branch")
        assert score_reviewer_present(rule, commit) is None

    def test_cep_only_key_is_unknown_not_fail(self, policy):
        """The fail condition is anchored to "references a CASSANDRA-N
        issue key" (governance-policy.yaml) -- a commit that references
        only a CEP-N key (found live on trunk, e.g. "Release notes and
        README updates for CEP-7") must not be scored `fail` just because
        a project-prefixed key of some kind is present."""
        rule = policy.rule("reviewer-present")
        commit = _commit(
            trailer_reviewers=(),
            message="Release notes and README updates for CEP-7 (Storage-Attached Indexes)",
            issue_keys=("CEP-7",),
        )
        result = score_reviewer_present(rule, commit)
        assert result.result == "unknown"

    def test_cassandra_and_cep_key_together_still_fails(self, policy):
        rule = policy.rule("reviewer-present")
        commit = _commit(
            trailer_reviewers=(),
            message="CEP-7 follow-up for CASSANDRA-100",
            issue_keys=("CEP-7", "CASSANDRA-100"),
        )
        result = score_reviewer_present(rule, commit)
        assert result.result == "fail"

    def test_regression_shas_now_pass(self, policy):
        """The two regression shas (governance-policy.yaml
        review_wording_check.regression_examples) must now parse as `pass`
        via the extended reviewer_trailer.py parser (issue #36), not fall
        through to the `unknown` guard."""
        from project_health.collectors.reviewer_trailer import ReviewerExtractor

        rule = policy.rule("reviewer-present")
        extractor = ReviewerExtractor()

        message1 = "patch by Mick Semb Wever; reviewed Štefan Miklošovič for CASSANDRA-21489"
        attribution1 = extractor.extract(message1)
        commit1 = _commit(
            sha="208d87513f658f6fbf82cabcbb04142e7319fa55",
            message=message1,
            trailer_reviewers=attribution1.reviewers,
            issue_keys=attribution1.issue_keys,
        )
        assert score_reviewer_present(rule, commit1).result == "pass"

        message2 = (
            "Authored by Lorina Poland (polandll); Reviewed by Branimir Lambov "
            "(blambov) for CASSANDRA-18236"
        )
        attribution2 = extractor.extract(message2)
        commit2 = _commit(
            sha="05186d786974f3caf0491d5373b648c97c718c4a",
            message=message2,
            trailer_reviewers=attribution2.reviewers,
            issue_keys=attribution2.issue_keys,
        )
        assert score_reviewer_present(rule, commit2).result == "pass"


class TestJiraTicketReferenced:
    def test_pass_when_issue_key_present(self, policy):
        rule = policy.rule("jira-ticket-referenced")
        result = score_jira_ticket_referenced(rule, _commit())
        assert result.result == "pass"

    def test_unknown_when_no_issue_key(self, policy):
        rule = policy.rule("jira-ticket-referenced")
        commit = _commit(issue_keys=(), message="typo fix")
        result = score_jira_ticket_referenced(rule, commit)
        assert result.result == "unknown"

    def test_exempt_release_housekeeping_no_issue_key_needed(self, policy):
        rule = policy.rule("jira-ticket-referenced")
        commit = _commit(issue_keys=(), message="Prepare debian changelog for 3.11.19")
        result = score_jira_ticket_referenced(rule, commit)
        assert result.result == "exempt"

    def test_never_fails(self, policy):
        rule = policy.rule("jira-ticket-referenced")
        commit = _commit(issue_keys=(), trailer_reviewers=(), message="nothing here")
        result = score_jira_ticket_referenced(rule, commit)
        assert result.result != "fail"


class TestPreCommitCiEvidence:
    def test_pass_when_ci_comment_evidence_found(self, policy):
        rule = policy.rule("pre-commit-ci-evidence")
        evidence = {
            "CASSANDRA-100": CIEvidence(
                issue_key="CASSANDRA-100",
                comment_id="123",
                comment_author="alice",
                comment_created_at="2024-06-01T00:00:00.000+0000",
                matched_term="circleci",
                matched_url="https://circleci.com/build/1",
            )
        }
        result = score_pre_commit_ci_evidence(rule, _commit(), evidence)
        assert result.result == "pass"
        assert result.evidence_url == "https://circleci.com/build/1"

    def test_unknown_when_no_ci_evidence(self, policy):
        rule = policy.rule("pre-commit-ci-evidence")
        result = score_pre_commit_ci_evidence(rule, _commit(), {})
        assert result.result == "unknown"

    def test_never_fails(self, policy):
        rule = policy.rule("pre-commit-ci-evidence")
        result = score_pre_commit_ci_evidence(rule, _commit(), {})
        assert result.result != "fail"

    def test_github_check_run_evidence_is_never_used_for_scoring(self, policy):
        """Documented policy limitation: GitHub check-runs are NOT a
        substitute for JIRA-comment CI evidence on this rule -- passing no
        ci_evidence_by_issue must stay unknown even though this commit
        "looks like" it has CI (that's code-style-checkstyle's job)."""
        rule = policy.rule("pre-commit-ci-evidence")
        result = score_pre_commit_ci_evidence(rule, _commit(), None)
        assert result.result == "unknown"


class TestCodeStyleCheckstyle:
    def test_pass_when_all_runs_succeed(self, policy):
        rule = policy.rule("code-style-checkstyle")
        runs = (
            CheckstyleEvidence(
                sha="a" * 40, check_run_name="ant-check-jdk11", conclusion="success"
            ),
            CheckstyleEvidence(
                sha="a" * 40, check_run_name="ant-check-jdk17", conclusion="success"
            ),
        )
        result = score_code_style_checkstyle(rule, _commit(), runs)
        assert result.result == "pass"

    def test_fail_when_a_run_failed(self, policy):
        rule = policy.rule("code-style-checkstyle")
        runs = (
            CheckstyleEvidence(
                sha="a" * 40,
                check_run_name="ant-check-jdk11",
                conclusion="failure",
                html_url="https://github.com/x/y/runs/1",
            ),
        )
        result = score_code_style_checkstyle(rule, _commit(), runs)
        assert result.result == "fail"
        assert result.evidence_url == "https://github.com/x/y/runs/1"

    def test_unknown_when_no_check_run_recorded(self, policy):
        rule = policy.rule("code-style-checkstyle")
        result = score_code_style_checkstyle(rule, _commit(), ())
        assert result.result == "unknown"

    def test_none_on_pre_4_1_branch(self, policy):
        rule = policy.rule("code-style-checkstyle")
        commit = _commit(branch="cassandra-4.0")
        assert score_code_style_checkstyle(rule, commit, ()) is None

    def test_applies_on_trunk_and_5_0(self, policy):
        rule = policy.rule("code-style-checkstyle")
        assert score_code_style_checkstyle(rule, _commit(branch="trunk"), ()) is not None
        assert score_code_style_checkstyle(rule, _commit(branch="cassandra-5.0"), ()) is not None

    def test_unknown_reason_distinguishes_outside_retention(self, policy):
        """issue #36 fixup cycle 2: a commit older than the collector's
        retention cutoff gets a distinct, more informative `unknown` reason
        than a commit that just hasn't been checked yet."""
        rule = policy.rule("code-style-checkstyle")
        old_commit = _commit(commit_date=datetime(2020, 1, 1, tzinfo=timezone.utc))

        recent_result = score_code_style_checkstyle(rule, old_commit, ())
        assert recent_result.result == "unknown"
        assert recent_result.evidence == "no GitHub check-run recorded for this commit sha"

        retention_cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc)
        outside_result = score_code_style_checkstyle(
            rule, old_commit, (), retention_cutoff=retention_cutoff
        )
        assert outside_result.result == "unknown"
        assert outside_result.evidence == "check-run history outside GitHub retention"

    def test_retention_cutoff_does_not_affect_commit_within_window(self, policy):
        rule = policy.rule("code-style-checkstyle")
        recent_commit = _commit(commit_date=datetime(2026, 6, 1, tzinfo=timezone.utc))
        retention_cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc)
        result = score_code_style_checkstyle(
            rule, recent_commit, (), retention_cutoff=retention_cutoff
        )
        assert result.result == "unknown"
        assert result.evidence == "no GitHub check-run recorded for this commit sha"

    def test_retention_cutoff_never_overrides_real_evidence(self, policy):
        """A commit outside retention that nonetheless *has* recorded
        evidence (e.g. from before the retention setting was tightened)
        still scores from that evidence -- retention only changes the
        `unknown` wording, never suppresses a real result."""
        rule = policy.rule("code-style-checkstyle")
        old_commit = _commit(commit_date=datetime(2020, 1, 1, tzinfo=timezone.utc))
        runs = (
            CheckstyleEvidence(
                sha="a" * 40, check_run_name="ant-check-jdk11", conclusion="success"
            ),
        )
        result = score_code_style_checkstyle(
            rule, old_commit, runs, retention_cutoff=datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        assert result.result == "pass"


class TestCommitFactsRow:
    def test_facts_derived_from_changed_paths(self):
        commit = _commit(changed_paths=("CHANGES.txt", "test/unit/Foo.java", "src/Bar.java"))
        fact = build_commit_facts_row(commit)
        assert fact.changes_txt_touched is True
        assert fact.news_txt_touched is False
        assert fact.test_touched is True

    def test_facts_false_when_changed_paths_not_collected(self):
        commit = _commit(changed_paths=None)
        fact = build_commit_facts_row(commit)
        assert fact.changes_txt_touched is False
        assert fact.news_txt_touched is False
        assert fact.test_touched is False
