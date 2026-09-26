"""Tests for `scoring/config.py`'s `scoring.yaml` loader (issue #57, D20)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from project_health.scoring.config import (
    DEFAULT_SCORING_PATH,
    ScoringConfigError,
    load_scoring_config,
)


def test_default_scoring_path_points_at_the_repo_root_file():
    assert DEFAULT_SCORING_PATH.name == "scoring.yaml"
    assert DEFAULT_SCORING_PATH.is_file()


def test_loads_the_real_repo_scoring_yaml():
    config = load_scoring_config()
    assert config.scoring_version == "1.0.0"
    assert config.baseline.trailing_months == 24
    assert config.baseline.min_completed_months == 12
    assert config.baseline.stable_threshold == 1.5
    assert config.baseline.large_deviation_threshold == 3.0
    assert config.baseline.confirmation_window_months == 3
    assert config.baseline.confirmation_required == 2
    assert set(config.composite.dimension_weights) == {
        "contributor sustainability",
        "reviewer capacity",
        "responsiveness",
        "organizational diversity",
        "release cadence",
    }
    assert sum(config.composite.dimension_weights.values()) == pytest.approx(1.0)
    # D20: classified (Phase 2) and governance are excluded, disclosed with a reason.
    assert "interaction health" in config.composite.excluded_dimensions
    assert "governance" in config.composite.excluded_dimensions


def test_mad_zero_floor_falls_back_to_the_default():
    config = load_scoring_config()
    floor = config.baseline.mad_zero_floor("some_metric_with_no_override")
    assert floor == config.baseline.mad_zero_floor_default


def test_missing_top_level_key_raises(tmp_path: Path):
    bad = tmp_path / "scoring.yaml"
    bad.write_text(yaml.safe_dump({"scoring_version": "1.0.0"}))
    with pytest.raises(ScoringConfigError):
        load_scoring_config(bad)


def test_missing_baseline_field_raises(tmp_path: Path):
    raw = yaml.safe_load(DEFAULT_SCORING_PATH.read_text())
    del raw["baseline"]["trailing_months"]
    bad = tmp_path / "scoring.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ScoringConfigError):
        load_scoring_config(bad)


def test_non_mapping_top_level_raises(tmp_path: Path):
    bad = tmp_path / "scoring.yaml"
    bad.write_text(yaml.safe_dump(["not", "a", "mapping"]))
    with pytest.raises(ScoringConfigError):
        load_scoring_config(bad)


def test_empty_dimension_weights_raises(tmp_path: Path):
    raw = yaml.safe_load(DEFAULT_SCORING_PATH.read_text())
    raw["composite"]["dimension_weights"] = {}
    bad = tmp_path / "scoring.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ScoringConfigError):
        load_scoring_config(bad)
