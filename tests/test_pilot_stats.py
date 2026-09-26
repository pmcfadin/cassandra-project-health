"""Tests for `project_health.pilot.stats` (issue #47). Pure-Python math,
fully offline; several golden values are hand-derived in the module
docstring-adjacent PR description and cross-checked against known reference
figures (e.g. the classic Wilson interval for 5/10 at 95%)."""

from __future__ import annotations

import pytest

from project_health.pilot import stats


class TestConfusionAndPrecisionRecallF1:
    def test_confusion_at_threshold(self):
        y_true = [1, 0, 1, 0, 1]
        y_prob = [0.9, 0.8, 0.4, 0.3, 0.6]
        confusion = stats.confusion_at_threshold(y_true, y_prob, 0.5)
        assert confusion == stats.Confusion(tp=2, fp=1, tn=1, fn=1)

    def test_precision_recall_f1_golden(self):
        confusion = stats.Confusion(tp=3, fp=1, fn=2, tn=4)
        precision, recall, f1 = stats.precision_recall_f1(confusion)
        assert precision == pytest.approx(0.75)
        assert recall == pytest.approx(0.6)
        assert f1 == pytest.approx(0.6666666666666666)

    def test_precision_recall_f1_zero_division_is_zero(self):
        confusion = stats.Confusion(tp=0, fp=0, fn=5, tn=5)
        precision, recall, f1 = stats.precision_recall_f1(confusion)
        assert (precision, recall, f1) == (0.0, 0.0, 0.0)

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError):
            stats.confusion_at_threshold([1, 0], [0.5], 0.5)


class TestBootstrapCi:
    def test_bootstrap_ci_is_seeded_and_deterministic(self):
        y_true = [1, 0, 1, 0, 1, 0, 1, 1, 0, 0]
        y_prob = [0.9, 0.2, 0.8, 0.4, 0.7, 0.1, 0.6, 0.55, 0.3, 0.05]
        a = stats.bootstrap_ci_at_threshold(y_true, y_prob, 0.5, seed=1, iterations=200)
        b = stats.bootstrap_ci_at_threshold(y_true, y_prob, 0.5, seed=1, iterations=200)
        assert a == b

    def test_bootstrap_ci_differs_with_different_seed(self):
        y_true = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 1, 0]
        y_prob = [
            0.9,
            0.8,
            0.4,
            0.3,
            0.6,
            0.55,
            0.2,
            0.45,
            0.7,
            0.35,
            0.65,
            0.5,
            0.5,
            0.6,
            0.75,
            0.25,
            0.85,
            0.15,
            0.55,
            0.45,
        ]
        a = stats.bootstrap_ci_at_threshold(y_true, y_prob, 0.5, seed=1, iterations=300)
        b = stats.bootstrap_ci_at_threshold(y_true, y_prob, 0.5, seed=2, iterations=300)
        assert a != b

    def test_bootstrap_ci_empty_data(self):
        p_ci, r_ci, f1_ci = stats.bootstrap_ci_at_threshold([], [], 0.5, seed=1)
        assert p_ci == (0.0, 0.0)
        assert r_ci == (0.0, 0.0)
        assert f1_ci == (0.0, 0.0)

    def test_ci_bounds_contain_point_estimate_roughly(self):
        # Not a strict guarantee for every bootstrap draw, but true often enough
        # with enough iterations on well-behaved data to catch a badly broken
        # implementation (e.g. bounds reversed or always (0, 0)).
        y_true = [1, 1, 1, 0, 0, 0, 1, 0, 1, 0]
        y_prob = [0.9, 0.8, 0.7, 0.3, 0.2, 0.1, 0.6, 0.4, 0.95, 0.05]
        confusion = stats.confusion_at_threshold(y_true, y_prob, 0.5)
        precision, recall, f1 = stats.precision_recall_f1(confusion)
        p_ci, r_ci, f1_ci = stats.bootstrap_ci_at_threshold(
            y_true, y_prob, 0.5, seed=7, iterations=500
        )
        assert p_ci[0] <= precision <= p_ci[1]
        assert r_ci[0] <= recall <= r_ci[1]
        assert f1_ci[0] <= f1 <= f1_ci[1]


class TestSweepThresholds:
    def test_sweep_covers_every_threshold(self):
        y_true = [1, 0, 1, 0]
        y_prob = [0.9, 0.1, 0.6, 0.4]
        sweep = stats.sweep_thresholds(
            y_true, y_prob, thresholds=(0.2, 0.5, 0.8), seed=1, iterations=20
        )
        assert [m.threshold for m in sweep] == [0.2, 0.5, 0.8]

    def test_best_threshold_by_f1(self):
        y_true = [1, 0, 1, 0]
        y_prob = [0.9, 0.1, 0.6, 0.4]
        sweep = stats.sweep_thresholds(
            y_true, y_prob, thresholds=(0.2, 0.5, 0.8), seed=1, iterations=20
        )
        best = stats.best_threshold_by_f1(sweep)
        # threshold=0.5 -> predicted [1,0,1,0] == y_true exactly: F1=1.0
        assert best.threshold == 0.5
        assert best.f1 == pytest.approx(1.0)

    def test_best_threshold_empty_sweep_raises(self):
        with pytest.raises(ValueError):
            stats.best_threshold_by_f1([])


