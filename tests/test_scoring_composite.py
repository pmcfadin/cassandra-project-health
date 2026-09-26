"""Golden tests for `scoring/composite.py` (issue #57, DECISIONS.md D20): the
0-100 per-metric normalization, a dimension's averaged key-metric score, and
the composite's disclosed weight re-normalization over dimensions with data.

`scoring.yaml`'s `composite.normalization` constants are `z_scale =
16.66666667` (`50 / 3`) and `target_range_scale = 33.33333333` (`100 / 3`),
chosen so `|z| == stable_threshold (1.5)` lands on 25/75 and `|z| ==
large_deviation_threshold (3.0)` saturates at 0/100 -- every assertion below
is that formula evaluated by hand.
"""

from __future__ import annotations

from datetime import date

import pytest

from project_health.scoring.baseline import BaselineStatusResult
from project_health.scoring.composite import (
    compute_composite,
    compute_dimension_score,
    normalize_metric_score,
)
from project_health.scoring.config import load_scoring_config
from project_health.scoring.dimension import DimensionStatusResult

CONFIG = load_scoring_config().composite
WINDOW_END = date(2025, 1, 1)


def _result(modified_z: float | None, status: str = "declining") -> BaselineStatusResult:
    return BaselineStatusResult(
        metric_id="unused",
        window_end=WINDOW_END,
        current_value=None,
        baseline_median=None,
        baseline_mad=None,
        baseline_months=24,
        modified_z=modified_z,
        status=status,
        confirmed=True,
        notable_single_month_event=False,
    )


# --- Per-metric normalization (monotonic, direction-aware) -----------------


def test_higher_direction_normalization_at_the_named_thresholds():
    assert normalize_metric_score(0.0, "higher", CONFIG) == pytest.approx(50.0)
    assert normalize_metric_score(1.5, "higher", CONFIG) == pytest.approx(75.0)
    assert normalize_metric_score(3.0, "higher", CONFIG) == pytest.approx(100.0)
    assert normalize_metric_score(6.0, "higher", CONFIG) == pytest.approx(100.0)  # saturates
    assert normalize_metric_score(-3.0, "higher", CONFIG) == pytest.approx(0.0)


def test_lower_direction_normalization_is_sign_flipped():
    """A `lower` metric's *rising* value is unfavorable -- a positive
    modified_z (value above baseline) scores *below* 50, the mirror image of
    `higher`."""
    assert normalize_metric_score(1.5, "lower", CONFIG) == pytest.approx(25.0)
    assert normalize_metric_score(-1.5, "lower", CONFIG) == pytest.approx(75.0)
    assert normalize_metric_score(3.0, "lower", CONFIG) == pytest.approx(0.0, abs=1e-6)


def test_target_range_normalization_penalizes_either_direction_equally():
    assert normalize_metric_score(0.0, "target-range", CONFIG) == pytest.approx(100.0)
    assert normalize_metric_score(1.5, "target-range", CONFIG) == pytest.approx(50.0)
    assert normalize_metric_score(-1.5, "target-range", CONFIG) == pytest.approx(50.0)
    assert normalize_metric_score(3.0, "target-range", CONFIG) == pytest.approx(0.0, abs=1e-6)


def test_none_direction_and_missing_z_are_never_scored():
    assert normalize_metric_score(1.5, "none", CONFIG) is None
    assert normalize_metric_score(None, "higher", CONFIG) is None


# --- Dimension score: average of available key metrics ---------------------


def test_dimension_score_averages_its_key_metrics():
    key_results = {"a": _result(1.5), "b": _result(-1.5)}  # scores 75, 25 for 'higher'
    result = compute_dimension_score(
        "d", key_results, {"a": "higher", "b": "higher"}, CONFIG
    )
    assert result.score == pytest.approx(50.0)
    assert result.key_metrics_scored == 2
    assert result.key_metrics_total == 2


def test_dimension_score_excludes_insufficient_data_key_metrics():
    """A key metric with `modified_z = None` (insufficient_data) drops out of
    the average -- the dimension's own weight among its key metrics is
    implicitly renormalized over the metrics that do have data."""
    key_results = {"a": _result(1.5), "b": _result(None, status="insufficient_data")}
    result = compute_dimension_score(
        "d", key_results, {"a": "higher", "b": "higher"}, CONFIG
    )
    assert result.score == pytest.approx(75.0)  # only "a" counted
    assert result.key_metrics_scored == 1
    assert result.key_metrics_total == 2


def test_dimension_score_is_none_when_every_key_metric_is_insufficient_data():
    key_results = {"a": _result(None, status="insufficient_data")}
    result = compute_dimension_score("d", key_results, {"a": "higher"}, CONFIG)
    assert result.score is None
    assert result.key_metrics_scored == 0
    assert result.key_metrics_total == 1


