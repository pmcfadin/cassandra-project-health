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
}


def test_registry_lists_exactly_the_six_m0_metrics():
    assert set(METRIC_IDS) == EXPECTED_METRIC_IDS


def test_build_registry_produces_one_row_per_metric_all_at_version_1_0():
    table = build_registry(NOW)

    assert table.num_rows == 6
    assert set(table.column("metric_id").to_pylist()) == EXPECTED_METRIC_IDS
    assert set(table.column("version").to_pylist()) == {"1.0"}
    assert all(table.column("description").to_pylist())  # every row has a non-empty description
    assert table.column("changed_at").to_pylist() == [NOW] * 6


def test_build_registry_is_a_pure_function_of_changed_at():
    first = build_registry(NOW)
    second = build_registry(NOW)
    assert first.equals(second)
