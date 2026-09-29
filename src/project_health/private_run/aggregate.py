"""Per-cell (venue × quarter, venue × year) aggregation for the private
Cassandra communication run (issue #110; COMMUNITY-HEALTH.md §5.1's
minimum-sample floors, §5.2's per-1,000-messages framing, D23's sensitivity
mappings).

A "cell" is one (venue, quarter) or (venue, year) bucket: a raw
(unweighted) message/distinct-author count for the §5.1 floor check, plus,
for every one of the 12 message-level labels, the weighted rate per 1,000
messages and its thread-level bootstrap 95% CI (`stats.py`) -- or, below
either floor, `None` everywhere except the counts themselves, so a reader
can see *why* a cell renders as `insufficient data` (COMMUNITY-HEALTH.md
§5.1: "renders as insufficient data", never silently omitted).
"""

from __future__ import annotations

import hashlib
from typing import Any

from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.private_run import stats
from project_health.private_run.sensitivity import STRONG_GATING_LABELS

# Issue #110, verbatim: "Apply §5.1 floors (>=30 messages and >=10 distinct
# authors per cell, else `insufficient data`)."
MIN_MESSAGES = 30
MIN_DISTINCT_AUTHORS = 10

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


def aggregate_cell(
    clusters: list[stats.ThreadCluster],
    distinct_authors: set[str],
    *,
    seed: int,
    cell_key: str,
    sensitivity_thresholds: dict[str, float],
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    threads_population: int = 0,
    threads_sampled: int = 0,
) -> dict[str, Any]:
    """One cell's full aggregate: counts, the §5.1 floor verdict, every
    label's weighted rate + CI (or `None` below the floor), and the
    sensitivity column for `STRONG_GATING_LABELS`.
    """
    n_messages = stats.message_count(clusters)
    n_authors = len(distinct_authors)
    insufficient = n_messages < MIN_MESSAGES or n_authors < MIN_DISTINCT_AUTHORS

    rates: dict[str, dict[str, Any] | None] = {}
    sensitivity: dict[str, float | None] = {}
    for index, label_id in enumerate(sorted(MESSAGE_LEVEL_LABELS)):
        if insufficient:
            rates[label_id] = None
        else:
            rate = stats.weighted_rate_per_1000(clusters, label_id)
            ci = stats.bootstrap_weighted_rate_ci(
                clusters,
                label_id,
                seed=_cell_seed(seed, cell_key, index),
                iterations=bootstrap_iterations,
            )
            rates[label_id] = {"per_1000": rate, "ci95": [ci[0], ci[1]]}

        if label_id in STRONG_GATING_LABELS:
            threshold = sensitivity_thresholds.get(label_id)
            if insufficient or threshold is None:
                sensitivity[label_id] = None
            else:
                sensitivity[label_id] = stats.weighted_sensitivity_rate_per_1000(
                    clusters, label_id, threshold
                )

    return {
        "messages_classified": n_messages,
        "distinct_authors": n_authors,
        "threads_sampled": threads_sampled,
        "threads_population": threads_population,
        "insufficient_data": insufficient,
        "rates_per_1000_messages": rates,
        "sensitivity_per_1000_messages": sensitivity,
    }
