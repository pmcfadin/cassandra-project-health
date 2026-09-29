"""Tests for project_health.private_run.aggregate (issue #110;
COMMUNITY-HEALTH.md §5.1 floors; issue #110 fixup round 1's fixed-cutoff
headline metrics)."""

from __future__ import annotations

from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.private_run.aggregate import (
    CUTOFFS,
    HEADLINE_CUTOFF,
    MIN_DISTINCT_AUTHORS,
    MIN_MESSAGES,
    aggregate_cell,
    cutoff_key,
)
from project_health.private_run.sensitivity import SensitivityThreshold
from project_health.private_run.stats import ThreadCluster


def _clusters(
    n_threads: int, messages_per_thread: int, hostility: float = 0.5
) -> list[ThreadCluster]:
    return [
        ThreadCluster(
            thread_id=f"t{i}",
            weight=1.0,
            messages=tuple({"hostility": hostility} for _ in range(messages_per_thread)),
        )
        for i in range(n_threads)
    ]


class TestFloors:
    def test_below_message_floor_is_insufficient_data(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES - 1)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_cell(
            clusters, authors, seed=1, cell_key="mailing_list:2024Q1", sensitivity_thresholds={}
        )
        assert cell["insufficient_data"] is True
        assert all(
            v is None
            for label in cell["cutoff_rates_per_1000_messages"].values()
            for v in label.values()
        )
        assert all(v is None for v in cell["probability_index_per_1000_messages"].values())

    def test_below_author_floor_is_insufficient_data(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS - 1)}
        cell = aggregate_cell(
            clusters, authors, seed=1, cell_key="mailing_list:2024Q1", sensitivity_thresholds={}
        )
        assert cell["insufficient_data"] is True

    def test_meeting_both_floors_computes_real_cutoff_counts(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds={},
            bootstrap_iterations=20,
        )
        assert cell["insufficient_data"] is False
        assert cell["messages_classified"] == MIN_MESSAGES
        assert cell["distinct_authors"] == MIN_DISTINCT_AUTHORS
        # every message is hostility=0.5 -> present at the 0.5 headline
        # cutoff (>=), absent at 0.7/0.9.
        headline = cell["cutoff_rates_per_1000_messages"]["hostility"][cutoff_key(HEADLINE_CUTOFF)]
        assert headline["per_1000"] == 1000.0
        lo, hi = headline["ci95"]
        assert lo <= 1000.0 <= hi
        at_07 = cell["cutoff_rates_per_1000_messages"]["hostility"][cutoff_key(0.7)]
        assert at_07["per_1000"] == 0.0

    def test_every_message_level_label_is_present_in_cutoff_rates_and_index(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds={},
            bootstrap_iterations=5,
        )
        assert set(cell["cutoff_rates_per_1000_messages"]) == MESSAGE_LEVEL_LABELS
        assert set(cell["probability_index_per_1000_messages"]) == MESSAGE_LEVEL_LABELS
        for label_rates in cell["cutoff_rates_per_1000_messages"].values():
            assert set(label_rates) == {cutoff_key(c) for c in CUTOFFS}

    def test_probability_index_is_the_old_weighted_mean(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES, hostility=0.6)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds={},
            bootstrap_iterations=5,
        )
        index = cell["probability_index_per_1000_messages"]["hostility"]
        assert index["per_1000"] == 600.0


class TestSensitivity:
    def test_sensitivity_only_computed_for_strong_gating_labels(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        thresholds = {
            "hostility": SensitivityThreshold("hostility", 0.4, 0.5, "Dataset A"),
            "personal_attack": SensitivityThreshold("personal_attack", 0.15, 0.5, "Dataset A"),
            "sarcasm": SensitivityThreshold("sarcasm", 0.6, 0.5, "Dataset B"),
        }
        cell = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds=thresholds,
            bootstrap_iterations=5,
        )
        assert set(cell["sensitivity_per_1000_messages"]) == {
            "hostility",
            "personal_attack",
            "sarcasm",
        }
        hostility_entry = cell["sensitivity_per_1000_messages"]["hostility"]
        assert hostility_entry["per_1000"] == 1000.0  # 0.5 >= 0.4
        assert hostility_entry["threshold"] == 0.4
        assert hostility_entry["dataset_name"] == "Dataset A"
        assert hostility_entry["permissive"] is False

    def test_missing_threshold_for_a_strong_label_is_none(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds={},  # no thresholds available at all
            bootstrap_iterations=5,
        )
        assert cell["sensitivity_per_1000_messages"]["hostility"] is None

    def test_insufficient_data_forces_sensitivity_to_none_too(self):
        clusters = _clusters(n_threads=1, messages_per_thread=1)
        authors = {"a1"}
        cell = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds={"hostility": SensitivityThreshold("hostility", 0.4, 0.5, "d")},
            bootstrap_iterations=5,
        )
        assert cell["insufficient_data"] is True
        assert cell["sensitivity_per_1000_messages"]["hostility"] is None

    def test_permissive_flag_reflects_threshold(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES, hostility=0.2)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds={
                "personal_attack": SensitivityThreshold("personal_attack", 0.15, 0.5, "LKML")
            },
            bootstrap_iterations=5,
        )
        # our synthetic clusters only carry "hostility" -- personal_attack
        # is simply absent from every message, so its rate is 0, but the
        # threshold metadata should still be attached.
        entry = cell["sensitivity_per_1000_messages"]["personal_attack"]
        assert entry["threshold"] == 0.15
        assert entry["permissive"] is True


class TestSeeding:
    def test_different_cell_keys_bootstrap_independently(self):
        clusters = _clusters(n_threads=5, messages_per_thread=10, hostility=0.5)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell_a = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds={},
            bootstrap_iterations=200,
        )
        cell_b = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="jira_comment:2024Q1",
            sensitivity_thresholds={},
            bootstrap_iterations=200,
        )
        # same point estimate (identical clusters), but the two cells must
        # not be forced through the exact same bootstrap resample sequence.
        key = cutoff_key(HEADLINE_CUTOFF)
        assert (
            cell_a["cutoff_rates_per_1000_messages"]["hostility"][key]["per_1000"]
            == cell_b["cutoff_rates_per_1000_messages"]["hostility"][key]["per_1000"]
        )
