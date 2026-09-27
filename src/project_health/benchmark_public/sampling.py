"""Seeded, stratified sampling for `benchmark-public` (issue #89; DECISIONS.md
D23: "Use the item counts from the report's shortlist with seeded sampling.
Stratify so the positives in each mapped label are well represented, and
record the sampling so prevalence estimates can be corrected or flagged.").

Each dataset's full loaded population is usually far larger than the item
count `docs/plans/2026-09-27-public-benchmark-datasets.md`'s shortlist
recommends running through Jev (e.g. Wikipedia's 115,737 comments vs. a
5,000-item target) -- and the labels we most need to measure precision/recall
for (`personal_attack`, `hostility`, ...) are rare. A uniform random sample of
`target_n` from the full population would, at real-world base rates, draw too
few positives to estimate precision/recall meaningfully (COMMUNITY-HEALTH.md
§6.1 makes exactly this argument for the project's own frozen benchmark).

`stratified_sample` therefore splits the population into "positive" (positive
for at least one of the dataset's *gating or strong* mapped labels) and
"negative" pools, oversamples the positive pool up to a documented cap
(`positive_fraction_cap`, default 0.5 -- positives are at most half the
sample, so precision is still measured against a realistic mix of positives
and negatives, not an artificially all-positive set), and fills the rest from
the negative pool. Every draw is seeded (`random.Random(seed)`) for exact
reproducibility, and the returned `SamplingResult` records exactly how many
positives/negatives existed in the population and how many were drawn -- the
"record the sampling so prevalence estimates can be corrected" part of D23:
a downstream report can reweight (population_positives / population_size)
against (sampled_positives / sample_size) to recover an unbiased prevalence
estimate, which this module does not attempt itself (that's `evaluate.py`/
the public report's job; this module only records the counts needed to do
it).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from project_health.benchmark_public.loaders import DatasetItem
from project_health.benchmark_public.mapping import LabelMapping, is_positive

DEFAULT_POSITIVE_FRACTION_CAP = 0.5


@dataclass(frozen=True)
class SamplingResult:
    dataset_id: str
    seed: int
    target_n: int
    population_n: int
    population_positives: int
    sample_n: int
    sample_positives: int
    positive_fraction_cap: float
    items: tuple[DatasetItem, ...]
    # {our_label: population positive count}, computed once here (over every
    # non-synthetic mapping) and kept on the result so `evaluate.py` can
    # report an exact population prevalence per label without holding the
    # full (possibly 100k+ item) population in memory after sampling.
    population_positives_by_label: dict[str, int]

    def to_summary_dict(self) -> dict:
        """Aggregate-only summary (counts, no items) -- safe to put straight
        into the public report/JSON manifest."""
        return {
            "dataset_id": self.dataset_id,
            "seed": self.seed,
            "target_n": self.target_n,
            "population_n": self.population_n,
            "population_positive_rate": (
                self.population_positives / self.population_n if self.population_n else None
            ),
            "population_positives_by_label": dict(self.population_positives_by_label),
            "sample_n": self.sample_n,
            "sample_positives": self.sample_positives,
            "sample_positive_rate": (
                self.sample_positives / self.sample_n if self.sample_n else None
            ),
            "positive_fraction_cap": self.positive_fraction_cap,
        }


def _is_positive_any(item: DatasetItem, mappings: Sequence[LabelMapping]) -> bool:
    for mapping in mappings:
        if mapping.synthetic:
            continue
        if is_positive(mapping, item.raw_labels):
            return True
    return False


def stratified_sample(
    dataset_id: str,
    items: Sequence[DatasetItem],
    mappings: Sequence[LabelMapping],
    *,
    target_n: int,
    seed: int,
    positive_fraction_cap: float = DEFAULT_POSITIVE_FRACTION_CAP,
) -> SamplingResult:
    """Draw a seeded sample of up to `target_n` items from `items`,
    oversampling items positive for at least one *gating-or-strong* mapped
    label (see module docstring). `mappings` should be the dataset's own
    `LabelMapping` entries (from `mapping.py`); only non-synthetic ones are
    used to decide "positive" (a synthetic rollup is derived from real labels
    later, in `evaluate.py`, so it adds no new stratification information
    here).

    If `target_n >= len(items)`, the whole population is returned (no
    sampling needed) and the recorded sample counts equal the population
    counts.
    """
    rng = random.Random(seed)
    population_n = len(items)

    positives = [item for item in items if _is_positive_any(item, mappings)]
    negatives = [item for item in items if not _is_positive_any(item, mappings)]
    population_positives = len(positives)

    if target_n >= population_n:
        sampled = list(items)
    else:
        max_positives = min(len(positives), round(target_n * positive_fraction_cap))
        shuffled_positives = positives[:]
        rng.shuffle(shuffled_positives)
        chosen_positives = shuffled_positives[:max_positives]

        remaining = target_n - len(chosen_positives)
        shuffled_negatives = negatives[:]
        rng.shuffle(shuffled_negatives)
        chosen_negatives = shuffled_negatives[:remaining]

        # If there weren't enough negatives to fill the remainder, top up
        # from whatever positives are left over (keeps sample_n == target_n
        # whenever the population is large enough to support it at all).
        shortfall = remaining - len(chosen_negatives)
        topup: list[DatasetItem] = []
        if shortfall > 0:
            topup = shuffled_positives[len(chosen_positives) : len(chosen_positives) + shortfall]

        sampled = chosen_positives + chosen_negatives + topup
        rng.shuffle(sampled)

    sample_positives = sum(1 for item in sampled if _is_positive_any(item, mappings))

    population_positives_by_label: dict[str, int] = {}
    for mapping in mappings:
        if mapping.synthetic:
            continue
        population_positives_by_label[mapping.our_label] = sum(
            1 for item in items if is_positive(mapping, item.raw_labels) is True
        )

    return SamplingResult(
        dataset_id=dataset_id,
        seed=seed,
        target_n=target_n,
        population_n=population_n,
        population_positives=population_positives,
        sample_n=len(sampled),
        sample_positives=sample_positives,
        positive_fraction_cap=positive_fraction_cap,
        items=tuple(sampled),
        population_positives_by_label=population_positives_by_label,
    )
