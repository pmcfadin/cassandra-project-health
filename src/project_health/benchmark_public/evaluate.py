"""Per dataset x label evaluation for `benchmark-public` (issue #89;
DECISIONS.md D23; COMMUNITY-HEALTH.md §6). Reuses
`project_health.pilot.stats` exactly as the Phase 2a pilot does (issue #47) --
same threshold sweep, bootstrap CIs, and reliability-bin machinery, applied
here per (dataset, mapped label) instead of per pilot label.

Ground truth for every item is already 100% known (these are fully
human-labeled public datasets, unlike the pilot's partial rater coverage), so
unlike `pilot/evaluate.py` there is no "excluded as unsure/tie" case here --
every sampled item either has its mapped `raw_field` present (scored) or
doesn't (skipped -- `n_candidates - n_scored`).

Because ground truth is fully known for the *entire* downloaded dataset, not
just the sample, this module reports two distinct prevalence figures per
label: the **sample** prevalence (what was actually classified -- inflated by
`sampling.py`'s deliberate positive-oversampling) and the **population**
prevalence (the dataset's real base rate, computed once during sampling and
carried on `SamplingResult.population_positives_by_label` -- see that
module's docstring). The public report is explicit that only the population
figure is a real-world base-rate estimate (D23: "record the sampling so
prevalence estimates can be corrected or flagged").

`questions_v1.yaml` currently pins every label's `threshold: null` (calibrated
later, issue #47) -- there is no existing §6.4 gate *threshold value* to
evaluate at yet. Each `DatasetLabelEvaluation` records that explicitly
(`gate_threshold_value` is always `None` today) rather than inventing one;
`report.py` renders this as "no calibrated threshold exists yet" per label,
exactly per issue #89's "the value at the §6.4 gate threshold if the spec has
one" -- it doesn't, yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from project_health.benchmark_public.categorize import get_categorizer
from project_health.benchmark_public.loaders import DatasetItem
from project_health.benchmark_public.mapping import LabelMapping, LabelMappingSet, is_positive
from project_health.benchmark_public.registry import Registry
from project_health.benchmark_public.runner import BenchmarkRunResult, item_id_from_message_id
from project_health.classify.classifier import ClassificationRecord
from project_health.pilot import stats

DEFAULT_SEED = 89  # this issue's number -- an arbitrary but fixed, documented base seed

# The four message-level labels the orchestrator review of issue #89 calls
# "our 4 negative labels" for the "separation by source category" table:
# personal_attack/hostility/dismissiveness/sarcasm are the labels a dataset's
# own fine-grained incivility-adjacent categories (Ferreira's TBDF, ToxiCR's
# is_toxic, TalkDown's condescension label, Wikipedia's attack label) are
# plausibly evidence for. `gatekeeping`/`status_authority_invocation` are
# left out -- no dataset in this shortlist maps to either at all (D23).
NEGATIVE_LABELS: tuple[str, ...] = ("personal_attack", "hostility", "dismissiveness", "sarcasm")

# No label has a calibrated threshold yet (questions_v1.yaml v1: `threshold:
# null` for all 12 labels, pending issue #47's calibration). Kept as a named
# constant (rather than a bare `None` scattered through this module) so a
# future change that *does* wire up a calibrated threshold has one place to
# start from.
GATE_THRESHOLD_VALUE: float | None = None


@dataclass(frozen=True)
class DatasetLabelEvaluation:
    dataset_id: str
    our_label: str
    mapping: LabelMapping
    n_candidates: int  # sampled items Jev actually classified
    n_scored: int  # of those, items with a usable raw_field value
    n_positives: int  # ground-truth positives within the *scored sample*
    sample_n: int
    population_n: int
    population_positives: int | None  # None for a synthetic rollup (no single raw field)
    sweep: list[stats.ThresholdMetrics]
    best: stats.ThresholdMetrics
    reliability: list[stats.ReliabilityBin]
    gate_threshold_value: float | None
    at_gate_threshold: stats.ThresholdMetrics | None


def _predicted_probability(record: ClassificationRecord, mapping: LabelMapping) -> float | None:
    if mapping.synthetic:
        probs = [
            record.labels[label].probability
            for label in mapping.source_labels
            if label in record.labels
        ]
        return max(probs) if probs else None
    label = record.labels.get(mapping.our_label)
    return label.probability if label is not None else None


def evaluate_dataset_label(
    dataset_id: str,
    mapping: LabelMapping,
    sampled_items: Sequence[DatasetItem],
    classification_by_item_id: dict[str, ClassificationRecord],
    *,
    population_n: int,
    population_positives: int | None,
    seed: int,
    bootstrap_iterations: int = stats.DEFAULT_BOOTSTRAP_ITERATIONS,
) -> DatasetLabelEvaluation | None:
    """`None` if no sampled item both has a classification record and a
    usable ground-truth value for `mapping` (e.g. the mapped raw field is
    absent from every item -- nothing to evaluate)."""
    y_true: list[int] = []
    y_prob: list[float] = []
    n_candidates = 0

    for item in sampled_items:
        record = classification_by_item_id.get(item.item_id)
        if record is None:
            continue
        n_candidates += 1
        prob = _predicted_probability(record, mapping)
        if prob is None:
            continue
        positive = is_positive(mapping, item.raw_labels)
        if positive is None:
            continue
        y_true.append(1 if positive else 0)
        y_prob.append(prob)

    if not y_true:
        return None

    sweep = stats.sweep_thresholds(y_true, y_prob, seed=seed, iterations=bootstrap_iterations)
    best = stats.best_threshold_by_f1(sweep)
    reliability = stats.reliability_bins(y_true, y_prob)

    at_gate_threshold = None
    if GATE_THRESHOLD_VALUE is not None:
        at_gate_threshold = next((m for m in sweep if m.threshold == GATE_THRESHOLD_VALUE), None)

    return DatasetLabelEvaluation(
        dataset_id=dataset_id,
        our_label=mapping.our_label,
        mapping=mapping,
        n_candidates=n_candidates,
        n_scored=len(y_true),
        n_positives=sum(y_true),
        sample_n=len(sampled_items),
        population_n=population_n,
        population_positives=population_positives,
        sweep=sweep,
        best=best,
        reliability=reliability,
        gate_threshold_value=GATE_THRESHOLD_VALUE,
        at_gate_threshold=at_gate_threshold,
    )


def evaluate_benchmark(
    run: BenchmarkRunResult,
    mapping_set: LabelMappingSet,
    *,
    seed: int = DEFAULT_SEED,
    bootstrap_iterations: int = stats.DEFAULT_BOOTSTRAP_ITERATIONS,
) -> dict[str, list[DatasetLabelEvaluation]]:
    """`{dataset_id: [DatasetLabelEvaluation, ...]}` for every (dataset,
    mapped label) pair with at least one scorable item, across every dataset
    `run` actually classified. A distinct, index-derived seed per (dataset,
    label) keeps the whole benchmark's bootstrap reproducible end to end from
    one top-level `seed`, exactly like `pilot/evaluate.py`'s per-label
    seeding."""
    results: dict[str, list[DatasetLabelEvaluation]] = {}
    index = 0
    for dataset_id, run_data in run.datasets.items():
        classification_by_item_id: dict[str, ClassificationRecord] = {}
        for message_id, record in run.classification_by_message_id.items():
            item_id = item_id_from_message_id(dataset_id, message_id)
            if item_id is not None:
                classification_by_item_id[item_id] = record

        dataset_evaluations: list[DatasetLabelEvaluation] = []
        for mapping in mapping_set.for_dataset(dataset_id):
            evaluation = evaluate_dataset_label(
                dataset_id,
                mapping,
                run_data.sampling.items,
                classification_by_item_id,
                population_n=run_data.sampling.population_n,
                population_positives=(
                    None
                    if mapping.synthetic
                    else run_data.sampling.population_positives_by_label.get(mapping.our_label)
                ),
                seed=seed + index * 1000,
                bootstrap_iterations=bootstrap_iterations,
            )
            index += 1
            if evaluation is not None:
                dataset_evaluations.append(evaluation)
        if dataset_evaluations:
            results[dataset_id] = dataset_evaluations
    return results


# --- Separation by source category (orchestrator review of issue #89) ------------------


@dataclass(frozen=True)
class CategorySeparation:
    """Mean Jev probability of each of `NEGATIVE_LABELS`, for every item
    tagged with one dataset-specific fine-grained category (`categorize.py`).
    Aggregate-only (a category name and counts/means, never an item id or
    text) -- safe to render directly in the public report."""

    dataset_id: str
    category: str
    n: int
    mean_probability: dict[str, float | None]


def compute_category_separation(
    dataset_id: str,
    categorizer_name: str,
    sampled_items: Sequence[DatasetItem],
    classification_by_item_id: dict[str, ClassificationRecord],
    *,
    labels: tuple[str, ...] = NEGATIVE_LABELS,
) -> list[CategorySeparation]:
    """One `CategorySeparation` per distinct category name the dataset's
    registered categorizer produces (`registry.py`'s `DatasetSpec.
    categorizer`), ordered by descending item count. An item belonging to
    several categories at once (Ferreira's `tbdf_categories`) is counted
    under each -- see `categorize.py`'s module docstring."""
    categorizer = get_categorizer(categorizer_name)
    sums: dict[str, dict[str, float]] = {}
    counts: dict[str, int] = {}

    for item in sampled_items:
        record = classification_by_item_id.get(item.item_id)
        if record is None:
            continue
        for category in categorizer(item.raw_labels):
            counts[category] = counts.get(category, 0) + 1
            label_sums = sums.setdefault(category, {label: 0.0 for label in labels})
            for label in labels:
                answer = record.labels.get(label)
                if answer is not None:
                    label_sums[label] += answer.probability

    results: list[CategorySeparation] = []
    for category, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        mean_probability = {label: (sums[category][label] / n) if n else None for label in labels}
        results.append(
            CategorySeparation(
                dataset_id=dataset_id, category=category, n=n, mean_probability=mean_probability
            )
        )
    return results


def compute_all_category_separations(
    run: BenchmarkRunResult, registry: Registry
) -> dict[str, list[CategorySeparation]]:
    """`{dataset_id: [CategorySeparation, ...]}` for every dataset `run`
    classified that has a registered categorizer (`registry.py`'s
    `DatasetSpec.categorizer` -- optional; a dataset with none is simply
    absent from the result, not an error)."""
    results: dict[str, list[CategorySeparation]] = {}
    for dataset_id, run_data in run.datasets.items():
        categorizer_name = run_data.spec.categorizer
        if categorizer_name is None:
            continue
        classification_by_item_id: dict[str, ClassificationRecord] = {}
        for message_id, record in run.classification_by_message_id.items():
            item_id = item_id_from_message_id(dataset_id, message_id)
            if item_id is not None:
                classification_by_item_id[item_id] = record
        separations = compute_category_separation(
            dataset_id, categorizer_name, run_data.sampling.items, classification_by_item_id
        )
        if separations:
            results[dataset_id] = separations
    return results
