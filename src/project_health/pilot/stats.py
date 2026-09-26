"""Pure-Python statistics for the Jev pilot evaluation (issue #47).

No `numpy`/`scipy` dependency -- this project's `pyproject.toml` doesn't
carry either, and the pilot's item counts (a few hundred) make a pure-Python
implementation fast enough. Every function here is a deterministic,
seeded-where-applicable, offline computation over already-loaded data; none
of them touch the network or a model (D22).

Conventions used throughout this module:

- A binary ground-truth/prediction pair is `(y_true, y_prob)`: `y_true` is
  `1` (label present), `0` (label absent) -- items with no usable ground
  truth (all raters "unsure", or a tie between raters with no unsure vote,
  see `evaluate.aggregate_human_mark`) are never included in `y_true`/
  `y_prob` pairs at all, not encoded as a third value.
- "Present" at a threshold means `y_prob >= threshold` (COMMUNITY-HEALTH.md
  §4.5: "present/absent is a code-side decision, made by applying each
  label's threshold... to the stored probability").
- Precision/recall/F1 are defined as `0.0` when their denominator is `0`
  (e.g. no predicted positives at a very high threshold) rather than
  raising or returning `None` -- this matches the common scikit-learn
  `zero_division=0` convention and keeps every threshold in a sweep
  plottable without special-casing.
- Bootstrap CIs follow `docs/spec/SCORING.md` §7's convention
  (resample-with-replacement, recompute, percentile bounds) but at the 95%
  level issue #47 asks for explicitly (SCORING.md's default 90%/1000-iter
  convention is for dashboard metrics generally; the pilot evaluation's own
  spec is explicit about 95%).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Hashable, Sequence

DEFAULT_BOOTSTRAP_ITERATIONS = 1000
DEFAULT_THRESHOLD_GRID: tuple[float, ...] = tuple(round(i / 100, 2) for i in range(5, 100, 5))


# --- Precision / recall / F1 ---------------------------------------------------------


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.tn + self.fn


def confusion_at_threshold(
    y_true: Sequence[int], y_prob: Sequence[float], threshold: float
) -> Confusion:
    """Confusion counts at `threshold` for paired, equal-length `y_true`
    (0/1) and `y_prob` ([0,1]) sequences. `y_prob >= threshold` is "present"
    (§4.5's code-side thresholding rule)."""
    if len(y_true) != len(y_prob):
        raise ValueError("y_true and y_prob must be the same length")
    tp = fp = tn = fn = 0
    for truth, prob in zip(y_true, y_prob):
        predicted = 1 if prob >= threshold else 0
        if predicted == 1 and truth == 1:
            tp += 1
        elif predicted == 1 and truth == 0:
            fp += 1
        elif predicted == 0 and truth == 0:
            tn += 1
        else:
            fn += 1
    return Confusion(tp=tp, fp=fp, tn=tn, fn=fn)


def precision_recall_f1(confusion: Confusion) -> tuple[float, float, float]:
    """`(precision, recall, f1)`, each `0.0` if its denominator is `0`."""
    pred_pos = confusion.tp + confusion.fp
    actual_pos = confusion.tp + confusion.fn
    precision = confusion.tp / pred_pos if pred_pos else 0.0
    recall = confusion.tp / actual_pos if actual_pos else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    confusion: Confusion
    precision: float
    recall: float
    f1: float
    precision_ci: tuple[float, float]
    recall_ci: tuple[float, float]
    f1_ci: tuple[float, float]


def bootstrap_ci_at_threshold(
    y_true: Sequence[int],
    y_prob: Sequence[float],
    threshold: float,
    *,
    seed: int,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    alpha: float = 0.05,
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """Nonparametric bootstrap 95% (or `1-alpha`) CIs for precision, recall,
    and F1 at `threshold`: resample `(y_true, y_prob)` pairs with
    replacement `iterations` times (seeded via `random.Random(seed)` for
    reproducibility), recompute each metric, and report the
    `alpha/2`/`1-alpha/2` percentile of the resampled distribution
    (`docs/spec/SCORING.md` §7's bootstrap convention). Returns
    `((p_lo, p_hi), (r_lo, r_hi), (f1_lo, f1_hi))`.

    With zero usable items, every bound is `(0.0, 0.0)` -- there is nothing
    to resample.
    """
    n = len(y_true)
    if n == 0:
        return (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)
    rng = random.Random(seed)
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    for _ in range(iterations):
        indices = [rng.randrange(n) for _ in range(n)]
        sample_true = [y_true[i] for i in indices]
        sample_prob = [y_prob[i] for i in indices]
        confusion = confusion_at_threshold(sample_true, sample_prob, threshold)
        p, r, f1 = precision_recall_f1(confusion)
        precisions.append(p)
        recalls.append(r)
        f1s.append(f1)
    return (
        _percentile_interval(precisions, alpha),
        _percentile_interval(recalls, alpha),
        _percentile_interval(f1s, alpha),
    )


def _percentile_interval(values: list[float], alpha: float) -> tuple[float, float]:
    ordered = sorted(values)
    n = len(ordered)
    lo_idx = max(0, min(n - 1, round((alpha / 2) * (n - 1))))
    hi_idx = max(0, min(n - 1, round((1 - alpha / 2) * (n - 1))))
    return ordered[lo_idx], ordered[hi_idx]


def sweep_thresholds(
    y_true: Sequence[int],
    y_prob: Sequence[float],
    *,
    thresholds: Sequence[float] = DEFAULT_THRESHOLD_GRID,
    seed: int,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    alpha: float = 0.05,
) -> list[ThresholdMetrics]:
    """Precision/recall/F1 (with bootstrap CIs) at every threshold in
    `thresholds`. A distinct, threshold-derived seed (`seed + round(t*1000)`)
    is used per threshold so the sweep is reproducible end to end from one
    top-level `seed` without every threshold's resample drawing the exact
    same index sequence."""
    results: list[ThresholdMetrics] = []
    for threshold in thresholds:
        confusion = confusion_at_threshold(y_true, y_prob, threshold)
        precision, recall, f1 = precision_recall_f1(confusion)
        threshold_seed = seed + round(threshold * 1000)
        p_ci, r_ci, f1_ci = bootstrap_ci_at_threshold(
            y_true, y_prob, threshold, seed=threshold_seed, iterations=iterations, alpha=alpha
        )
        results.append(
            ThresholdMetrics(
                threshold=threshold,
                confusion=confusion,
                precision=precision,
                recall=recall,
                f1=f1,
                precision_ci=p_ci,
                recall_ci=r_ci,
                f1_ci=f1_ci,
            )
        )
    return results


def best_threshold_by_f1(sweep: Sequence[ThresholdMetrics]) -> ThresholdMetrics:
    """The sweep entry maximizing F1, ties broken by higher precision (the
    asymmetric, precision-weighted framing COMMUNITY-HEALTH.md §6.4 uses for
    every gate group), then by the lowest threshold (prefer the more
    permissive, simpler cutoff among equivalent choices)."""
    if not sweep:
        raise ValueError("sweep must not be empty")
    return max(sweep, key=lambda m: (m.f1, m.precision, -m.threshold))


# --- Reliability diagram (calibration, §6.5) ------------------------------------------


@dataclass(frozen=True)
class ReliabilityBin:
    bin_lo: float
    bin_hi: float
    n: int
    mean_predicted: float | None
    observed_rate: float | None


def reliability_bins(
    y_true: Sequence[int], y_prob: Sequence[float], *, num_bins: int = 10
) -> list[ReliabilityBin]:
    """Predicted-probability bins of width `1/num_bins` vs. the observed
    positive rate within each bin (COMMUNITY-HEALTH.md §6.5's reliability
    diagram). A bin with no items has `n=0` and `None` for both the mean
    predicted probability and the observed rate, rather than being omitted
    -- an empty bin is itself informative (no calibration evidence there)."""
    edges = [i / num_bins for i in range(num_bins + 1)]
    bins: list[ReliabilityBin] = []
    for i in range(num_bins):
        lo, hi = edges[i], edges[i + 1]
        in_bin = [
            (t, p)
            for t, p in zip(y_true, y_prob)
            if (p >= lo and (p < hi or (i == num_bins - 1 and p <= hi)))
        ]
        if not in_bin:
            bins.append(
                ReliabilityBin(bin_lo=lo, bin_hi=hi, n=0, mean_predicted=None, observed_rate=None)
            )
            continue
        mean_predicted = sum(p for _, p in in_bin) / len(in_bin)
        observed_rate = sum(t for t, _ in in_bin) / len(in_bin)
        bins.append(
            ReliabilityBin(
                bin_lo=lo,
                bin_hi=hi,
                n=len(in_bin),
                mean_predicted=mean_predicted,
                observed_rate=observed_rate,
            )
        )
    return bins


# --- Prevalence (Wilson score interval) ------------------------------------------------


def wilson_interval(successes: int, n: int, *, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score 95% (default `z=1.96`) confidence interval for a
    binomial proportion `successes/n` -- the standard closed-form interval
    for a proportion, well-behaved (unlike the naive normal-approximation
    interval) at the small n and near-0/near-1 proportions a rare label's
    prevalence estimate is likely to produce. `(0.0, 0.0)` for `n == 0`
    (nothing to estimate)."""
    if n == 0:
        return 0.0, 0.0
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    lo = (center - margin) / denom
    hi = (center + margin) / denom
    return max(0.0, lo), min(1.0, hi)


# --- Tone agreement: exact, off-by-one, weighted kappa ----------------------------------


def exact_agreement_rate(a: Sequence[int], b: Sequence[int]) -> float:
    if len(a) != len(b):
        raise ValueError("sequences must be the same length")
    if not a:
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def off_by_one_rate(a: Sequence[int], b: Sequence[int]) -> float:
    if len(a) != len(b):
        raise ValueError("sequences must be the same length")
    if not a:
        return 0.0
    return sum(1 for x, y in zip(a, b) if abs(x - y) <= 1) / len(a)


def weighted_kappa(a: Sequence[int], b: Sequence[int], levels: Sequence[int]) -> float:
    """Quadratic weighted kappa (Cohen, 1968) between two raters' ordinal
    ratings over `levels` (e.g. `questions_v1.yaml`'s `tone_intensity`
    levels, 0-4 -- pass the *full* defined scale, not just the observed
    values, so the weight matrix reflects the real ordinal distance between
    levels even when one rater/dataset happens not to use the full range).
    `1.0` (trivially perfect -- there is no variability in the scale itself
    to disagree over) if `levels` collapses to fewer than 2 distinct values.
    `0.0` (no agreement signal) if either rater's ratings have zero variance
    and the observed/expected disagreement is degenerate (`0/0`), and `0.0`
    with fewer than 2 paired items."""
    if len(a) != len(b):
        raise ValueError("sequences must be the same length")
    n = len(a)
    if n < 2:
        return 0.0
    levels = sorted(set(levels))
    k = len(levels)
    if k < 2:
        return 1.0
    index = {level: i for i, level in enumerate(levels)}
    weights = [[((i - j) ** 2) / ((k - 1) ** 2) for j in range(k)] for i in range(k)]

    observed = [[0] * k for _ in range(k)]
    for x, y in zip(a, b):
        observed[index[x]][index[y]] += 1

    row_marg = [sum(row) for row in observed]
    col_marg = [sum(observed[i][j] for i in range(k)) for j in range(k)]

    observed_disagreement = (
        sum(observed[i][j] * weights[i][j] for i in range(k) for j in range(k)) / n
    )
    expected_disagreement = sum(
        row_marg[i] * col_marg[j] * weights[i][j] for i in range(k) for j in range(k)
    ) / (n * n)

    if expected_disagreement == 0:
        return 0.0
    return 1 - (observed_disagreement / expected_disagreement)


# --- Krippendorff's alpha (nominal, missing-data-tolerant, §6.3) -----------------------


def krippendorff_alpha_nominal(
    reliability_data: Sequence[Sequence[Hashable | None]],
) -> float | None:
    """Krippendorff's alpha for nominal data (Krippendorff, 2011,
    "Computing Krippendorff's Alpha-Reliability"), one row per item
    (unit), one column per rater, `None` for a missing/excluded rating
    (this project's own convention: an "unsure" mark is excluded, i.e.
    treated as missing, per COMMUNITY-HEALTH.md §1.2's ratable labels and
    issue #47's "human 'unsure' labels are excluded from that label's
    scoring").

    Returns `None` (not `0.0` -- "no agreement statistic exists" per issue
    #47, distinct from a real alpha of exactly 0) if fewer than 2 items have
    at least 2 non-missing ratings, since alpha is undefined with no
    pairable data at all.

    Binary present/absent labels are nominal with only two categories, so
    the nominal-metric (`delta(c, k) = 0 if c == k else 1`) form used here
    is exactly what COMMUNITY-HEALTH.md §6.3 calls for ("each label is its
    own binary reliability problem").
    """
    # Coincidence matrix: o[c][k] = sum over units of (# of ordered pairs of
    # ratings (c, k) within that unit) / (m_u - 1), for units with m_u >= 2
    # non-missing ratings.
    categories: set[Hashable] = set()
    pairable_units: list[list[Hashable]] = []
    for row in reliability_data:
        values = [v for v in row if v is not None]
        if len(values) >= 2:
            pairable_units.append(values)
            categories.update(values)

    if len(pairable_units) < 2:
        return None

    categories_list = sorted(categories, key=str)
    cat_index = {c: i for i, c in enumerate(categories_list)}
    k = len(categories_list)
    o = [[0.0] * k for _ in range(k)]

    for values in pairable_units:
        m_u = len(values)
        weight = 1.0 / (m_u - 1)
        for i, vi in enumerate(values):
            for j, vj in enumerate(values):
                if i == j:
                    continue
                o[cat_index[vi]][cat_index[vj]] += weight

    n = sum(sum(row) for row in o)
    if n == 0:
        return None
    n_c = [sum(o[c][k_] for k_ in range(k)) for c in range(k)]

    def delta(c: int, k_: int) -> float:
        return 0.0 if c == k_ else 1.0

    d_o = sum(o[c][k_] * delta(c, k_) for c in range(k) for k_ in range(k)) / n
    d_e_denom = n * (n - 1)
    if d_e_denom == 0:
        return None
    d_e = sum(n_c[c] * n_c[k_] * delta(c, k_) for c in range(k) for k_ in range(k)) / d_e_denom

    if d_e == 0:
        # No expected disagreement at all (e.g. every rater always agrees
        # with every other on every category) -- perfect agreement, alpha=1.
        return 1.0
    return 1 - (d_o / d_e)


# --- Rater time (minutes per message) --------------------------------------------------


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile (`p` in `[0, 100]`), `0.0` for an
    empty sequence."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[int(rank)]
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


# --- Full-benchmark size estimate -------------------------------------------------------


def required_n_for_ci_half_width(
    proportion: float, half_width: float = 0.1, z: float = 1.96
) -> int:
    """The sample size needed for a normal-approximation binomial CI half-
    width `<= half_width` at proportion `proportion` and confidence `z`
    (default `1.96` ~= 95%): `n = ceil(z^2 * p * (1-p) / E^2)` (standard
    proportion sample-size formula; documented explicitly, per issue #47,
    as the rule this project's full-benchmark size recommendation uses).
    `proportion` is clamped to `[0.01, 0.99]` first so a label with an
    observed prevalence/F1 of exactly 0 or 1 in the pilot (very plausible at
    n~192) still yields a finite, non-zero size estimate rather than `0`
    (which would understate how much more data a boundary observation
    actually needs).
    """
    p = min(0.99, max(0.01, proportion))
    return math.ceil((z * z) * p * (1 - p) / (half_width * half_width))
