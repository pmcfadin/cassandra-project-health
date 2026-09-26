"""Tests for project_health.governance.overrides (issue #36)."""

import pytest

from project_health.governance.overrides import (
    OverrideValidationError,
    index_overrides,
    load_overrides,
)


def test_missing_file_returns_empty_list(tmp_path):
    assert load_overrides(tmp_path / "nope.yaml") == []


def test_empty_overrides_list(tmp_path):
    path = tmp_path / "overrides.yaml"
    path.write_text("overrides: []\n")
    assert load_overrides(path) == []


def test_loads_valid_override(tmp_path):
    path = tmp_path / "overrides.yaml"
    path.write_text(
        """
overrides:
  - sha: "abc123"
    check_id: reviewer-present
    result: pass
    reason: "confirmed manually"
    reviewer: pmcfadin
"""
    )
    overrides = load_overrides(path)
    assert len(overrides) == 1
    assert overrides[0].sha == "abc123"
    assert overrides[0].result == "pass"


def test_missing_required_field_raises(tmp_path):
    path = tmp_path / "overrides.yaml"
    path.write_text(
        """
overrides:
  - sha: "abc123"
    check_id: reviewer-present
    result: pass
    reason: "confirmed manually"
"""
    )
    with pytest.raises(OverrideValidationError):
        load_overrides(path)


def test_invalid_result_state_raises(tmp_path):
    path = tmp_path / "overrides.yaml"
    path.write_text(
        """
overrides:
  - sha: "abc123"
    check_id: reviewer-present
    result: maybe
    reason: "x"
    reviewer: pmcfadin
"""
    )
    with pytest.raises(OverrideValidationError):
        load_overrides(path)


def test_the_real_repo_governance_overrides_file_loads_cleanly():
    """governance_overrides.yaml (repo root) must always load without
    error, even with its default empty `overrides: []` list."""
    from project_health.governance.overrides import DEFAULT_OVERRIDES_PATH

    assert load_overrides(DEFAULT_OVERRIDES_PATH) == []


def test_index_overrides_keyed_by_sha_and_check_id():
    from project_health.governance.overrides import Override

    o1 = Override(sha="a", check_id="reviewer-present", result="pass", reason="x", reviewer="p")
    o2 = Override(
        sha="b", check_id="jira-ticket-referenced", result="unknown", reason="y", reviewer="p"
    )
    index = index_overrides([o1, o2])
    assert index[("a", "reviewer-present")] is o1
    assert index[("b", "jira-ticket-referenced")] is o2
    assert len(index) == 2
