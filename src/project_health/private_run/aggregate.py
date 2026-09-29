"""Per-cell (venue × quarter, venue × year) aggregation for the private
Cassandra communication run (issue #110; COMMUNITY-HEALTH.md §5.1's
minimum-sample floors, §5.2's per-1,000-messages framing, D23's sensitivity
mappings).

A "cell" is one (venue, quarter), (venue, year), or pooled trend-window
bucket: a raw (unweighted) message/distinct-author count for the §5.1
floor check, plus, for every one of the 12 message-level labels:

- **Headline: fixed-cutoff counts** (issue #110 fixup round 1). "Weighted
  mean probability x 1000" alone is not a rate -- every message carries a
  small residual probability for every label, so that number has a floor
  even where a label essentially never applies (the smoke run's own
  `gatekeeping` read ~11.5 per 1,000 messages in every cell, an artifact,
  not prevalence). The headline numbers are instead **counts of messages
  at or above fixed probability cutoffs 0.5/0.7/0.9** (`CUTOFFS`), each
  with its own thread-level bootstrap 95% CI, still survey-weighted by
  thread inclusion weight. These are explicitly **uncalibrated** -- no
  label has a Cassandra-calibrated production threshold yet (issue #47) --
  and are labeled as such everywhere they're rendered (`report.py`).
- **Probability index** (secondary, uncalibrated, trend-only): the original
  weighted-mean-probability-x-1000 number, kept for continuity/trend
  reading but demoted to secondary status and paired with `runner.py`'s
  run-wide "probability index floor" (the median message probability x
  1000, computed once across the whole run) so a reader can see how much
  of the index is just baseline.
- **Public-benchmark sensitivity** (`STRONG_GATING_LABELS` only): the count
  at the D23 strong+gating public-benchmark best-F1 threshold
  (`sensitivity.py`), now carrying which dataset that threshold came from
  and whether it's a permissive cutoff.

Below either §5.1 floor, every per-label statistic renders as `None` --
only the counts themselves are shown -- so a reader can see *why* a cell
renders as `insufficient data` (COMMUNITY-HEALTH.md §5.1: never silently
omitted).
"""

from __future__ import annotations

import hashlib
from typing import Any

from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.private_run import stats
from project_health.private_run.sensitivity import STRONG_GATING_LABELS, SensitivityThreshold

# Issue #110, verbatim: "Apply §5.1 floors (>=30 messages and >=10 distinct
# authors per cell, else `insufficient data`)."
MIN_MESSAGES = 30
MIN_DISTINCT_AUTHORS = 10

# Issue #110 fixup round 1: fixed, uncalibrated probability cutoffs every
# label is reported at. 0.5 is the headline; 0.7/0.9 are the "how much
# stricter does it get" sensitivity cutoffs.
CUTOFFS: tuple[float, ...] = (0.5, 0.7, 0.9)
HEADLINE_CUTOFF = 0.5

DEFAULT_BOOTSTRAP_ITERATIONS = stats.DEFAULT_BOOTSTRAP_ITERATIONS


def _cell_seed(base_seed: int, cell_key: str, label_index: int) -> int:
    """A deterministic, reproducible per-(cell, label) bootstrap seed,
    derived from `base_seed` -- so every label in every cell resamples
    independently (never the identical bootstrap draw sequence reused
    across cells/labels) while the whole run stays fully reproducible from
    one top-level `--seed`. `hashlib` rather than Python's builtin `hash()`
    on purpose: `hash()` is salted per-process (`PYTHONHASHSEED`) and would
    make two runs with the same `--seed` disagree on their bootstrap CIs.
    """
    digest = hashlib.sha256(f"{base_seed}:{cell_key}:{label_index}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def cutoff_key(cutoff: float) -> str:
    return f"{cutoff:.1f}"


def aggregate_cell(
    clusters: list[stats.ThreadCluster],
    distinct_authors: set[str],
    *,
    seed: int,
    cell_key: str,
    sensitivity_thresholds: dict[str, SensitivityThreshold],
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    threads_population: int = 0,
    threads_sampled: int = 0,
) -> dict[str, Any]:
    """One cell's full aggregate: counts, the §5.1 floor verdict, every
    label's fixed-cutoff counts (headline + sensitivity cutoffs) and
    probability index (secondary), and the public-benchmark sensitivity
    column for `STRONG_GATING_LABELS`.
    """
    n_messages = stats.message_count(clusters)
    n_authors = len(distinct_authors)
    insufficient = n_messages < MIN_MESSAGES or n_authors < MIN_DISTINCT_AUTHORS

    cutoff_rates: dict[str, dict[str, dict[str, Any] | None] | None] = {}
    probability_index: dict[str, dict[str, Any] | None] = {}
    sensitivity: dict[str, dict[str, Any] | None] = {}

    for index, label_id in enumerate(sorted(MESSAGE_LEVEL_LABELS)):
        if insufficient:
            cutoff_rates[label_id] = {cutoff_key(c): None for c in CUTOFFS}
            probability_index[label_id] = None
        else:
            ci_by_metric = stats.bootstrap_multi_metric_ci(
                clusters,
                label_id,
                CUTOFFS,
                seed=_cell_seed(seed, cell_key, index),
                iterations=bootstrap_iterations,
            )
            mean_point = stats.weighted_rate_per_1000(clusters, label_id)
            mean_ci = ci_by_metric["mean"]
            probability_index[label_id] = {"per_1000": mean_point, "ci95": [mean_ci[0], mean_ci[1]]}

            cutoff_rates[label_id] = {}
            for cutoff in CUTOFFS:
                key = cutoff_key(cutoff)
                point = stats.weighted_sensitivity_rate_per_1000(clusters, label_id, cutoff)
                ci = ci_by_metric[key]
                cutoff_rates[label_id][key] = {"per_1000": point, "ci95": [ci[0], ci[1]]}

        if label_id in STRONG_GATING_LABELS:
            threshold_info = sensitivity_thresholds.get(label_id)
            if insufficient or threshold_info is None:
                sensitivity[label_id] = None
            else:
                rate = stats.weighted_sensitivity_rate_per_1000(
                    clusters, label_id, threshold_info.threshold
                )
                sensitivity[label_id] = {
                    "per_1000": rate,
                    "threshold": threshold_info.threshold,
                    "dataset_name": threshold_info.dataset_name,
                    "permissive": threshold_info.permissive,
                }

    return {
        "messages_classified": n_messages,
        "distinct_authors": n_authors,
        "threads_sampled": threads_sampled,
        "threads_population": threads_population,
        "insufficient_data": insufficient,
        "cutoff_rates_per_1000_messages": cutoff_rates,
        "probability_index_per_1000_messages": probability_index,
        "sensitivity_per_1000_messages": sensitivity,
    }
