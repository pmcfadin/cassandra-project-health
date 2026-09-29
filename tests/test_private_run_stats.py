"""Tests for project_health.private_run.stats (issue #110)."""

from __future__ import annotations

from project_health.private_run.stats import (
    ThreadCluster,
    bootstrap_multi_metric_ci,
    bootstrap_weighted_rate_ci,
    median_probability_per_1000,
    message_count,
    percentile_interval,
    weighted_rate_per_1000,
    weighted_sensitivity_rate_per_1000,
)


def _cluster(
    thread_id: str, weight: float, probs: list[float], label: str = "hostility"
) -> ThreadCluster:
    return ThreadCluster(
        thread_id=thread_id, weight=weight, messages=tuple({label: p} for p in probs)
    )


class TestMessageCount:
    def test_counts_raw_messages_ignoring_weight(self):
        clusters = [
            _cluster("t1", weight=10.0, probs=[0.1, 0.2]),
            _cluster("t2", weight=1.0, probs=[0.3]),
        ]
        assert message_count(clusters) == 3

    def test_empty_clusters_is_zero(self):
        assert message_count([]) == 0


class TestWeightedRatePer1000:
    def test_unweighted_case_is_plain_mean_times_1000(self):
        # weight=1.0 everywhere -> reduces to an ordinary mean.
        clusters = [_cluster("t1", weight=1.0, probs=[0.0, 1.0])]
        assert weighted_rate_per_1000(clusters, "hostility") == 500.0

    def test_heavier_thread_dominates_the_weighted_mean(self):
        # one heavily-weighted thread with prob 1.0, one lightly-weighted
        # thread with prob 0.0 -- the rate should skew toward the heavy one.
        clusters = [
            _cluster("t1", weight=9.0, probs=[1.0]),
            _cluster("t2", weight=1.0, probs=[0.0]),
        ]
        assert weighted_rate_per_1000(clusters, "hostility") == 900.0

    def test_missing_label_on_a_message_is_skipped_not_zero(self):
        clusters = [
            ThreadCluster(
                thread_id="t1", weight=1.0, messages=({"hostility": 1.0}, {"sarcasm": 1.0})
            ),
        ]
        # the second message has no "hostility" key at all -- it must not
        # count as a hostility=0.0 observation, or the rate would be diluted.
        assert weighted_rate_per_1000(clusters, "hostility") == 1000.0

    def test_no_clusters_is_zero(self):
        assert weighted_rate_per_1000([], "hostility") == 0.0

    def test_no_message_carries_the_label_is_zero(self):
        clusters = [_cluster("t1", weight=1.0, probs=[], label="hostility")]
        assert weighted_rate_per_1000(clusters, "hostility") == 0.0


class TestWeightedSensitivityRate:
    def test_counts_messages_at_or_above_threshold(self):
        clusters = [_cluster("t1", weight=1.0, probs=[0.1, 0.5, 0.9])]
        # threshold 0.5 -> 2 of 3 present -> 666.67 per 1000
        assert round(weighted_sensitivity_rate_per_1000(clusters, "hostility", 0.5), 2) == 666.67

    def test_no_messages_clear_threshold_is_zero(self):
        clusters = [_cluster("t1", weight=1.0, probs=[0.1, 0.2])]
        assert weighted_sensitivity_rate_per_1000(clusters, "hostility", 0.9) == 0.0


class TestPercentileInterval:
    def test_empty_is_zero_zero(self):
        assert percentile_interval([]) == (0.0, 0.0)

    def test_returns_bounds_within_the_data_range(self):
        values = [float(i) for i in range(100)]
        lo, hi = percentile_interval(values, alpha=0.05)
        assert 0.0 <= lo <= hi <= 99.0


class TestBootstrapWeightedRateCi:
    def test_empty_clusters_is_zero_zero(self):
        assert bootstrap_weighted_rate_ci([], "hostility", seed=1, iterations=50) == (0.0, 0.0)

    def test_ci_brackets_the_point_estimate(self):
        clusters = [
            _cluster("t1", weight=1.0, probs=[0.9, 0.8]),
            _cluster("t2", weight=1.0, probs=[0.1, 0.2]),
            _cluster("t3", weight=1.0, probs=[0.5]),
        ]
        point = weighted_rate_per_1000(clusters, "hostility")
        lo, hi = bootstrap_weighted_rate_ci(clusters, "hostility", seed=42, iterations=500)
        assert lo <= point <= hi

    def test_deterministic_given_same_seed(self):
        clusters = [
            _cluster("t1", weight=2.0, probs=[0.9]),
            _cluster("t2", weight=1.0, probs=[0.1]),
        ]
        a = bootstrap_weighted_rate_ci(clusters, "hostility", seed=7, iterations=200)
        b = bootstrap_weighted_rate_ci(clusters, "hostility", seed=7, iterations=200)
        assert a == b

    def test_single_cluster_collapses_ci_to_point_estimate(self):
        # with n=1, every bootstrap resample is [that one cluster] again --
        # the CI must collapse exactly to the point estimate.
        clusters = [_cluster("t1", weight=1.0, probs=[0.3, 0.7])]
        point = weighted_rate_per_1000(clusters, "hostility")
        lo, hi = bootstrap_weighted_rate_ci(clusters, "hostility", seed=1, iterations=100)
        assert lo == hi == point