class TestReliabilityBins:
    def test_bins_partition_and_edge_cases(self):
        y_true = [1, 0, 1]
        y_prob = [0.05, 0.55, 1.0]
        bins = stats.reliability_bins(y_true, y_prob, num_bins=10)
        assert len(bins) == 10
        assert bins[0].n == 1  # 0.05 in [0.0, 0.1)
        assert bins[5].n == 1  # 0.55 in [0.5, 0.6)
        assert bins[9].n == 1  # 1.0 included in the final bin's closed upper edge
        assert bins[9].mean_predicted == pytest.approx(1.0)
        assert bins[9].observed_rate == pytest.approx(1.0)

    def test_empty_bin_has_none_fields(self):
        bins = stats.reliability_bins([1], [0.95], num_bins=10)
        assert bins[0].n == 0
        assert bins[0].mean_predicted is None
        assert bins[0].observed_rate is None


class TestWilsonInterval:
    def test_zero_n(self):
        assert stats.wilson_interval(0, 0) == (0.0, 0.0)

    def test_classic_5_of_10_reference_values(self):
        lo, hi = stats.wilson_interval(5, 10)
        assert lo == pytest.approx(0.2366, abs=1e-3)
        assert hi == pytest.approx(0.7634, abs=1e-3)

    def test_bounds_are_clamped_to_unit_interval(self):
        lo, hi = stats.wilson_interval(1, 1)
        assert 0.0 <= lo <= hi <= 1.0


class TestToneAgreement:
    def test_exact_agreement_rate(self):
        assert stats.exact_agreement_rate([1, 2, 3], [1, 2, 4]) == pytest.approx(2 / 3)

    def test_off_by_one_rate(self):
        assert stats.off_by_one_rate([1, 2, 3], [2, 2, 1]) == pytest.approx(2 / 3)

    def test_agreement_rate_mismatched_lengths_raise(self):
        with pytest.raises(ValueError):
            stats.exact_agreement_rate([1], [1, 2])
        with pytest.raises(ValueError):
            stats.off_by_one_rate([1], [1, 2])

    def test_weighted_kappa_perfect_agreement_is_one(self):
        levels = [0, 1, 2, 3, 4]
        assert stats.weighted_kappa(levels, levels, levels) == pytest.approx(1.0)

    def test_weighted_kappa_degenerate_marginals_is_zero(self):
        a = [0, 0, 0, 0]
        b = [4, 4, 4, 4]
        assert stats.weighted_kappa(a, b, [0, 1, 2, 3, 4]) == pytest.approx(0.0)

    def test_weighted_kappa_too_few_items_is_zero(self):
        assert stats.weighted_kappa([1], [1], [0, 1, 2]) == 0.0


class TestKrippendorffAlpha:
    def test_perfect_agreement_is_one(self):
        data = [[1, 1], [1, 1], [0, 0], [0, 0]]
        assert stats.krippendorff_alpha_nominal(data) == pytest.approx(1.0)

    def test_golden_mixed_agreement_value(self):
        data = [[1, 0], [0, 1], [1, 1], [0, 0]]
        alpha = stats.krippendorff_alpha_nominal(data)
        assert alpha == pytest.approx(0.125, abs=1e-9)

    def test_undefined_with_fewer_than_two_pairable_units(self):
        assert stats.krippendorff_alpha_nominal([[1, None], [None, None]]) is None

    def test_missing_values_are_excluded_not_counted_as_a_category(self):
        data = [[1, 1], [1, None], [0, 0], [0, None]]
        # Only the first and third units are pairable; both perfect agreement.
        assert stats.krippendorff_alpha_nominal(data) == pytest.approx(1.0)


class TestMedianPercentile:
    def test_median_odd_and_even(self):
        assert stats.median([1, 3, 2]) == 2
        assert stats.median([1, 2, 3, 4]) == pytest.approx(2.5)

    def test_median_empty(self):
        assert stats.median([]) == 0.0

    def test_percentile_p90(self):
        values = list(range(1, 11))  # 1..10
        assert stats.percentile(values, 90) == pytest.approx(9.1)

    def test_percentile_empty(self):
        assert stats.percentile([], 50) == 0.0


class TestRequiredNForCiHalfWidth:
    def test_classic_p_half_reference_value(self):
        assert stats.required_n_for_ci_half_width(0.5) == 97

    def test_clamped_at_extremes(self):
        # p=0 or p=1 clamp to 0.01/0.99 rather than yielding n=0.
        n_zero = stats.required_n_for_ci_half_width(0.0)
        n_one = stats.required_n_for_ci_half_width(1.0)
        assert n_zero > 0
        assert n_one > 0
        assert n_zero == n_one
