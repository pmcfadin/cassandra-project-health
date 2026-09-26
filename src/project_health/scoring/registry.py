"""Per-metric scoring metadata, transcribed verbatim from `docs/spec/
METRICS.md` §1's summary table (dimension, phase, direction_of_good, role).

This is a separate module from `site/metrics_meta.py` on purpose: that module
is presentation-only metadata (card grouping, number formatting, which site
page a metric's card renders on) for the site generator, while this module is
the scoring engine's own input -- the exact same underlying facts
(`dimension`, `direction_of_good`) plus the two fields the scoring engine
needs that the site doesn't (`role`, `phase`), kept independent so an issue
landing in parallel that edits `site/metrics_meta.py` for a presentation
reason can never accidentally change what the scoring engine computes, and
vice versa. Both modules cite METRICS.md §1 as their shared source of truth,
so a real definition change (e.g. a `key` tag moving, SCORING.md §8) must be
applied to both, deliberately, not silently inherited from one to the other.

Only metrics with a real computation (`metrics.registry.METRIC_IDS`) can ever
appear in a `metric_value` snapshot, so `scoring/engine.py` only computes a
baseline status for a `metric_id` that is in *both* this registry and that
run's `metric_value` table -- a metric catalogued in METRICS.md but not yet
implemented (e.g. `release_frequency`, release cadence's only key metric, has
no collector yet) is listed here for forward-completeness (so scoring picks
it up automatically the day it ships, with no registry edit needed) but
simply never appears in any run's baseline-status output until then, exactly
like any other not-yet-implemented metric's absence from `metric_value`.
"""

from __future__ import annotations

from dataclasses import dataclass

# phase: 1 (deterministic MVP, D1) | 2 (classified, gated -- COMMUNITY-HEALTH.md)
# direction_of_good: 'higher' | 'lower' | 'target-range' | 'none' (SCORING.md §4.3)
# role: 'key' | 'supporting' (SCORING.md §5.3, capped at 1-3 key metrics/dimension)


@dataclass(frozen=True)
class MetricScoringMeta:
    metric_id: str
    dimension: str
    phase: int
    direction_of_good: str
    role: str


