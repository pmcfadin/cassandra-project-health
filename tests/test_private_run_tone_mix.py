"""Tests for project_health.private_run.tone_mix (issue #153:
"Conversations: tone over time" -- COMMUNITY-HEALTH.md §1.2/§2.2's
intensity tiers, §5.1's minimum-sample floors)."""

from __future__ import annotations

from project_health.private_run.aggregate import MIN_DISTINCT_AUTHORS, MIN_MESSAGES
from project_health.private_run.stats import ThreadCluster
from project_health.private_run.tone_mix import (
    TIERS,
    TONE_MIX_CUTOFFS,
    TONE_MIX_HEADLINE_CUTOFF,
    TONE_MIX_SENSITIVITY_CUTOFF,
    TierCluster,
    aggregate_tone_mix_cell,
    bootstrap_tier_shares_ci,
    from_message_clusters,
    message_count,
    weighted_tier_shares,
)


def _message_clusters(n_threads: int, messages_per_thread: int, **labels) -> list[ThreadCluster]:
    return [
        ThreadCluster(
            thread_id=f"t{i}",
            weight=1.0,
            messages=tuple(dict(labels) for _ in range(messages_per_thread)),
        )
        for i in range(n_threads)
    ]


class TestFromMessageClusters:
    def test_derives_tier_from_labels_at_cutoff(self):
        # One hostility=0.9 message (tier 3), one neutral message (tier 0),
        # in the same thread.
        clusters = [
            ThreadCluster(
                thread_id="t1",
                weight=2.0,
                messages=({"hostility": 0.9}, {"technical_disagreement": 0.1}),
            )
        ]
        tier_clusters = from_message_clusters(clusters, 0.5)
        assert len(tier_clusters) == 1
        assert tier_clusters[0].thread_id == "t1"
        assert tier_clusters[0].weight == 2.0
        assert tier_clusters[0].tiers == (3, 0)

    def test_cutoff_changes_which_messages_trigger_a_tier(self):
        clusters = [
            ThreadCluster(thread_id="t1", weight=1.0, messages=({"hostility": 0.6},))
        ]
        at_05 = from_message_clusters(clusters, 0.5)
        at_07 = from_message_clusters(clusters, 0.7)
        assert at_05[0].tiers == (3,)
        assert at_07[0].tiers == (0,)  # 0.6 < 0.7 -> not present -> neutral


class TestWeightedTierShares:
    def test_shares_sum_to_one(self):
        clusters = [
            TierCluster(thread_id="t1", weight=1.0, tiers=(-2, -1, 0, 1)),
            TierCluster(thread_id="t2", weight=2.0, tiers=(2, 3, 4)),
        ]
        shares = weighted_tier_shares(clusters)
        assert set(shares) == set(TIERS)
        assert sum(shares.values()) == 1.0

    def test_empty_clusters_returns_all_zero_not_an_error(self):
        shares = weighted_tier_shares([])
        assert shares == {tier: 0.0 for tier in TIERS}

    def test_weight_is_respected(self):
        # t2's tier-4 messages are weighted 3x -- the weighted share of
        # tier 4 should be well above its raw (unweighted) message share.
        clusters = [
            TierCluster(thread_id="t1", weight=1.0, tiers=(0, 0, 0)),
            TierCluster(thread_id="t2", weight=3.0, tiers=(4,)),
        ]
        shares = weighted_tier_shares(clusters)
        # weighted: 1*3 = 3 messages at tier 0, 3*1 = 3 "messages" of weight
        # at tier 4 -> 0.5/0.5, versus an unweighted 3/4 vs 1/4 split.
        assert shares[0] == 0.5
        assert shares[4] == 0.5


class TestBootstrapTierSharesCi:
    def test_ci_brackets_the_point_estimate(self):
        clusters = [
            TierCluster(thread_id=f"t{i}", weight=1.0, tiers=(0, 0, 1)) for i in range(20)
        ]
        shares = weighted_tier_shares(clusters)
        cis = bootstrap_tier_shares_ci(clusters, seed=7, iterations=200)
        for tier in TIERS:
            lo, hi = cis[tier]
            assert lo <= shares[tier] <= hi

    def test_empty_clusters_returns_zero_interval(self):
        cis = bootstrap_tier_shares_ci([], seed=1, iterations=50)
        assert cis == {tier: (0.0, 0.0) for tier in TIERS}


class TestAggregateToneMixCell:
    def test_below_floor_is_insufficient_data_with_no_tier_values(self):
        clusters = from_message_clusters(
            _message_clusters(1, MIN_MESSAGES - 1, hostility=0.9), TONE_MIX_HEADLINE_CUTOFF
        )
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_tone_mix_cell(clusters, authors, seed=1, cell_key="mailing_list:2024Q1")
        assert cell["insufficient_data"] is True
        assert all(v is None for v in cell["tiers"].values())

    def test_meeting_floor_computes_real_shares_summing_to_one(self):
        clusters = from_message_clusters(
            _message_clusters(1, MIN_MESSAGES, hostility=0.9), TONE_MIX_HEADLINE_CUTOFF
        )
        authors = {f"a{i}" for i in range(MIN_DISTINCT_AUTHORS)}
        cell = aggregate_tone_mix_cell(
            clusters, authors, seed=1, cell_key="mailing_list:2024Q1", bootstrap_iterations=20
        )
        assert cell["insufficient_data"] is False
        assert cell["messages_classified"] == MIN_MESSAGES
        assert cell["distinct_authors"] == MIN_DISTINCT_AUTHORS
        # Every message is hostility=0.9 -> tier 3 -- share should be 1.0
        # there and 0.0 everywhere else, summing to 1.0 overall.
        tiers = cell["tiers"]
        assert tiers["3"]["share"] == 1.0
        assert tiers["3"]["name"] == "Hostile"
        for tier in TIERS:
            if tier != 3:
                assert tiers[str(tier)]["share"] == 0.0
        assert sum(entry["share"] for entry in tiers.values()) == 1.0
        lo, hi = tiers["3"]["ci95"]
        assert lo <= 1.0 <= hi

    def test_every_tier_key_present_at_both_headline_and_sensitivity_cutoffs(self):
        assert TONE_MIX_CUTOFFS == (TONE_MIX_HEADLINE_CUTOFF, TONE_MIX_SENSITIVITY_CUTOFF)


class TestMessageCount:
    def test_counts_every_message_across_clusters(self):
        clusters = [
            TierCluster(thread_id="t1", weight=1.0, tiers=(0, 1)),
            TierCluster(thread_id="t2", weight=1.0, tiers=(2,)),
        ]
        assert message_count(clusters) == 3