# --- Composite: disclosed weight re-normalization (D20) --------------------


def _dimension_score(dimension: str, score: float | None):
    from project_health.scoring.composite import DimensionScoreResult

    scored = 1 if score is not None else 0
    return DimensionScoreResult(
        dimension=dimension, score=score, key_metrics_scored=scored, key_metrics_total=1
    )


def _dimension_status(dimension: str, status: str) -> DimensionStatusResult:
    return DimensionStatusResult(dimension=dimension, status=status, driven_by=())


def test_composite_renormalizes_weights_over_dimensions_with_data():
    """5 equally-weighted (0.2 each) dimensions, 2 of them insufficient_data
    (no score). The composite is the plain average of the 3 scored
    dimensions' scores (60, 80, 100 -> 80.0), because equal weights
    renormalize to equal weights again over any subset -- the renormalized
    weight per included dimension is `0.2 / 0.6 = 1/3`, disclosed per
    dimension in the breakdown.
    """
    scores = {
        "contributor sustainability": _dimension_score("contributor sustainability", 60.0),
        "reviewer capacity": _dimension_score("reviewer capacity", 80.0),
        "responsiveness": _dimension_score("responsiveness", 100.0),
        "organizational diversity": _dimension_score("organizational diversity", None),
        "release cadence": _dimension_score("release cadence", None),
    }
    statuses = {
        "contributor sustainability": _dimension_status("contributor sustainability", "stable"),
        "reviewer capacity": _dimension_status("reviewer capacity", "improving"),
        "responsiveness": _dimension_status("responsiveness", "improving"),
        "organizational diversity": _dimension_status(
            "organizational diversity", "insufficient_data"
        ),
        "release cadence": _dimension_status("release cadence", "insufficient_data"),
    }
    result = compute_composite(scores, statuses, CONFIG)
    assert result.composite == pytest.approx((60.0 + 80.0 + 100.0) / 3)
    assert result.dimensions_included == 3
    assert result.dimensions_total == 5
    included_entries = [e for e in result.breakdown if e.included]
    assert len(included_entries) == 3
    for entry in included_entries:
        assert entry.renormalized_weight == pytest.approx(1.0 / 3.0)
    excluded_entries = [e for e in result.breakdown if not e.included]
    for entry in excluded_entries:
        assert entry.renormalized_weight is None
        assert entry.status == "insufficient_data"


def test_composite_unequal_weighted_average_arithmetic():
    """A non-trivial weighted average, hand-checked: weights 0.2 (x5) but
    only two dimensions score (0.2 each of the total 1.0) -> renormalized
    weight 0.5/0.5 each -> simple average of the two scores."""
    scores = {
        "contributor sustainability": _dimension_score("contributor sustainability", 40.0),
        "reviewer capacity": _dimension_score("reviewer capacity", 90.0),
        "responsiveness": _dimension_score("responsiveness", None),
        "organizational diversity": _dimension_score("organizational diversity", None),
        "release cadence": _dimension_score("release cadence", None),
    }
    statuses = {name: _dimension_status(name, "stable") for name in scores}
    result = compute_composite(scores, statuses, CONFIG)
    assert result.composite == pytest.approx((40.0 + 90.0) / 2)
    assert result.dimensions_included == 2


def test_composite_is_none_when_every_dimension_is_insufficient_data():
    scores = {name: _dimension_score(name, None) for name in CONFIG.dimension_weights}
    statuses = {
        name: _dimension_status(name, "insufficient_data") for name in CONFIG.dimension_weights
    }
    result = compute_composite(scores, statuses, CONFIG)
    assert result.composite is None
    assert result.dimensions_included == 0


def test_has_declining_dimension_flag_set_from_an_included_dimensions_status():
    scores = {name: _dimension_score(name, 50.0) for name in CONFIG.dimension_weights}
    statuses = {name: _dimension_status(name, "stable") for name in CONFIG.dimension_weights}
    declining_dim = next(iter(CONFIG.dimension_weights))
    statuses[declining_dim] = _dimension_status(declining_dim, "declining")
    result = compute_composite(scores, statuses, CONFIG)
    assert result.has_declining_dimension is True


def test_has_declining_dimension_flag_false_when_nothing_declining():
    scores = {name: _dimension_score(name, 50.0) for name in CONFIG.dimension_weights}
    statuses = {name: _dimension_status(name, "stable") for name in CONFIG.dimension_weights}
    result = compute_composite(scores, statuses, CONFIG)
    assert result.has_declining_dimension is False
