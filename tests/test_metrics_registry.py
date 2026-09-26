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
}

HEADCOUNT_METRIC_IDS = {
    "active_contributors_monthly",
    "new_contributors_monthly",
    "unique_reviewers_monthly",
}


def test_registry_lists_exactly_the_nine_registered_metrics():
    assert set(METRIC_IDS) == EXPECTED_METRIC_IDS


def test_build_registry_stamps_headcount_metrics_1_1_and_others_1_0():
    """Issue #27: the three headcount metrics bumped to "1.1" (no more
    sample-size floor); every other metric (including pmc_joins_quarterly
    from issue #50 and issue #53's three new ones) ships at "1.0"."""
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

    other_notes = set()
    for metric_id in EXPECTED_METRIC_IDS - HEADCOUNT_METRIC_IDS:
        row = rows_by_id[metric_id]
        assert row["version"] == "1.0"
        assert row["changelog_note"]
        other_notes.add(row["changelog_note"])

    # The headcount notes are a real, distinct explanation of the floor
    # change -- not the same generic note the other three metrics carry.
    assert headcount_notes.isdisjoint(other_notes)
    for note in headcount_notes:
        assert "floor" in note.lower() or "sample" in note.lower()


def test_build_registry_is_a_pure_function_of_changed_at():
    first = build_registry(NOW)
    second = build_registry(NOW)
    assert first.equals(second)
