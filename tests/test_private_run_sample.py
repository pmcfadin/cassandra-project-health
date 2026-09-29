"""Tests for project_health.private_run.sample (issue #110)."""

from __future__ import annotations

from project_health.private_run.sample import sample_all_strata, sample_stratum


class TestSampleStratum:
    def test_samples_up_to_k_and_computes_weight(self):
        population = [f"thread-{i}" for i in range(100)]
        result = sample_stratum("mailing_list", "2024Q1", population, seed=110, k=10)
        assert result.population == 100
        assert len(result.sampled_ids) == 10
        assert result.weight == 10.0  # 100 / 10
        # every sampled id really came from the population
        assert set(result.sampled_ids) <= set(population)

    def test_population_smaller_than_k_samples_everything(self):
        population = [f"thread-{i}" for i in range(5)]
        result = sample_stratum("jira_comment", "2024Q1", population, seed=110, k=60)
        assert len(result.sampled_ids) == 5
        assert result.weight == 1.0
        assert sorted(result.sampled_ids) == sorted(population)

    def test_empty_population_samples_nothing_and_weight_is_zero(self):
        result = sample_stratum("mailing_list", "2024Q1", [], seed=110, k=60)
        assert result.population == 0
        assert result.sampled_ids == ()
        assert result.weight == 0.0

    def test_deterministic_given_same_seed(self):
        population = [f"thread-{i}" for i in range(200)]
        a = sample_stratum("mailing_list", "2024Q1", population, seed=110, k=20)
        b = sample_stratum("mailing_list", "2024Q1", population, seed=110, k=20)
        assert a.sampled_ids == b.sampled_ids

    def test_different_seed_usually_picks_a_different_sample(self):
        population = [f"thread-{i}" for i in range(200)]
        a = sample_stratum("mailing_list", "2024Q1", population, seed=110, k=20)
        b = sample_stratum("mailing_list", "2024Q1", population, seed=999, k=20)
        assert a.sampled_ids != b.sampled_ids

    def test_independent_of_population_order(self):
        population = [f"thread-{i}" for i in range(200)]
        shuffled = list(reversed(population))
        a = sample_stratum("mailing_list", "2024Q1", population, seed=110, k=20)
        b = sample_stratum("mailing_list", "2024Q1", shuffled, seed=110, k=20)
        assert set(a.sampled_ids) == set(b.sampled_ids)

    def test_venue_and_quarter_namespace_the_sample_independently(self):
        population = [f"thread-{i}" for i in range(200)]
        mail = sample_stratum("mailing_list", "2024Q1", population, seed=110, k=20)
        jira = sample_stratum("jira_comment", "2024Q1", population, seed=110, k=20)
        other_quarter = sample_stratum("mailing_list", "2024Q2", population, seed=110, k=20)
        assert mail.sampled_ids != jira.sampled_ids
        assert mail.sampled_ids != other_quarter.sampled_ids


class TestSampleAllStrata:
    def test_every_requested_quarter_present_even_with_no_population(self):
        frame = {"2024Q1": [f"t{i}" for i in range(5)]}
        result = sample_all_strata("mailing_list", frame, ["2024Q1", "2024Q2"], seed=110, k=60)
        assert set(result) == {"2024Q1", "2024Q2"}
        assert result["2024Q1"].population == 5
        assert result["2024Q2"].population == 0
        assert result["2024Q2"].sampled_ids == ()
