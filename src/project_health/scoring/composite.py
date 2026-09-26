"""The versioned 0-100 composite health score (DECISIONS.md D20, amending D4).

Normalizes each dimension's `key` metrics to a 0-100 score (self-baseline,
reusing the exact modified z-score `baseline.py` already computed -- never a
second, independently-tuned scale), averages a dimension's available key
metrics into that dimension's score, then takes a disclosed weighted average
of dimension scores into one composite -- re-normalizing weights over
whichever dimensions have data, and over whichever key metrics have data
within a dimension, exactly as `scoring.yaml`'s
`composite.insufficient_data_handling` documents.

The composite is deliberately never computed or returned on its own: every
`CompositeResult` carries its full `breakdown` (one entry per would-be
dimension, whether or not it ended up included), so a caller can never render
the number without also having everything D20 requires alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass

from project_health.scoring.baseline import BaselineStatusResult
from project_health.scoring.config import CompositeConfig
from project_health.scoring.dimension import DimensionStatusResult


def normalize_metric_score(
    modified_z: float | None, direction_of_good: str, config: CompositeConfig
) -> float | None:
    """One key metric's current `modified_z` -> a 0-100 score.

    `None` for a `none`-direction metric (never classified, never scored) or
    when `modified_z` itself is `None` (insufficient_data). See
    `scoring.yaml`'s `composite.normalization` comment for the formula and
    why its two scale constants are what they are.
    """
    if modified_z is None or direction_of_good == "none":
        return None
    norm = config.normalization
    if direction_of_good == "target-range":
        score = 100.0 - norm.target_range_scale * abs(modified_z)
    else:
        signed = modified_z if direction_of_good == "higher" else -modified_z
        score = norm.neutral_score + norm.z_scale * signed
    return max(0.0, min(100.0, score))


@dataclass(frozen=True)
class DimensionScoreResult:
    dimension: str
    score: float | None  # None when every key metric is insufficient_data
    key_metrics_scored: int
    key_metrics_total: int


def compute_dimension_score(
    dimension: str,
    key_metric_results: dict[str, BaselineStatusResult],
    direction_by_metric: dict[str, str],
    config: CompositeConfig,
) -> DimensionScoreResult:
    """`key_metric_results` maps this dimension's key metric_ids to their
    current-month `BaselineStatusResult` -- a key metric absent from the
    mapping (not yet implemented, or produced no row this run) is treated
    the same as one with `insufficient_data`: excluded, `key_metrics_total`
    still counts it so the disclosed "N of M key metrics scored" note is
    honest about the full roster (SCORING.md §5.3: 1-3 key metrics per
    dimension by design)."""
    scores = [
        s
        for metric_id, result in key_metric_results.items()
        if (s := normalize_metric_score(result.modified_z, direction_by_metric[metric_id], config))
        is not None
    ]
    if not scores:
        return DimensionScoreResult(
            dimension=dimension,
            score=None,
            key_metrics_scored=0,
            key_metrics_total=len(key_metric_results),
        )
    return DimensionScoreResult(
        dimension=dimension,
        score=sum(scores) / len(scores),
        key_metrics_scored=len(scores),
        key_metrics_total=len(key_metric_results),
    )


@dataclass(frozen=True)
class DimensionBreakdownEntry:
    dimension: str
    weight: float
    renormalized_weight: float | None  # None when this dimension isn't included this month
    score: float | None
    status: str
    included: bool
    key_metrics_scored: int
    key_metrics_total: int


@dataclass(frozen=True)
class CompositeResult:
    composite: float | None  # None when every dimension is insufficient_data
    dimensions_included: int
    dimensions_total: int
    has_declining_dimension: bool
    breakdown: tuple[DimensionBreakdownEntry, ...]


def compute_composite(
    dimension_scores: dict[str, DimensionScoreResult],
    dimension_statuses: dict[str, DimensionStatusResult],
    config: CompositeConfig,
) -> CompositeResult:
    """The disclosed, re-normalized weighted average across
    `config.dimension_weights`'s dimensions.

    `dimension_scores`/`dimension_statuses` should each carry an entry for
    every one of `config.dimension_weights`' dimensions where the pipeline
    computed one; a dimension entirely missing from both (e.g. no metric for
    it implemented yet) is treated as `insufficient_data`/no score, same as
    one explicitly marked that way.
    """
    weights = config.dimension_weights

    def _has_score(dimension: str) -> bool:
        result = dimension_scores.get(dimension)
        return result is not None and result.score is not None

    included_weight_sum = sum(
        weight for dimension, weight in weights.items() if _has_score(dimension)
    )

    composite = None
    if included_weight_sum > 0:
        composite = (
            sum(
                weight * dimension_scores[dimension].score
                for dimension, weight in weights.items()
                if _has_score(dimension)
            )
            / included_weight_sum
        )

    breakdown: list[DimensionBreakdownEntry] = []
    dimensions_included = 0
    has_declining_dimension = False
    for dimension, weight in weights.items():
        score_result = dimension_scores.get(dimension)
        status_result = dimension_statuses.get(dimension)
        score = score_result.score if score_result is not None else None
        included = score is not None
        if included:
            dimensions_included += 1
        status = status_result.status if status_result is not None else "insufficient_data"
        if status == "declining":
            has_declining_dimension = True
        renormalized_weight = (
            (weight / included_weight_sum) if included and included_weight_sum > 0 else None
        )
        key_metrics_scored = score_result.key_metrics_scored if score_result is not None else 0
        key_metrics_total = score_result.key_metrics_total if score_result is not None else 0
        breakdown.append(
            DimensionBreakdownEntry(
                dimension=dimension,
                weight=weight,
                renormalized_weight=renormalized_weight,
                score=score,
                status=status,
                included=included,
                key_metrics_scored=key_metrics_scored,
                key_metrics_total=key_metrics_total,
            )
        )

    return CompositeResult(
        composite=composite,
        dimensions_included=dimensions_included,
        dimensions_total=len(weights),
        has_declining_dimension=has_declining_dimension,
        breakdown=tuple(breakdown),
    )