class TestMedianProbabilityPer1000:
    def test_median_of_probabilities_times_1000(self):
        clusters = [_cluster("t1", weight=1.0, probs=[0.1, 0.2, 0.3])]
        assert median_probability_per_1000(clusters, "hostility") == 200.0

    def test_unweighted_even_with_wildly_different_thread_weights(self):
        # median_probability_per_1000 is documented as deliberately
        # unweighted -- a heavy thread must not skew the floor.
        clusters = [
            _cluster("t1", weight=1000.0, probs=[0.9]),
            _cluster("t2", weight=1.0, probs=[0.1]),
        ]
        assert median_probability_per_1000(clusters, "hostility") == 500.0  # median(0.9, 0.1)

    def test_missing_label_is_excluded_not_zero(self):
        clusters = [
            ThreadCluster(
                thread_id="t1", weight=1.0, messages=({"hostility": 0.8}, {"sarcasm": 0.2})
            )
        ]
        assert median_probability_per_1000(clusters, "hostility") == 800.0

    def test_no_messages_is_zero(self):
        assert median_probability_per_1000([], "hostility") == 0.0


class TestBootstrapMultiMetricCi:
    def test_empty_clusters_returns_zero_zero_for_every_metric(self):
        result = bootstrap_multi_metric_ci([], "hostility", (0.5, 0.7, 0.9), seed=1, iterations=50)
        assert result == {
            "mean": (0.0, 0.0),
            "0.5": (0.0, 0.0),
            "0.7": (0.0, 0.0),
            "0.9": (0.0, 0.0),
        }

    def test_mean_matches_bootstrap_weighted_rate_ci_given_the_same_seed_and_draws(self):
        # bootstrap_multi_metric_ci's "mean" entry must be the identical
        # statistic bootstrap_weighted_rate_ci computes -- same resample
        # sequence given the same seed, since both draw
        # rng.randrange(n) once per cluster per iteration in the same order.
        clusters = [
            _cluster("t1", weight=1.0, probs=[0.9, 0.8]),
            _cluster("t2", weight=1.0, probs=[0.1, 0.2]),
            _cluster("t3", weight=1.0, probs=[0.5]),
        ]
        expected = bootstrap_weighted_rate_ci(clusters, "hostility", seed=42, iterations=300)
        result = bootstrap_multi_metric_ci(clusters, "hostility", (0.5,), seed=42, iterations=300)
        assert result["mean"] == expected

    def test_cutoff_entries_match_independent_sensitivity_ci_shape(self):
        clusters = [
            _cluster("t1", weight=1.0, probs=[0.9, 0.1]),
            _cluster("t2", weight=1.0, probs=[0.6, 0.4]),
        ]
        result = bootstrap_multi_metric_ci(
            clusters, "hostility", (0.5, 0.9), seed=5, iterations=200
        )
        assert set(result) == {"mean", "0.5", "0.9"}
        for key in ("0.5", "0.9"):
            lo, hi = result[key]
            assert 0.0 <= lo <= hi <= 1000.0

    def test_single_cluster_collapses_every_metric_to_its_point_estimate(self):
        clusters = [_cluster("t1", weight=1.0, probs=[0.3, 0.7])]
        result = bootstrap_multi_metric_ci(clusters, "hostility", (0.5,), seed=1, iterations=50)
        assert (
            result["mean"][0] == result["mean"][1] == weighted_rate_per_1000(clusters, "hostility")
        )
        expected_cutoff = weighted_sensitivity_rate_per_1000(clusters, "hostility", 0.5)
        assert result["0.5"][0] == result["0.5"][1] == expected_cutoff

    def test_deterministic_given_same_seed(self):
        clusters = [
            _cluster("t1", weight=2.0, probs=[0.9]),
            _cluster("t2", weight=1.0, probs=[0.1]),
        ]
        a = bootstrap_multi_metric_ci(clusters, "hostility", (0.5, 0.7), seed=7, iterations=200)
        b = bootstrap_multi_metric_ci(clusters, "hostility", (0.5, 0.7), seed=7, iterations=200)
        assert a == b
