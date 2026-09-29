"""Stratified thread sampling for the private Cassandra communication run
(issue #110).

Reuses `classify.sample.deterministic_sample` (issue #44): a seeded,
reproducible pick within each (venue, quarter) stratum, independent of the
population list's own order, so re-running with the same seed always
selects the same threads. Every stratum also gets an inclusion weight
(population / sampled) -- issue #110's "record inclusion weights (threads
in stratum / threads sampled) so quarterly rates are population estimates"
-- which every message inside a sampled thread inherits (`stats.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from project_health.classify.sample import deterministic_sample

# "Per stratum draw up to K=60 threads" (issue #110).
DEFAULT_K = 60
# The sampler's own seed (issue #110: "seeded, recorded"). Namespaced by
# venue/quarter (see `sample_stratum`), so one seed covers the whole run.
DEFAULT_SEED = 110


@dataclass(frozen=True)
class StratumSample:
    venue: str
    quarter: str
    population: int
    sampled_ids: tuple[str, ...]
    # population / len(sampled_ids); 0.0 for an empty stratum (nothing to weight).
    weight: float


def sample_stratum(
    venue: str,
    quarter: str,
    population_ids: list[str],
    *,
    seed: int,
    k: int = DEFAULT_K,
) -> StratumSample:
    """Sample up to `k` threads from `population_ids` for one (`venue`,
    `quarter`) stratum, deterministically given `(seed, venue, quarter)`.
    """
    population = len(population_ids)
    sampled = deterministic_sample(seed, f"private_run:{venue}:{quarter}", population_ids, k)
    weight = (population / len(sampled)) if sampled else 0.0
    return StratumSample(
        venue=venue,
        quarter=quarter,
        population=population,
        sampled_ids=tuple(sampled),
        weight=weight,
    )


def sample_all_strata(
    venue: str,
    frame: dict[str, list[str]],
    quarters: list[str],
    *,
    seed: int,
    k: int = DEFAULT_K,
) -> dict[str, StratumSample]:
    """`{quarter: StratumSample}` for every quarter in `quarters`, even one
    with an empty population (`StratumSample(population=0, sampled_ids=(),
    weight=0.0)`) -- callers should never need a membership check before
    indexing by quarter.
    """
    return {q: sample_stratum(venue, q, frame.get(q, []), seed=seed, k=k) for q in quarters}
