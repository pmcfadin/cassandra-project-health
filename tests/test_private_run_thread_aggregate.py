"""Tests for project_health.private_run.thread_aggregate (issue #114:
COMMUNITY-HEALTH.md §5.1 floors, §5.2 formulas). Entirely synthetic
threads/authors."""

from __future__ import annotations

from project_health.private_run.thread_aggregate import (
    NEWCOMER_MIN_DISTINCT_NEWCOMERS,
    NEWCOMER_MIN_MESSAGES,
    THREAD_MIN_DISTINCT_PARTICIPANTS,
    THREAD_MIN_QUALIFYING,
    NewcomerMessage,
    aggregate_newcomer_rows,
    aggregate_thread_metrics,
)
from project_health.private_run.thread_derive import ThreadDerivation


def _thread(
    key,
    *,
    weight=1.0,
    participants,
    has_technical_disagreement=False,
    has_disagreement_or_friction=False,
    reached_friction=False,
    escalation=False,
    outcome="indeterminate",
    abandoned_target_author=None,
    pile_on_target_authors=(),
):
    return ThreadDerivation(
        thread_key=key,
        weight=weight,
        n_messages=len(participants),
        participant_authors=frozenset(participants),
        has_technical_disagreement=has_technical_disagreement,
        has_disagreement_or_friction=has_disagreement_or_friction,
        reached_friction=reached_friction,
        escalation=escalation,
        deescalation=False,
        outcome=outcome,
        abandoned_target_author=abandoned_target_author,
        pile_on_target_authors=pile_on_target_authors,
    )


def _qualifying_threads(n, *, escalated_n=0, resolved_n=0, abandoned_n=0):
    threads = []
    for i in range(n):
        threads.append(
            _thread(
                f"t{i}",
                participants=[f"author{i}", f"author{i + 1000}"],
                has_technical_disagreement=True,
                has_disagreement_or_friction=True,
                reached_friction=True,
                escalation=i < escalated_n,
                outcome=(
                    "resolved"
                    if i < resolved_n
                    else ("abandoned" if i < abandoned_n else "indeterminate")
                ),
                abandoned_target_author=f"target{i}" if i < abandoned_n else None,
            )
        )
    return threads


class TestThreadMetricsFloors:
    def test_below_min_qualifying_threads_is_insufficient(self):
        threads = _qualifying_threads(THREAD_MIN_QUALIFYING - 1)
        result = aggregate_thread_metrics(threads, seed=1, cell_key="c1", bootstrap_iterations=10)
        assert result["escalation_rate"]["insufficient_data"] is True
        assert result["escalation_rate"]["rate"] is None

    def test_below_min_distinct_participants_is_insufficient(self):
        # Enough qualifying threads, but every thread shares the same two
        # authors -> distinct-participant floor (5) not cleared.
        threads = [
            _thread(
                f"t{i}",
                participants=["alice", "bob"],
                has_technical_disagreement=True,
                has_disagreement_or_friction=True,
                reached_friction=True,
            )
            for i in range(THREAD_MIN_QUALIFYING + 5)
        ]
        result = aggregate_thread_metrics(threads, seed=1, cell_key="c2", bootstrap_iterations=10)
        assert result["escalation_rate"]["insufficient_data"] is True

    def test_meets_floors_and_computes_a_rate(self):
        threads = _qualifying_threads(THREAD_MIN_QUALIFYING + 5, escalated_n=10)
        result = aggregate_thread_metrics(threads, seed=1, cell_key="c3", bootstrap_iterations=50)
        entry = result["escalation_rate"]
        assert entry["insufficient_data"] is False
        assert entry["rate"] is not None
        assert 0.0 <= entry["rate"] <= 1.0
        assert entry["ci95"] is not None
        assert entry["distinct_participants"] >= THREAD_MIN_DISTINCT_PARTICIPANTS

    def test_escalation_rate_denominator_is_technical_disagreement_threads(self):
        threads = _qualifying_threads(THREAD_MIN_QUALIFYING + 10, escalated_n=5)
        result = aggregate_thread_metrics(threads, seed=2, cell_key="c4", bootstrap_iterations=50)
        rate = result["escalation_rate"]["rate"]
        expected = 5 / (THREAD_MIN_QUALIFYING + 10)
        assert abs(rate - expected) < 1e-9

    def test_constructive_resolution_rate(self):
        threads = _qualifying_threads(THREAD_MIN_QUALIFYING + 10, resolved_n=8)
        result = aggregate_thread_metrics(threads, seed=3, cell_key="c5", bootstrap_iterations=50)
        rate = result["constructive_resolution_rate"]["rate"]
        expected = 8 / (THREAD_MIN_QUALIFYING + 10)
        assert abs(rate - expected) < 1e-9

    def test_thread_abandonment_rate(self):
        threads = _qualifying_threads(THREAD_MIN_QUALIFYING + 10, abandoned_n=6)
        result = aggregate_thread_metrics(threads, seed=4, cell_key="c6", bootstrap_iterations=50)
        rate = result["thread_abandonment_rate_post_friction"]["rate"]
        expected = 6 / (THREAD_MIN_QUALIFYING + 10)
        assert abs(rate - expected) < 1e-9

    def test_weighting_changes_the_point_estimate(self):
        # One heavily-weighted escalated thread should pull the rate up
        # relative to an unweighted (all weight=1) computation.
        threads = _qualifying_threads(THREAD_MIN_QUALIFYING + 10, escalated_n=1)
        heavy = list(threads)
        heavy[0] = _thread(
            heavy[0].thread_key,
            weight=50.0,
            participants=list(heavy[0].participant_authors),
            has_technical_disagreement=True,
            has_disagreement_or_friction=True,
            reached_friction=True,
            escalation=True,
        )
        light_result = aggregate_thread_metrics(
            threads, seed=5, cell_key="w1", bootstrap_iterations=10
        )
        heavy_result = aggregate_thread_metrics(
            heavy, seed=5, cell_key="w2", bootstrap_iterations=10
        )
        assert heavy_result["escalation_rate"]["rate"] > light_result["escalation_rate"]["rate"]


