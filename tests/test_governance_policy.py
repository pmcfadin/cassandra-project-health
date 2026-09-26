"""Tests for project_health.governance.policy (issue #36).

Loads the real repo-root `governance-policy.yaml` (not a fixture copy) —
this is the binding v1 policy this engine scores against, so a test here
that stops matching the real file is exactly the signal we want.
"""

from datetime import date

import pytest

from project_health.governance.policy import (
    DEFAULT_POLICY_PATH,
    PolicyValidationError,
    load_policy,
)


@pytest.fixture(scope="module")
def policy():
    return load_policy(DEFAULT_POLICY_PATH)


def test_top_level_fields(policy):
    assert policy.version == 1
    assert policy.policy_name == "cassandra-governance-v1"
    assert policy.approved_by == "pmcfadin"
    assert policy.approved_on == date(2026, 9, 25)


def test_all_four_scored_checks_present(policy):
    for check_id in (
        "reviewer-present",
        "jira-ticket-referenced",
        "pre-commit-ci-evidence",
        "code-style-checkstyle",
    ):
        rule = policy.rule(check_id)
        assert rule.id == check_id


def test_unscored_rules_marked_not_scored(policy):
    for check_id in ("changes-txt-entry", "news-txt-entry", "test-touched"):
        assert policy.rule(check_id).scored is False


def test_unknown_check_id_raises(policy):
    with pytest.raises(KeyError):
        policy.rule("not-a-real-check")


class TestReviewerPresentRule:
    def test_effective_from_and_fail_allowed(self, policy):
        rule = policy.rule("reviewer-present")
        assert rule.effective_from == date(2020, 6, 25)
        assert rule.fail_allowed is True
        assert rule.in_force_on(date(2020, 6, 24)) is False
        assert rule.in_force_on(date(2020, 6, 25)) is True
        assert rule.in_force_on(date(2024, 1, 1)) is True

    def test_applies_to_trunk_and_release_branches(self, policy):
        rule = policy.rule("reviewer-present")
        assert rule.applies_to_branch("trunk") is True
        assert rule.applies_to_branch("cassandra-5.0") is True
        assert rule.applies_to_branch("cassandra-4.1") is True
        assert rule.applies_to_branch("some-feature-branch") is False

    def test_ninja_exemption(self, policy):
        rule = policy.rule("reviewer-present")
        exemption = rule.matching_exemption("ninjafix - links in CONTRIBUTING.md")
        assert exemption is not None
        assert exemption.id == "ninja"

    def test_ninja_exemption_requires_word_boundary(self, policy):
        rule = policy.rule("reviewer-present")
        # "ninjas" contains "ninja" but the pattern is \bninja(fix)?\b --
        # "ninjas" doesn't match because "s" glues onto the boundary.
        assert rule.matching_exemption("we are not ninjas here") is None

    def test_release_housekeeping_version_increment_first_line_only(self, policy):
        rule = policy.rule("reviewer-present")
        exemption = rule.matching_exemption("increment to version 5.0.10")
        assert exemption is not None
        assert exemption.id == "release-housekeeping"

    def test_release_housekeeping_debian_changelog(self, policy):
        rule = policy.rule("reviewer-present")
        exemption = rule.matching_exemption("Prepare debian changelog for 3.11.19")
        assert exemption is not None
        assert exemption.id == "release-housekeeping"

    def test_release_housekeeping_submodule_repin(self, policy):
        rule = policy.rule("reviewer-present")
        exemption = rule.matching_exemption("repin accord submodule")
        assert exemption is not None
        assert exemption.id == "release-housekeeping"

    def test_version_increment_pattern_is_first_line_scoped(self, policy):
        rule = policy.rule("reviewer-present")
        # The "increment...version" phrase appears, but not on the first
        # line -- first_line scope must not match it.
        message = "Some unrelated subject\n\nincrement to version 5.0.10 mentioned in body"
        assert rule.matching_exemption(message) is None

    def test_review_wording_check_matches_review_variants(self, policy):
        rule = policy.rule("reviewer-present")
        for word in ("review", "reviewed", "reviewer", "reviewers", "Reviewed", "REVIEWERS"):
            assert rule.review_wording_present(f"some message with {word} in it") is True

    def test_review_wording_check_false_when_absent(self, policy):
        rule = policy.rule("reviewer-present")
        assert rule.review_wording_present("just a plain commit message") is False


class TestCodeStyleCheckstyleRule:
    def test_applies_only_to_4_1_plus(self, policy):
        rule = policy.rule("code-style-checkstyle")
        assert rule.applies_to_branch("trunk") is True
        assert rule.applies_to_branch("cassandra-5.0") is True
        assert rule.applies_to_branch("cassandra-6.0") is True
        assert rule.applies_to_branch("cassandra-4.1") is True
        assert rule.applies_to_branch("cassandra-4.0") is False
        assert rule.applies_to_branch("cassandra-3.11") is False

    def test_no_dated_effective_from_always_in_force(self, policy):
        rule = policy.rule("code-style-checkstyle")
        assert rule.effective_from is None
        assert rule.in_force_on(date(1999, 1, 1)) is True

    def test_fail_allowed(self, policy):
        assert policy.rule("code-style-checkstyle").fail_allowed is True


class TestPreCommitCiEvidenceRule:
    def test_no_fail_allowed(self, policy):
        assert policy.rule("pre-commit-ci-evidence").fail_allowed is False

    def test_no_exemptions(self, policy):
        assert policy.rule("pre-commit-ci-evidence").exemptions == ()


class TestJiraTicketReferencedRule:
    def test_no_fail_allowed(self, policy):
        assert policy.rule("jira-ticket-referenced").fail_allowed is False

    def test_null_effective_from_always_in_force(self, policy):
        rule = policy.rule("jira-ticket-referenced")
        assert rule.effective_from is None
        assert rule.in_force_on(date(2000, 1, 1)) is True


def test_load_policy_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path / "does-not-exist.yaml")


def test_load_policy_missing_required_key_raises(tmp_path):
    bad = tmp_path / "bad-policy.yaml"
    bad.write_text("version: 1\npolicy_name: x\n")
    with pytest.raises(PolicyValidationError):
        load_policy(bad)
