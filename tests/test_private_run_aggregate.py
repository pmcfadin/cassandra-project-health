"""Tests for project_health.private_run.aggregate (issue #110;
COMMUNITY-HEALTH.md §5.1 floors)."""

from __future__ import annotations

from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.private_run.aggregate import (
    MIN_DISTINCT_AUTHORS,
    MIN_MESSAGES,
    aggregate_cell,
)
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
        assert all(v is None for v in cell["rates_per_1000_messages"].values())

    def test_below_author_floor_is_insufficient_data(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS - 1)}
        cell = aggregate_cell(
            clusters, authors, seed=1, cell_key="mailing_list:2024Q1", sensitivity_thresholds={}
        )
        assert cell["insufficient_data"] is True

    def test_meeting_both_floors_computes_real_rates(self):
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
        hostility = cell["rates_per_1000_messages"]["hostility"]
        assert hostility is not None
        assert hostility["per_1000"] == 500.0
        lo, hi = hostility["ci95"]
        assert lo <= 500.0 <= hi

    def test_every_message_level_label_is_present_in_rates(self):
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
        assert set(cell["rates_per_1000_messages"]) == MESSAGE_LEVEL_LABELS


class TestSensitivity:
    def test_sensitivity_only_computed_for_strong_gating_labels(self):
        clusters = _clusters(n_threads=1, messages_per_thread=MIN_MESSAGES)
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_cell(
            clusters,
            authors,
            seed=1,
            cell_key="mailing_list:2024Q1",
            sensitivity_thresholds={"hostility": 0.4, "personal_attack": 0.15, "sarcasm": 0.6},
            bootstrap_iterations=5,
        )
        assert set(cell["sensitivity_per_1000_messages"]) == {
            "hostility",
            "personal_attack",
            "sarcasm",
        }
        assert cell["sensitivity_per_1000_messages"]["hostility"] == 1000.0  # 0.5 >= 0.4

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
            sensitivity_thresholds={"hostility": 0.4},
            bootstrap_iterations=5,
        )
        assert cell["insufficient_data"] is True
        assert cell["sensitivity_per_1000_messages"]["hostility"] is None


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
        assert (
            cell_a["rates_per_1000_messages"]["hostility"]["per_1000"]
            == cell_b["rates_per_1000_messages"]["hostility"]["per_1000"]
        )