class TestPileOnRate:
    def test_below_floor_is_insufficient(self):
        threads = [
            _thread(f"t{i}", participants=["a", "b"], pile_on_target_authors=("target",))
            for i in range(THREAD_MIN_QUALIFYING - 1)
        ]
        result = aggregate_thread_metrics(threads, seed=1, cell_key="p1", bootstrap_iterations=10)
        assert result["pile_on_rate"]["insufficient_data"] is True

    def test_needs_five_distinct_targets_not_just_thirty_threads(self):
        # 30 threads, but every pile-on targets the *same* one person -> the
        # distinct-targets weaponization guard (§7.5) should still gate it.
        threads = [
            _thread(f"t{i}", participants=["a", "b"], pile_on_target_authors=("same_target",))
            for i in range(THREAD_MIN_QUALIFYING)
        ]
        result = aggregate_thread_metrics(threads, seed=1, cell_key="p2", bootstrap_iterations=10)
        assert result["pile_on_rate"]["insufficient_data"] is True
        assert result["pile_on_rate"]["distinct_targets"] == 1

    def test_meets_floor_with_five_distinct_targets(self):
        threads = []
        for i in range(THREAD_MIN_QUALIFYING):
            target = f"target{i % 5}"
            threads.append(
                _thread(f"t{i}", participants=["a", "b"], pile_on_target_authors=(target,))
            )
        result = aggregate_thread_metrics(threads, seed=1, cell_key="p3", bootstrap_iterations=20)
        entry = result["pile_on_rate"]
        assert entry["insufficient_data"] is False
        assert entry["per_100_threads"] is not None
        assert entry["distinct_targets"] == 5


class TestNewcomerRows:
    def test_below_min_messages_is_insufficient(self):
        messages = [
            NewcomerMessage(target_author=f"n{i}", responder_author="r", tier=-1)
            for i in range(NEWCOMER_MIN_MESSAGES - 1)
        ]
        result = aggregate_newcomer_rows(
            messages, seed=1, cell_key="n1", bootstrap_iterations=10
        )
        assert result["insufficient_data"] is True
        assert result["constructive_response_rate"] is None

    def test_below_min_distinct_newcomers_is_insufficient(self):
        # Enough messages, but all directed at the same single newcomer.
        messages = [
            NewcomerMessage(target_author="only_newcomer", responder_author=f"r{i}", tier=-1)
            for i in range(NEWCOMER_MIN_MESSAGES + 5)
        ]
        result = aggregate_newcomer_rows(
            messages, seed=1, cell_key="n2", bootstrap_iterations=10
        )
        assert result["insufficient_data"] is True
        assert result["distinct_newcomers"] == 1

    def test_rates_reported_separately_and_never_summed_to_one(self):
        messages = []
        for i in range(NEWCOMER_MIN_MESSAGES + 5):
            newcomer = f"newcomer{i % 6}"
            # A mix: some constructive (tier <= -1), some hostile (tier >=
            # 2), and some neutral (tier 0/1) -- neutral messages count in
            # the denominator but neither numerator, so the two rates must
            # not sum to 1.
            if i % 3 == 0:
                tier = -1
            elif i % 3 == 1:
                tier = 2
            else:
                tier = 0
            messages.append(
                NewcomerMessage(target_author=newcomer, responder_author=f"r{i}", tier=tier)
            )
        result = aggregate_newcomer_rows(
            messages, seed=2, cell_key="n3", bootstrap_iterations=50
        )
        assert result["insufficient_data"] is False
        constructive = result["constructive_response_rate"]
        hostile = result["dismissive_hostile_response_rate"]
        assert constructive is not None and hostile is not None
        assert constructive + hostile < 1.0  # the neutral-tier third is excluded from both
        assert result["distinct_newcomers"] >= NEWCOMER_MIN_DISTINCT_NEWCOMERS
        assert result["constructive_response_rate_ci95"] is not None
        assert result["dismissive_hostile_response_rate_ci95"] is not None

    def test_all_constructive_gives_rate_one_and_hostile_rate_zero(self):
        messages = [
            NewcomerMessage(target_author=f"n{i % 6}", responder_author=f"r{i}", tier=-2)
            for i in range(NEWCOMER_MIN_MESSAGES + 5)
        ]
        result = aggregate_newcomer_rows(
            messages, seed=3, cell_key="n4", bootstrap_iterations=20
        )
        assert result["constructive_response_rate"] == 1.0
        assert result["dismissive_hostile_response_rate"] == 0.0
