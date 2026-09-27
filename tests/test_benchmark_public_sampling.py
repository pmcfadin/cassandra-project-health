"""Tests for project_health.benchmark_public.sampling (issue #89)."""

from __future__ import annotations

from project_health.benchmark_public.loaders import DatasetItem
from project_health.benchmark_public.mapping import LabelMapping
from project_health.benchmark_public.sampling import stratified_sample


def _items(n_positive: int, n_negative: int) -> list[DatasetItem]:
    items = []
    for i in range(n_positive):
        items.append(
            DatasetItem(item_id=f"pos{i}", text="t", parent_text=None, raw_labels={"attack": True})
        )
    for i in range(n_negative):
        items.append(
            DatasetItem(item_id=f"neg{i}", text="t", parent_text=None, raw_labels={"attack": False})
        )
    return items


def _mapping(**overrides) -> LabelMapping:
    base = dict(
        dataset_id="ds1",
        our_label="personal_attack",
        raw_field="attack",
        positive_values=(True,),
        strength="strong",
        gating=False,
        notes="x",
    )
    base.update(overrides)
    return LabelMapping(**base)


def test_sample_returns_whole_population_when_target_exceeds_it() -> None:
    items = _items(5, 5)
    result = stratified_sample("ds1", items, [_mapping()], target_n=100, seed=1)
    assert result.sample_n == 10
    assert result.population_n == 10
    assert result.population_positives == 5
    assert result.population_positives_by_label == {"personal_attack": 5}


def test_sample_caps_positive_fraction() -> None:
    # 500 positives, 10 negatives, target 20, cap 0.5 -> at most 10 positives drawn.
    items = _items(500, 10)
    result = stratified_sample(
        "ds1", items, [_mapping()], target_n=20, seed=1, positive_fraction_cap=0.5
    )
    assert result.sample_n == 20
    assert result.sample_positives <= 10
    assert result.population_n == 510
    assert result.population_positives == 500


def test_sample_is_deterministic_for_a_fixed_seed() -> None:
    items = _items(50, 50)
    result_a = stratified_sample("ds1", items, [_mapping()], target_n=20, seed=42)
    result_b = stratified_sample("ds1", items, [_mapping()], target_n=20, seed=42)
    assert [i.item_id for i in result_a.items] == [i.item_id for i in result_b.items]


def test_sample_differs_across_seeds_with_enough_population() -> None:
    items = _items(50, 50)
    result_a = stratified_sample("ds1", items, [_mapping()], target_n=20, seed=1)
    result_b = stratified_sample("ds1", items, [_mapping()], target_n=20, seed=2)
    assert [i.item_id for i in result_a.items] != [i.item_id for i in result_b.items]


def test_sample_fills_from_negatives_when_positives_scarce() -> None:
    items = _items(2, 100)
    result = stratified_sample("ds1", items, [_mapping()], target_n=10, seed=1)
    assert result.sample_n == 10
    assert result.sample_positives == 2  # both scarce positives included


def test_synthetic_mapping_ignored_for_stratification() -> None:
    items = _items(3, 3)
    synthetic = LabelMapping(
        dataset_id="ds1",
        our_label="any_negative_rollup",
        raw_field="attack",
        positive_values=(True,),
        strength=None,
        gating=False,
        notes="x",
        synthetic=True,
        source_labels=("personal_attack", "hostility"),
    )
    # Only the synthetic mapping is passed -- since it's ignored for
    # stratification, "positive" pool is empty and population_positives_by_label
    # has no entries (synthetic mappings never contribute to it).
    result = stratified_sample("ds1", items, [synthetic], target_n=100, seed=1)
    assert result.population_positives == 0
    assert result.population_positives_by_label == {}
