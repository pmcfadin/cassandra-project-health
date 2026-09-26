"""Tests for project_health.governance.registry (issue #36)."""

from datetime import datetime, timezone

from project_health.governance.engine import SCORED_CHECK_IDS
from project_health.governance.metrics import DEFINITION_VERSION, metric_id_for_check
from project_health.governance.registry import build_governance_registry
from project_health.schema import get_schema

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def test_registers_one_row_per_scored_check_at_v1_0():
    table = build_governance_registry(NOW)
    rows = {r["metric_id"]: r for r in table.to_pylist()}
    expected_ids = {metric_id_for_check(check_id) for check_id in SCORED_CHECK_IDS}
    assert set(rows) == expected_ids
    for row in rows.values():
        assert row["version"] == DEFINITION_VERSION == "1.0"
        assert row["changed_at"] == NOW
        assert row["description"]
        assert row["changelog_note"]


def test_schema_valid():
    table = build_governance_registry(NOW)
    assert table.schema.equals(get_schema("metric_definition_version"))


def test_pure_function_of_changed_at():
    first = build_governance_registry(NOW)
    second = build_governance_registry(NOW)
    assert first.equals(second)
