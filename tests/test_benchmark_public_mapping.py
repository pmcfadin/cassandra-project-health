"""Tests for project_health.benchmark_public.mapping (issue #89)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from project_health.benchmark_public.mapping import (
    MappingError,
    is_positive,
    load_label_mapping,
)


def _write_mapping(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "mapping.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_load_real_mapping_parses_and_validates() -> None:
    mapping_set = load_label_mapping()
    assert mapping_set.version == 1
    assert mapping_set.for_dataset("ferreira_lkml")
    assert mapping_set.for_dataset("nonexistent_dataset") == ()


def test_synthetic_entry_requires_gating_false(tmp_path: Path) -> None:
    data = {
        "version": 1,
        "mappings": {
            "ds1": [
                {
                    "our_label": "any_negative_rollup",
                    "synthetic": True,
                    "source_labels": ["personal_attack", "hostility"],
                    "raw_field": "is_toxic",
                    "positive_values": [1],
                    "gating": True,  # invalid -- must be False
                    "notes": "x",
                }
            ]
        },
    }
    path = _write_mapping(tmp_path, data)
    with pytest.raises(MappingError, match="gating"):
        load_label_mapping(path)


def test_non_synthetic_our_label_must_be_message_level(tmp_path: Path) -> None:
    data = {
        "version": 1,
        "mappings": {
            "ds1": [
                {
                    "our_label": "not_a_real_label",
                    "raw_field": "x",
                    "positive_values": [1],
                    "strength": "strong",
                    "gating": False,
                    "notes": "x",
                }
            ]
        },
    }
    path = _write_mapping(tmp_path, data)
    with pytest.raises(MappingError, match="message-level"):
        load_label_mapping(path)


def test_invalid_strength_rejected(tmp_path: Path) -> None:
    data = {
        "version": 1,
        "mappings": {
            "ds1": [
                {
                    "our_label": "hostility",
                    "raw_field": "x",
                    "positive_values": [1],
                    "strength": "medium",
                    "gating": False,
                    "notes": "x",
                }
            ]
        },
    }
    path = _write_mapping(tmp_path, data)
    with pytest.raises(MappingError, match="strength"):
        load_label_mapping(path)


# --- is_positive ------------------------------------------------------------------------


def _mapping(**overrides):
    from project_health.benchmark_public.mapping import LabelMapping

    base = dict(
        dataset_id="ds1",
        our_label="hostility",
        raw_field="tbdf_categories",
        positive_values=("bitter_frustration", "vulgarity"),
        strength="strong",
        gating=True,
        notes="x",
    )
    base.update(overrides)
    return LabelMapping(**base)


def test_is_positive_scalar_membership() -> None:
    mapping = _mapping(raw_field="is_toxic", positive_values=(1,))
    assert is_positive(mapping, {"is_toxic": 1}) is True
    assert is_positive(mapping, {"is_toxic": 0}) is False


def test_is_positive_set_intersection() -> None:
    mapping = _mapping()
    assert is_positive(mapping, {"tbdf_categories": frozenset({"bitter_frustration"})}) is True
    assert is_positive(mapping, {"tbdf_categories": frozenset({"irony"})}) is False
    assert is_positive(mapping, {"tbdf_categories": frozenset()}) is False


def test_is_positive_missing_or_none_field_returns_none() -> None:
    mapping = _mapping()
    assert is_positive(mapping, {}) is None
    assert is_positive(mapping, {"tbdf_categories": None}) is None
    other = _mapping(raw_field="email_classification", positive_values=("technical",))
    assert is_positive(other, {"email_classification": None}) is None
