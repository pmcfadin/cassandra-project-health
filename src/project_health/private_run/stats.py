"""Weighted rate + thread-level bootstrap CI for the private Cassandra
communication run (issue #110; COMMUNITY-HEALTH.md §5.2's per-1,000-
messages framing, extended here with survey inclusion weights).

A `ThreadCluster` is one sampled thread: its stratum inclusion weight
(`private_run.sample.StratumSample.weight`) plus every classified message
in it, each as a `{label_id: probability}` dict (a message missing a label
-- shouldn't happen for a v1 record, but handled defensively -- is simply
skipped for that label's aggregate).

The **expected rate per 1,000 messages** for a label is a weighted mean of
per-message probabilities (issue #110: "weighted mean Jev probability ×
1000"), threshold-free since no label has a calibrated production
threshold yet (`questions_v1.yaml`'s `threshold: null`). Because *threads*,
not messages, were sampled, every message in a sampled thread is weighted
by its thread's inclusion weight (population / sampled) so the resulting
rate estimates the population, not just this run's own sample.

Its 95% CI is a nonparametric **thread-level (cluster) bootstrap**:
resampling whole threads with replacement, never individual messages,
since messages within one sampled thread are not independent draws from
the population of threads (`docs/spec/SCORING.md` §7's bootstrap
convention, applied at the thread level per issue #110's explicit "thread-
level bootstrap 95% CI"). No `numpy`/`scipy` dependency, matching
`pilot/stats.py`'s own convention -- these are the only two places in this
project doing a nonparametric bootstrap, and both keep it pure Python.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

DEFAULT_BOOTSTRAP_ITERATIONS = 1000


@dataclass(frozen=True)
class ThreadCluster:
    thread_id: str
    weight: float
    messages: tuple[dict[str, float], ...] = field(default_factory=tuple)


def message_count(clusters: list[ThreadCluster]) -> int:
    """The raw (unweighted) count of classified messages across `clusters`
    -- what COMMUNITY-HEALTH.md §5.1's minimum-sample floor is checked
    against (`aggregate.py`), not the weighted/population estimate."""
    return sum(len(c.messages) for c in clusters)


def weighted_rate_per_1000(clusters: list[ThreadCluster], label_id: str) -> float:
    """The weighted-mean-probability-x-1000 rate for `label_id` across
    `clusters`. `0.0` if no message in `clusters` carries `label_id` at
    all (an empty-denominator convention matching `pilot.stats`'s
    `precision_recall_f1`)."""
    weighted_sum = 0.0
    weighted_n = 0.0
    for cluster in clusters:
        for message in cluster.messages:
            probability = message.get(label_id)
            if probability is None:
                continue
            weighted_sum += cluster.weight * probability
            weighted_n += cluster.weight
    return (1000.0 * weighted_sum / weighted_n) if weighted_n else 0.0


def weighted_sensitivity_rate_per_1000(
    clusters: list[ThreadCluster], label_id: str, threshold: float
) -> float:
    """Like `weighted_rate_per_1000`, but counting a message as "present"
    only when its probability clears `threshold` (the label's public-
    benchmark best-F1 threshold, `sensitivity.py`) rather than using the
    raw probability directly -- issue #110's sensitivity column for labels
    with a strong public mapping."""
    weighted_present = 0.0
    weighted_n = 0.0
    for cluster in clusters:
        for message in cluster.messages:
            probability = message.get(label_id)
            if probability is None:
                continue
            if probability >= threshold:
                weighted_present += cluster.weight
            weighted_n += cluster.weight
    return (1000.0 * weighted_present / weighted_n) if weighted_n else 0.0


def percentile_interval(values: list[float], alpha: float = 0.05) -> tuple[float, float]:
    """The `(alpha/2, 1-alpha/2)` percentile interval of `values`. `(0.0,
    0.0)` for an empty sequence -- nothing to bound."""
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0, 0.0
    lo_idx = max(0, min(n - 1, round((alpha / 2) * (n - 1))))
    hi_idx = max(0, min(n - 1, round((1 - alpha / 2) * (n - 1))))
    return ordered[lo_idx], ordered[hi_idx]


def bootstrap_weighted_rate_ci(
    clusters: list[ThreadCluster],
    label_id: str,
    *,
    seed: int,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """The 95% (or `1-alpha`) thread-level bootstrap CI for `label_id`'s
    weighted rate across `clusters`: resample the *thread list* (not
    individual messages) with replacement `iterations` times, recompute
    `weighted_rate_per_1000` each time (each resampled thread keeps its own
    weight and its own messages intact), and report the percentile
    interval. `(0.0, 0.0)` with zero clusters -- nothing to resample.
    """
    n = len(clusters)
    if n == 0:
        return 0.0, 0.0
    rng = random.Random(seed)
    rates: list[float] = []
    for _ in range(iterations):
        resampled = [clusters[rng.randrange(n)] for _ in range(n)]
        rates.append(weighted_rate_per_1000(resampled, label_id))
    return percentile_interval(rates, alpha)


def bootstrap_multi_metric_ci(
    clusters: list[ThreadCluster],
    label_id: str,
    thresholds: Sequence[float],
    *,
    seed: int,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    alpha: float = 0.05,
) -> dict[str, tuple[float, float]]:
    """The weighted-mean rate's CI *and* every `thresholds` cutoff rate's
    CI for `label_id`, all from **one shared bootstrap resample per
    iteration** (issue #110 fixup round 1: reporting fixed-cutoff counts at
    0.5/0.7/0.9 *and* the mean-probability "probability index" for every
    label would otherwise mean 4 independent bootstraps per label per cell
    -- generating the resampled thread list is the expensive part, so
    computing all 4 statistics from the same resample cuts that back to 1x,
    with no loss of validity: each statistic is still evaluated
    independently over each resample, exactly as `bootstrap_weighted_rate_
    ci` does for one metric at a time).

    Returns `{"mean": (lo, hi), "<cutoff>": (lo, hi), ...}` -- cutoffs are
    stringified (`f"{t:.1f}"`) so the result is directly JSON-serializable
    without a float-key workaround. `(0.0, 0.0)` everywhere with zero
    clusters.
    """
    n = len(clusters)
    empty = (0.0, 0.0)
    if n == 0:
        return {"mean": empty, **{f"{t:.1f}": empty for t in thresholds}}
    rng = random.Random(seed)
    mean_samples: list[float] = []
    cutoff_samples: dict[float, list[float]] = {t: [] for t in thresholds}
    for _ in range(iterations):
        resampled = [clusters[rng.randrange(n)] for _ in range(n)]
        mean_samples.append(weighted_rate_per_1000(resampled, label_id))
        for threshold in thresholds:
            cutoff_samples[threshold].append(
                weighted_sensitivity_rate_per_1000(resampled, label_id, threshold)
            )
    return {
        "mean": percentile_interval(mean_samples, alpha),
        **{
            f"{threshold:.1f}": percentile_interval(samples, alpha)
            for threshold, samples in cutoff_samples.items()
        },
    }


def median_probability_per_1000(clusters: list[ThreadCluster], label_id: str) -> float:
    """The **unweighted** median of every message's raw `label_id`
    probability across `clusters`, x1000 -- issue #110 fixup round 1's
    "probability index floor": how much of the (weighted) probability-index
    secondary metric is just the classifier's typical baseline output for
    this label, not real prevalence signal. Deliberately unweighted, unlike
    every other statistic in this module: this is a diagnostic about the
    classifier's raw output distribution, not a population estimate, so
    survey inclusion weights would only obscure "what does a typical
    message's probability actually look like". `0.0` if no message in
    `clusters` carries `label_id` at all.
    """
    probabilities = [
        message[label_id]
        for cluster in clusters
        for message in cluster.messages
        if label_id in message
    ]
    if not probabilities:
        return 0.0
    return statistics.median(probabilities) * 1000.0