_METRICS: tuple[MetricScoringMeta, ...] = (
    # --- Contributor sustainability (METRICS.md §2) -------------------------
    MetricScoringMeta(
        "active_contributors_monthly", "contributor sustainability", 1, "higher", "supporting"
    ),
    MetricScoringMeta(
        "new_contributors_monthly", "contributor sustainability", 1, "higher", "supporting"
    ),
    MetricScoringMeta(
        "first_to_second_conversion_rate", "contributor sustainability", 1, "higher", "supporting"
    ),
    MetricScoringMeta(
        "funnel_stage_conversion", "contributor sustainability", 1, "higher", "supporting"
    ),
    MetricScoringMeta(
        "contributor_tenure_survival", "contributor sustainability", 1, "higher", "supporting"
    ),
    MetricScoringMeta(
        "contributor_churn_rate", "contributor sustainability", 1, "lower", "supporting"
    ),
    MetricScoringMeta(
        "sustained_contributor_count", "contributor sustainability", 1, "higher", "key"
    ),
    MetricScoringMeta(
        "pmc_joins_quarterly", "contributor sustainability", 1, "higher", "supporting"
    ),
    MetricScoringMeta("truck_factor", "contributor sustainability", 1, "higher", "key"),
    MetricScoringMeta(
        "contributor_absence_factor", "contributor sustainability", 1, "higher", "supporting"
    ),
    MetricScoringMeta("contributor_hhi", "contributor sustainability", 1, "lower", "key"),
    MetricScoringMeta(
        "effective_contributor_population", "contributor sustainability", 1, "higher", "supporting"
    ),
    # --- Reviewer capacity (METRICS.md §3) -----------------------------------
    MetricScoringMeta("unique_reviewers_monthly", "reviewer capacity", 1, "higher", "key"),
    MetricScoringMeta("reviewer_top_k_share", "reviewer capacity", 1, "lower", "supporting"),
    MetricScoringMeta("reviewer_hhi", "reviewer capacity", 1, "lower", "key"),
    MetricScoringMeta(
        "effective_reviewer_population", "reviewer capacity", 1, "higher", "supporting"
    ),
    MetricScoringMeta(
        "merge_authority_concentration", "reviewer capacity", 1, "none", "supporting"
    ),
    MetricScoringMeta("review_latency", "reviewer capacity", 1, "lower", "key"),
    MetricScoringMeta("pr_time_to_first_review", "reviewer capacity", 1, "lower", "supporting"),
    MetricScoringMeta("pr_review_engagement", "reviewer capacity", 1, "none", "supporting"),
    MetricScoringMeta("review_load_per_reviewer", "reviewer capacity", 1, "none", "supporting"),
    MetricScoringMeta("contributor_reviewer_ratio", "reviewer capacity", 1, "lower", "supporting"),
    # --- Responsiveness (METRICS.md §4) --------------------------------------
    MetricScoringMeta("time_to_first_response_pr", "responsiveness", 1, "lower", "supporting"),
    MetricScoringMeta("time_to_first_response_jira", "responsiveness", 1, "lower", "key"),
    MetricScoringMeta("time_to_first_reply_devlist", "responsiveness", 1, "lower", "key"),
    MetricScoringMeta("unanswered_thread_rate_devlist", "responsiveness", 1, "lower", "supporting"),
    MetricScoringMeta("change_request_closure_ratio", "responsiveness", 1, "higher", "supporting"),
    MetricScoringMeta("stale_pr_rate", "responsiveness", 1, "lower", "supporting"),
    MetricScoringMeta("stale_jira_rate", "responsiveness", 1, "lower", "key"),
    MetricScoringMeta("median_resolution_latency_jira", "responsiveness", 1, "lower", "supporting"),
    MetricScoringMeta("pr_merge_lead_time", "responsiveness", 1, "lower", "supporting"),
    MetricScoringMeta("pr_time_to_close", "responsiveness", 1, "lower", "supporting"),
    # --- Organizational diversity (METRICS.md §5) ----------------------------
    MetricScoringMeta("elephant_factor", "organizational diversity", 1, "higher", "key"),
    MetricScoringMeta("organizational_hhi", "organizational diversity", 1, "lower", "key"),
    MetricScoringMeta(
        "effective_organizational_population", "organizational diversity", 1, "higher", "supporting"
    ),
    MetricScoringMeta("single_org_share", "organizational diversity", 1, "lower", "supporting"),
    MetricScoringMeta(
        "unknown_affiliation_rate", "organizational diversity", 1, "none", "supporting"
    ),
    # --- Release cadence (METRICS.md §6) -------------------------------------
    MetricScoringMeta("release_frequency", "release cadence", 1, "target-range", "key"),
    MetricScoringMeta("release_regularity", "release cadence", 1, "none", "supporting"),
    MetricScoringMeta("time_since_last_release", "release cadence", 1, "none", "supporting"),
    # --- Interaction health (METRICS.md §7, Phase 2 -- excluded from scoring
    #     per SCORING.md §5.1 rule "phase 2/classified metrics" and D20's
    #     "classified metrics don't enter the composite until they pass their
    #     validation gates"; listed here for completeness only) -------------
    MetricScoringMeta(
        "dismissive_interaction_rate", "interaction health", 2, "lower", "supporting"
    ),
    MetricScoringMeta("escalation_rate", "interaction health", 2, "lower", "key"),
    MetricScoringMeta("constructive_resolution_rate", "interaction health", 2, "higher", "key"),
    MetricScoringMeta(
        "newcomer_interaction_quality", "interaction health", 2, "higher", "supporting"
    ),
)

METRIC_SCORING_META: dict[str, MetricScoringMeta] = {m.metric_id: m for m in _METRICS}

# Phase-1 dimensions in D4's own order (contributor sustainability, reviewer
# capacity, responsiveness, organizational diversity, release cadence).
# `interaction health` is Phase 2 and deliberately excluded here -- it is
# never a composite input (D20) and is not scored for baseline status either
# until its metrics are actually implemented and validated.
PHASE_1_DIMENSIONS: tuple[str, ...] = (
    "contributor sustainability",
    "reviewer capacity",
    "responsiveness",
    "organizational diversity",
    "release cadence",
)


def key_metrics_for(dimension: str) -> tuple[str, ...]:
    """The `key`-role, Phase-1 metric ids for `dimension`, in registry order
    (SCORING.md §5.3: 1-3 per dimension by design)."""
    return tuple(
        m.metric_id
        for m in _METRICS
        if m.dimension == dimension and m.role == "key" and m.phase == 1
    )
