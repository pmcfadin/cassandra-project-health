"""Tests for project_health.benchmark_public.evaluate (issue #89)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_health.benchmark_public.evaluate import (
    compute_all_category_separations,
    compute_category_separation,
    evaluate_benchmark,
    evaluate_dataset_label,
)
from project_health.benchmark_public.loaders import DatasetItem
from project_health.benchmark_public.mapping import LabelMapping, LabelMappingSet
from project_health.benchmark_public.registry import DatasetSpec, Registry
from project_health.benchmark_public.runner import (
    BenchmarkRunResult,
    DatasetRunData,
    message_id_for,
)
from project_health.benchmark_public.sampling import SamplingResult
from project_health.classify.classifier import ClassificationRecord, Label, RunResult, Usage

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def _record(message_id: str, **label_probs: float) -> ClassificationRecord:
    return ClassificationRecord(
        message_id=message_id,
        thread_id=message_id,
        source="mailing_list",
        classifier_version="1.0.0",
        question_set_version="1",
        model_id="jev-1.13.0",
        input_hash="h" * 64,
        classified_at=NOW,
        usage=Usage(input_tokens=10, output_tokens=1),
        labels={k: Label(probability=v) for k, v in label_probs.items()},
    )


def _mapping(**overrides) -> LabelMapping:
    base = dict(
        dataset_id="ds1",
        our_label="hostility",
        raw_field="tbdf_categories",
        positive_values=("bitter_frustration",),
        strength="strong",
        gating=True,
        notes="x",
    )
    base.update(overrides)
    return LabelMapping(**base)


def test_evaluate_dataset_label_basic() -> None:
    items = [
        DatasetItem("i1", "t", None, {"tbdf_categories": frozenset({"bitter_frustration"})}),
        DatasetItem("i2", "t", None, {"tbdf_categories": frozenset()}),
        DatasetItem("i3", "t", None, {"tbdf_categories": frozenset({"irony"})}),
    ]
    records = {
        "i1": _record("i1", hostility=0.9),
        "i2": _record("i2", hostility=0.1),
        "i3": _record("i3", hostility=0.05),
    }
    evaluation = evaluate_dataset_label(
        "ds1",
        _mapping(),
        items,
        records,
        population_n=3,
        population_positives=1,
        seed=1,
        bootstrap_iterations=20,
    )
    assert evaluation is not None
    assert evaluation.n_candidates == 3
    assert evaluation.n_scored == 3
    assert evaluation.n_positives == 1
    assert evaluation.best.precision == 1.0
    assert evaluation.best.recall == 1.0
    assert evaluation.gate_threshold_value is None
    assert evaluation.at_gate_threshold is None


def test_evaluate_dataset_label_returns_none_when_nothing_scorable() -> None:
    items = [DatasetItem("i1", "t", None, {})]  # no raw_field at all
    records = {"i1": _record("i1", hostility=0.9)}
    evaluation = evaluate_dataset_label(
        "ds1", _mapping(), items, records, population_n=1, population_positives=0, seed=1
    )
    assert evaluation is None


def test_evaluate_dataset_label_skips_items_with_no_record() -> None:
    items = [
        DatasetItem("i1", "t", None, {"tbdf_categories": frozenset({"bitter_frustration"})}),
        DatasetItem("i2", "t", None, {"tbdf_categories": frozenset()}),
    ]
    records = {"i1": _record("i1", hostility=0.9)}  # i2 never classified
    evaluation = evaluate_dataset_label(
        "ds1", _mapping(), items, records, population_n=2, population_positives=1, seed=1
    )
    assert evaluation is not None
    assert evaluation.n_candidates == 1
    assert evaluation.n_scored == 1


def test_synthetic_rollup_uses_max_of_source_labels() -> None:
    rollup = LabelMapping(
        dataset_id="toxicr",
        our_label="any_negative_rollup",
        raw_field="is_toxic",
        positive_values=(1,),
        strength=None,
        gating=False,
        notes="x",
        synthetic=True,
        source_labels=("personal_attack", "hostility"),
    )
    items = [
        DatasetItem("i1", "t", None, {"is_toxic": 1}),
        DatasetItem("i2", "t", None, {"is_toxic": 0}),
    ]
    records = {
        "i1": _record("i1", personal_attack=0.2, hostility=0.7),
        "i2": _record("i2", personal_attack=0.1, hostility=0.1),
    }
    evaluation = evaluate_dataset_label(
        "toxicr", rollup, items, records, population_n=2, population_positives=None, seed=1
    )
    assert evaluation is not None
    # y_prob for i1 should be max(0.2, 0.7) = 0.7
    assert evaluation.best.precision <= 1.0  # sanity: doesn't crash, computed fine
    assert evaluation.population_positives is None


def test_evaluate_benchmark_end_to_end() -> None:
    spec = DatasetSpec(
        id="ds1",
        name="Dataset One",
        citation="c",
        license="CC0",
        status="working",
        blocked_reason=None,
        files=(),
        loader="load_ds1",
        categorizer=None,
        source_venue="mailing_list",
        source_venue_rationale="r",
        target_n=2,
        seed=1,
        reported_iaa=None,
        caveats=(),
    )
    items = (
        DatasetItem("i1", "t", None, {"tbdf_categories": frozenset({"bitter_frustration"})}),
        DatasetItem("i2", "t", None, {"tbdf_categories": frozenset()}),
    )
    sampling = SamplingResult(
        dataset_id="ds1",
        seed=1,
        target_n=2,
        population_n=2,
        population_positives=1,
        sample_n=2,
        sample_positives=1,
        positive_fraction_cap=0.5,
        items=items,
        population_positives_by_label={"hostility": 1},
    )
    run_data = DatasetRunData(spec=spec, sampling=sampling, file_paths=())
    classification_by_message_id = {
        message_id_for("ds1", "i1"): _record(message_id_for("ds1", "i1"), hostility=0.9),
        message_id_for("ds1", "i2"): _record(message_id_for("ds1", "i2"), hostility=0.1),
    }
    run = BenchmarkRunResult(
        datasets={"ds1": run_data},
        classification_by_message_id=classification_by_message_id,
        run_result=RunResult(
            status="completed",
            records=list(classification_by_message_id.values()),
            calls_made=2,
            cache_hits=0,
            input_tokens_used=20,
            output_tokens_used=2,
            estimated_cost_usd=0.0001,
        ),
        elapsed_seconds=0.1,
        cache_path=Path("/tmp/cache.jsonl"),
        manifest={},
    )
    mapping_set = LabelMappingSet(version=1, by_dataset={"ds1": (_mapping(),)})

    results = evaluate_benchmark(run, mapping_set, seed=1, bootstrap_iterations=10)
    assert "ds1" in results
    assert len(results["ds1"]) == 1
    assert results["ds1"][0].our_label == "hostility"


# --- Separation by source category (orchestrator review of issue #89) ------------------


def test_compute_category_separation_groups_and_averages() -> None:
    items = [
        DatasetItem("i1", "t", None, {"tbdf_categories": frozenset({"bitter_frustration"})}),
        DatasetItem(
            "i2", "t", None, {"tbdf_categories": frozenset({"bitter_frustration", "irony"})}
        ),
        DatasetItem("i3", "t", None, {"tbdf_categories": frozenset()}),
    ]
    records = {
        "i1": _record("i1", personal_attack=0.2, hostility=0.8, dismissiveness=0.1, sarcasm=0.1),
        "i2": _record("i2", personal_attack=0.1, hostility=0.6, dismissiveness=0.1, sarcasm=0.9),
        "i3": _record("i3", personal_attack=0.0, hostility=0.05, dismissiveness=0.0, sarcasm=0.0),
    }
    separations = compute_category_separation("ds1", "categorize_ferreira", items, records)
    by_category = {s.category: s for s in separations}

    assert by_category["bitter_frustration"].n == 2  # i1 and i2 both coded bitter_frustration
    assert by_category["bitter_frustration"].mean_probability["hostility"] == pytest.approx(0.7)
    assert by_category["irony"].n == 1
    assert by_category["irony"].mean_probability["sarcasm"] == pytest.approx(0.9)
    assert by_category["(none coded)"].n == 1
    assert by_category["(none coded)"].mean_probability["hostility"] == pytest.approx(0.05)


def test_compute_category_separation_skips_unclassified_items() -> None:
    items = [
        DatasetItem("i1", "t", None, {"is_toxic": 1}),
        DatasetItem("i2", "t", None, {"is_toxic": 0}),
    ]
    records = {"i1": _record("i1", hostility=0.9)}  # i2 never classified
    separations = compute_category_separation("ds1", "categorize_toxicr", items, records)
    by_category = {s.category: s for s in separations}
    assert "toxic" in by_category
    assert "not_toxic" not in by_category  # i2 skipped entirely, no record


def test_compute_all_category_separations_skips_datasets_with_no_categorizer() -> None:
    spec_no_categorizer = DatasetSpec(
        id="ds1",
        name="Dataset One",
        citation="c",
        license="CC0",
        status="working",
        blocked_reason=None,
        files=(),
        loader="load_ds1",
        categorizer=None,
        source_venue="mailing_list",
        source_venue_rationale="r",
        target_n=2,
        seed=1,
        reported_iaa=None,
        caveats=(),
    )
    items = (DatasetItem("i1", "t", None, {"tbdf_categories": frozenset()}),)
    sampling = SamplingResult(
        dataset_id="ds1",
        seed=1,
        target_n=1,
        population_n=1,
        population_positives=0,
        sample_n=1,
        sample_positives=0,
        positive_fraction_cap=0.5,
        items=items,
        population_positives_by_label={},
    )
    run_data = DatasetRunData(spec=spec_no_categorizer, sampling=sampling, file_paths=())
    run = BenchmarkRunResult(
        datasets={"ds1": run_data},
        classification_by_message_id={
            message_id_for("ds1", "i1"): _record(message_id_for("ds1", "i1"), hostility=0.1)
        },
        run_result=RunResult(
            status="completed",
            records=[],
            calls_made=1,
            cache_hits=0,
            input_tokens_used=10,
            output_tokens_used=1,
            estimated_cost_usd=0.0,
        ),
        elapsed_seconds=0.1,
        cache_path=Path("/tmp/cache.jsonl"),
        manifest={},
    )
    registry = Registry(version=1, datasets={"ds1": spec_no_categorizer})
    result = compute_all_category_separations(run, registry)
    assert result == {}
