"""Tone-mix (intensity-tier stack) aggregation for the private Cassandra
communication run (issue #153; COMMUNITY-HEALTH.md §1.2/§2.2's intensity
tiers, §5.1's minimum-sample floors).

Owner request (readers): "Getting graphs back on the sentiment over time
would be ideal... maybe a stacked graph?" Raw sentiment is never published
(§1.2's `sentiment_polarity` is explicitly non-gating and never feeds a
published aggregate). This module instead computes, per (venue, period)
cell and per probability cutoff, the **weighted share of classified
messages in each §2.2 intensity tier** -- the same fixed, versioned
tier-lookup table `thread_derive.py` already uses for thread derivation,
reused here unchanged (not re-implemented) so a message's tier is always
computed exactly one way across this whole project.

Tier shares are a true partition of every classified message (every
message maps to exactly one of the seven tiers via `thread_derive.
intensity_tier`, which always returns a value in `TIERS`), so they sum to
1.0 per cell by construction -- this is what makes the result stack
cleanly to 100% on a chart. Each tier's share carries its own thread-level
(cluster) bootstrap 95% CI, matching `stats.py`'s own bootstrap
convention: resample whole threads with replacement, never individual
messages, since messages within one sampled thread are not independent
draws from the population of threads.

Computed at both the headline (0.5) and a sensitivity (0.7) probability
cutoff (`TONE_MIX_CUTOFFS`) -- mirrors `aggregate.py`'s own headline/
sensitivity split, at the subset of cutoffs issue #153 actually asks for
(0.5/0.7, not the full 0.5/0.7/0.9 message-level sweep). A cell below the
same §5.1 floor `aggregate.py` uses (`MIN_MESSAGES`/`MIN_DISTINCT_AUTHORS`,
imported from there rather than redefined, so the two floors can never
drift apart) renders every tier as `None` -- "insufficient data," never
silently omitted.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from project_health.private_run import stats
from project_health.private_run.aggregate import MIN_DISTINCT_AUTHORS, MIN_MESSAGES
from project_health.private_run.thread_derive import intensity_tier, labels_present_at_cutoff

# §2.2's seven intensity tiers, in bottom-to-top stacking order -- the same
# order a stacked chart reading "closing/positive" at the bottom through
# "attack" at the top wants, and the same order `thread_derive.LABEL_TIER`'s
# values span.
TIERS: tuple[int, ...] = (-2, -1, 0, 1, 2, 3, 4)

# Plain-language names for each tier (COMMUNITY-HEALTH.md §2.2's own
# "Meaning" column, verbatim) -- what a chart legend/tooltip shows instead
# of a bare signed integer.
TIER_NAMES: dict[int, str] = {
    -2: "Closing/positive",
    -1: "Constructive/positive",
    0: "Neutral",
    1: "Substantive disagreement",
    2: "Non-substantive friction",
    3: "Hostile",
    4: "Attack",
}

# Issue #153, verbatim: "weighted ... share of messages in each tier ...
# Also at cutoff 0.7 for sensitivity." The headline cutoff is first so
# callers that want "the" cutoff by default can take `TONE_MIX_CUTOFFS[0]`.
TONE_MIX_HEADLINE_CUTOFF = 0.5
TONE_MIX_SENSITIVITY_CUTOFF = 0.7
TONE_MIX_CUTOFFS: tuple[float, ...] = (TONE_MIX_HEADLINE_CUTOFF, TONE_MIX_SENSITIVITY_CUTOFF)

DEFAULT_BOOTSTRAP_ITERATIONS = stats.DEFAULT_BOOTSTRAP_ITERATIONS


@dataclass(frozen=True)
class TierCluster:
    """One sampled thread's messages, already reduced to their §2.2
    intensity tier at one probability cutoff -- the tone-mix analog of
    `stats.ThreadCluster` (which keeps full per-label probabilities; this
    only needs each message's single resulting tier)."""

    thread_id: str
    weight: float
    tiers: tuple[int, ...] = field(default_factory=tuple)


def from_message_clusters(
    clusters: list[stats.ThreadCluster], cutoff: float
) -> list[TierCluster]:
    """Build `TierCluster`s from the same `stats.ThreadCluster` list
    `aggregate.aggregate_cell` already consumes (`runner.py`'s
    `clusters_by_cell`/`year_clusters`) -- every message's tier is derived
    here via `thread_derive.intensity_tier`/`labels_present_at_cutoff`, the
    same functions (and therefore the same fixed §2.2 table) thread-level
    derivation uses, so tone-mix and thread derivation can never disagree
    about what tier a message is in at a given cutoff."""
    return [
        TierCluster(
            thread_id=cluster.thread_id,
            weight=cluster.weight,
            tiers=tuple(
                intensity_tier(labels_present_at_cutoff(message, cutoff))
                for message in cluster.messages
            ),
        )
        for cluster in clusters
    ]


def message_count(clusters: list[TierCluster]) -> int:
    """The raw (unweighted) count of classified messages across
    `clusters` -- what the §5.1 floor is checked against, matching
    `stats.message_count`'s own convention."""
    return sum(len(c.tiers) for c in clusters)


def weighted_tier_shares(clusters: list[TierCluster]) -> dict[int, float]:
    """The survey-weighted share of messages in each tier, across
    `clusters`. Sums to `1.0` over `TIERS` whenever `clusters` carries at
    least one message (every message contributes its thread's weight to
    exactly one tier) -- all-zero (never a ZeroDivisionError) when
    `clusters` has no messages at all."""
    weighted_total = sum(cluster.weight * len(cluster.tiers) for cluster in clusters)
    shares = {tier: 0.0 for tier in TIERS}
    if weighted_total <= 0:
        return shares
    for cluster in clusters:
        for tier in cluster.tiers:
            shares[tier] += cluster.weight
    return {tier: value / weighted_total for tier, value in shares.items()}


def bootstrap_tier_shares_ci(
    clusters: list[TierCluster],
    *,
    seed: int,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    alpha: float = 0.05,
) -> dict[int, tuple[float, float]]:
    """The thread-level bootstrap 95% (or `1-alpha`) CI for every tier's
    share, from **one shared resample per iteration** (mirrors `stats.
    bootstrap_multi_metric_ci`'s own "generating the resampled thread list
    is the expensive part" reasoning -- computing all seven tiers' shares
    from the same resample costs the same as computing just one). `(0.0,
    0.0)` for every tier with zero clusters -- nothing to resample."""
    n = len(clusters)
    if n == 0:
        return {tier: (0.0, 0.0) for tier in TIERS}
    rng = random.Random(seed)
    samples_by_tier: dict[int, list[float]] = {tier: [] for tier in TIERS}
    for _ in range(iterations):
        resampled = [clusters[rng.randrange(n)] for _ in range(n)]
        shares = weighted_tier_shares(resampled)
        for tier in TIERS:
            samples_by_tier[tier].append(shares[tier])
    return {
        tier: stats.percentile_interval(samples, alpha)
        for tier, samples in samples_by_tier.items()
    }


def aggregate_tone_mix_cell(
    clusters: list[TierCluster],
    distinct_authors: set[str],
    *,
    seed: int,
    cell_key: str,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    threads_population: int = 0,
    threads_sampled: int = 0,
) -> dict[str, Any]:
    """One (venue, period, cutoff) cell's tone mix: counts, the §5.1 floor
    verdict, and every tier's weighted share + thread-level bootstrap 95%
    CI -- `None` for every tier when the cell is below floor, mirroring
    `aggregate.aggregate_cell`'s own convention so a reader can see *why*
    a cell renders as insufficient data (never silently omitted)."""
    n_messages = message_count(clusters)
    n_authors = len(distinct_authors)
    insufficient = n_messages < MIN_MESSAGES or n_authors < MIN_DISTINCT_AUTHORS

    tiers: dict[str, dict[str, Any] | None]
    if insufficient:
        tiers = {str(tier): None for tier in TIERS}
    else:
        shares = weighted_tier_shares(clusters)
        cis = bootstrap_tier_shares_ci(clusters, seed=seed, iterations=bootstrap_iterations)
        tiers = {
            str(tier): {
                "name": TIER_NAMES[tier],
                "share": shares[tier],
                "ci95": [cis[tier][0], cis[tier][1]],
            }
            for tier in TIERS
        }

    return {
        "messages_classified": n_messages,
        "distinct_authors": n_authors,
        "threads_sampled": threads_sampled,
        "threads_population": threads_population,
        "insufficient_data": insufficient,
        "tiers": tiers,
    }
