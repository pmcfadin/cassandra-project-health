"""Tests for project_health.metrics.registry (issue #7)."""

from __future__ import annotations

from datetime import datetime, timezone

from project_health.metrics.registry import METRIC_IDS, build_registry

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)

EXPECTED_METRIC_IDS = {
    "active_contributors_monthly",
    "new_contributors_monthly",
    "unique_reviewers_monthly",
    "reviewer_hhi",
    "median_resolution_latency_jira",
    "stale_jira_rate",
    "pmc_joins_quarterly",
    # issue #53
    "truck_factor",
    "contributor_absence_factor",
    "contributor_hhi",
    # issue #52
    "elephant_factor",
    "organizational_hhi",
    "single_org_share",
    "unknown_affiliation_rate",
    # issue #35
    "time_to_first_reply_devlist",
    "unanswered_thread_rate_devlist",
    # issue #158
    "time_to_first_reply_userlist",
    "unanswered_thread_rate_userlist",
    "mailing_list_messages_monthly_dev",
    "mailing_list_messages_monthly_user",
    "mailing_list_new_threads_monthly_dev",
    "mailing_list_new_threads_monthly_user",
    "mailing_list_distinct_senders_monthly_dev",
    "mailing_list_distinct_senders_monthly_user",
    # issue #54
    "pr_merge_lead_time",
    "pr_time_to_first_review",
    "pr_time_to_close",
    "pr_review_engagement",
    "time_to_first_response_jira",
    "stale_pr_rate",
    # issue #134
    "median_resolution_latency_jira_cohort_12m",
    # issue #136, DECISIONS.md D29
    "change_request_closure_ratio_pr",
    "change_request_closure_ratio_jira_patch",
    # issue #135
    "release_frequency",
    "release_regularity",
    "time_since_last_release",
    "days_between_releases",
    # issue #159
    "first_to_second_conversion_rate",
    "funnel_stage_conversion",
    "contributor_tenure_survival",
    "contributor_churn_rate",
    "sustained_contributor_count",
}

HEADCOUNT_METRIC_IDS = {
    "active_contributors_monthly",
    "new_contributors_monthly",
    "unique_reviewers_monthly",
}

# issue #134: `median_resolution_latency_jira` bumped 1.0 -> 1.1 (adds the
# `bulk_closure_days` annotation to details_json; the value/n/flag formula
# itself is unchanged) -- a third version bucket, distinct from the
# headcount metrics' own 1.0 -> 1.1 bump (issue #27) and from every
# still-1.0 metric.
OTHER_1_1_METRIC_IDS = {"median_resolution_latency_jira"}

# issue #173, DECISIONS.md D31: every GitHub-PR-sourced metric whose
# population spans `pull_requests.repos` (now filtered to the configured
# repo set, pipeline.py::_filter_to_configured_pr_repos) bumped 1.0 -> 1.1
# in lockstep with engine.py's `DEFINITION_VERSIONS`. `time_to_first_
# response_jira`/`change_request_closure_ratio_jira_patch` are JIRA-sourced
# and stay 1.0 -- a fourth, distinct version bucket.
DEBLEND_1_1_METRIC_IDS = {
    "pr_merge_lead_time",
    "pr_time_to_first_review",
    "pr_time_to_close",
    "pr_review_engagement",
    "stale_pr_rate",
    "change_request_closure_ratio_pr",
}


def test_registry_lists_exactly_the_registered_metrics():
    assert set(METRIC_IDS) == EXPECTED_METRIC_IDS


def test_build_registry_stamps_headcount_metrics_1_1_and_others_1_0():
    """Issue #27: the three headcount metrics bumped to "1.1" (no more
    sample-size floor); every other metric (including pmc_joins_quarterly
    from issue #50, issue #53's three new ones, issue #52's four new
    organizational-diversity metrics, and issue #35's two new dev@
    responsiveness metrics) ships at "1.0"."""
    table = build_registry(NOW)

    assert table.num_rows == len(EXPECTED_METRIC_IDS)
    assert set(table.column("metric_id").to_pylist()) == EXPECTED_METRIC_IDS
    assert all(table.column("description").to_pylist())  # every row has a non-empty description
    assert table.column("changed_at").to_pylist() == [NOW] * len(EXPECTED_METRIC_IDS)

    rows_by_id = {row["metric_id"]: row for row in table.to_pylist()}

    headcount_notes = set()
    for metric_id in HEADCOUNT_METRIC_IDS:
        row = rows_by_id[metric_id]
        assert row["version"] == "1.1"
        assert row["changelog_note"]
        headcount_notes.add(row["changelog_note"])

    other_1_1_notes = set()
    for metric_id in OTHER_1_1_METRIC_IDS:
        row = rows_by_id[metric_id]
        assert row["version"] == "1.1"
        assert row["changelog_note"]
        other_1_1_notes.add(row["changelog_note"])

    deblend_notes = set()
    for metric_id in DEBLEND_1_1_METRIC_IDS:
        row = rows_by_id[metric_id]
        assert row["version"] == "1.1"
        assert row["changelog_note"]
        deblend_notes.add(row["changelog_note"])

    other_notes = set()
    for metric_id in (
        EXPECTED_METRIC_IDS - HEADCOUNT_METRIC_IDS - OTHER_1_1_METRIC_IDS - DEBLEND_1_1_METRIC_IDS
    ):
        row = rows_by_id[metric_id]
        assert row["version"] == "1.0"
        assert row["changelog_note"]
        other_notes.add(row["changelog_note"])

    # The headcount notes are a real, distinct explanation of the floor
    # change -- not the same generic note the other three metrics carry.
    assert headcount_notes.isdisjoint(other_notes)
    for note in headcount_notes:
        assert "floor" in note.lower() or "sample" in note.lower()

    # issue #134: median_resolution_latency_jira's own 1.0 -> 1.1 note is
    # about the bulk-closure annotation, not the headcount floor change --
    # a distinct explanation from both other buckets.
    assert other_1_1_notes.isdisjoint(headcount_notes)
    assert other_1_1_notes.isdisjoint(other_notes)
    assert other_1_1_notes.isdisjoint(deblend_notes)
    for note in other_1_1_notes:
        assert "bulk_closure" in note.lower() or "bulk-closure" in note.lower()

    # issue #173: the de-blend note is a distinct explanation again -- about
    # the repo population change, not the floor or the bulk-closure annotation.
    assert deblend_notes.isdisjoint(headcount_notes)
    assert deblend_notes.isdisjoint(other_notes)
    for note in deblend_notes:
        assert "pull_requests.repos" in note


def test_build_registry_is_a_pure_function_of_changed_at():
    first = build_registry(NOW)
    second = build_registry(NOW)
    assert first.equals(second)
