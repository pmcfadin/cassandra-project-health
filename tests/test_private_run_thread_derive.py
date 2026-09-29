"""Tests for project_health.private_run.thread_derive (issue #114:
COMMUNITY-HEALTH.md §2.2 intensity tiers, §2.3 deterministic derivation
rules). Entirely synthetic threads/authors -- never real Cassandra text or
identities.
"""

from __future__ import annotations

from project_health.private_run.thread_derive import (
    OUTCOME_ABANDONED,
    OUTCOME_INDETERMINATE,
    OUTCOME_RESOLVED,
    ThreadMessage,
    derive_thread,
    intensity_tier,
    labels_present_at_cutoff,
)


def _msg(call_id, order, author, parent, labels, cutoff=0.5):
    probabilities = {label: 1.0 for label in labels}
    labels_present = labels_present_at_cutoff(probabilities, cutoff)
    return ThreadMessage(
        call_id=call_id,
        order_index=order,
        author_raw=author,
        posted_at=f"2024-01-01T00:{order:02d}:00+00:00",
        parent_call_id=parent,
        tier=intensity_tier(labels_present),
        labels_present=labels_present,
    )


# --- §2.2 intensity tiers -----------------------------------------------------


class TestIntensityTier:
    def test_no_labels_is_neutral(self):
        assert intensity_tier(labels_present_at_cutoff({}, 0.5)) == 0

    def test_evidence_based_argument_alone_is_neutral(self):
        labels = labels_present_at_cutoff({"evidence_based_argument": 0.9}, 0.5)
        assert intensity_tier(labels) == 0

    def test_technical_disagreement_is_tier_1(self):
        labels = labels_present_at_cutoff({"technical_disagreement": 0.9}, 0.5)
        assert intensity_tier(labels) == 1

    def test_dismissiveness_sarcasm_gatekeeping_authority_are_tier_2(self):
        for label in ("dismissiveness", "sarcasm", "gatekeeping", "status_authority_invocation"):
            labels = labels_present_at_cutoff({label: 0.9}, 0.5)
            assert intensity_tier(labels) == 2, label

    def test_hostility_is_tier_3(self):
        labels = labels_present_at_cutoff({"hostility": 0.9}, 0.5)
        assert intensity_tier(labels) == 3

    def test_personal_attack_is_tier_4(self):
        labels = labels_present_at_cutoff({"personal_attack": 0.9}, 0.5)
        assert intensity_tier(labels) == 4

    def test_resolution_marker_is_tier_minus_2(self):
        labels = labels_present_at_cutoff({"resolution_marker": 0.9}, 0.5)
        assert intensity_tier(labels) == -2

    def test_acknowledgment_and_compromise_offer_are_tier_minus_1(self):
        for label in ("acknowledgment", "compromise_offer"):
            labels = labels_present_at_cutoff({label: 0.9}, 0.5)
            assert intensity_tier(labels) == -1, label

    def test_highest_tier_wins_when_multiple_labels_present(self):
        labels = labels_present_at_cutoff(
            {"acknowledgment": 0.9, "hostility": 0.9, "technical_disagreement": 0.9}, 0.5
        )
        assert intensity_tier(labels) == 3

    def test_cutoff_excludes_below_threshold_probabilities(self):
        labels = labels_present_at_cutoff({"hostility": 0.4}, 0.5)
        assert intensity_tier(labels) == 0
        labels_07 = labels_present_at_cutoff({"hostility": 0.6}, 0.7)
        assert intensity_tier(labels_07) == 0


# --- §2.3 rule 3/4: escalation / de-escalation --------------------------------


class TestEscalationDeescalation:
    def test_climb_to_hostility_by_two_authors_is_escalation(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "alice", "m1", ["dismissiveness"]),
            _msg("m3", 3, "bob", "m2", ["hostility"]),
        ]
        d = derive_thread("t1", 1.0, messages)
        assert d.escalation is True

    def test_single_author_venting_alone_is_not_escalation(self):
        # Only "bob" ever strikes a new running-maximum tier -- one person's
        # multi-message rant, per §2.3 rule 3's own parenthetical.
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["dismissiveness"]),
            _msg("m2", 2, "bob", "m1", ["hostility"]),
            _msg("m3", 3, "bob", "m2", ["personal_attack"]),
        ]
        d = derive_thread("t2", 1.0, messages)
        assert d.escalation is False

    def test_climb_that_never_reaches_tier_3_is_not_escalation(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "alice", "m1", ["dismissiveness"]),
        ]
        d = derive_thread("t3", 1.0, messages)
        assert d.escalation is False

    def test_deescalation_after_peak_with_calm_ending(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "alice", "m1", ["hostility"]),
            _msg("m3", 3, "bob", "m2", ["acknowledgment"]),
        ]
        d = derive_thread("t4", 1.0, messages)
        assert d.escalation is True
        assert d.deescalation is True

    def test_no_deescalation_when_thread_stays_hot(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "alice", "m1", ["hostility"]),
            _msg("m3", 3, "bob", "m2", ["personal_attack"]),
        ]
        d = derive_thread("t5", 1.0, messages)
        assert d.escalation is True
        assert d.deescalation is False

    def test_no_deescalation_without_escalation(self):
        messages = [_msg("m0", 0, "alice", None, ["acknowledgment"])]
        d = derive_thread("t6", 1.0, messages)
        assert d.escalation is False
        assert d.deescalation is False


# --- §2.3 rule 5: pile-on ------------------------------------------------------


class TestPileOn:
    def test_three_distinct_authors_targeting_one_participant_is_pile_on(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["dismissiveness"]),
            _msg("m2", 2, "carol", "m0", ["sarcasm"]),
            _msg("m3", 3, "dave", "m0", ["gatekeeping"]),
        ]
        d = derive_thread("t7", 1.0, messages)
        assert d.pile_on_target_authors == ("alice",)

    def test_two_distinct_authors_is_not_pile_on(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["dismissiveness"]),
            _msg("m2", 2, "carol", "m0", ["sarcasm"]),
        ]
        d = derive_thread("t8", 1.0, messages)
        assert d.pile_on_target_authors == ()

    def test_same_author_replying_twice_does_not_count_twice(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["dismissiveness"]),
            _msg("m2", 2, "bob", "m0", ["sarcasm"]),
            _msg("m3", 3, "carol", "m0", ["gatekeeping"]),
        ]
        d = derive_thread("t9", 1.0, messages)
        assert d.pile_on_target_authors == ()


# --- §2.3 rule 6/7: resolution / abandonment ----------------------------------


class TestOutcome:
    def test_resolution_marker_in_tail_resolves(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "alice", "m1", ["resolution_marker"]),
        ]
        d = derive_thread("t10", 1.0, messages)
        assert d.outcome == OUTCOME_RESOLVED

    def test_compromise_then_different_author_acknowledgment_resolves(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "alice", "m1", ["compromise_offer"]),
            _msg("m3", 3, "bob", "m2", ["acknowledgment"]),
        ]
        d = derive_thread("t11", 1.0, messages)
        assert d.outcome == OUTCOME_RESOLVED

    def test_compromise_then_same_author_acknowledgment_does_not_resolve(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "alice", "m0", ["compromise_offer"]),
            _msg("m2", 2, "alice", "m1", ["acknowledgment"]),
        ]
        d = derive_thread("t12", 1.0, messages)
        assert d.outcome != OUTCOME_RESOLVED

    def test_resolution_marker_followed_by_new_hostility_does_not_resolve(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["resolution_marker"]),
            _msg("m2", 2, "carol", "m1", ["hostility"]),
        ]
        d = derive_thread("t13", 1.0, messages)
        assert d.outcome != OUTCOME_RESOLVED

    def test_target_of_last_friction_message_never_posts_again_is_abandoned(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "carol", "m1", ["hostility"]),  # directed at bob
        ]
        d = derive_thread("t14", 1.0, messages)
        assert d.outcome == OUTCOME_ABANDONED
        assert d.abandoned_target_author == "bob"

    def test_target_posting_again_is_not_abandoned(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "carol", "m1", ["hostility"]),  # directed at bob
            _msg("m3", 3, "bob", "m2", ["technical_disagreement"]),  # bob posts again
        ]
        d = derive_thread("t15", 1.0, messages)
        assert d.outcome == OUTCOME_INDETERMINATE

    def test_thread_with_no_friction_at_all_is_indeterminate(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["evidence_based_argument"]),
        ]
        d = derive_thread("t16", 1.0, messages)
        assert d.outcome == OUTCOME_INDETERMINATE
        assert d.reached_friction is False

    def test_root_level_friction_with_no_parent_is_indeterminate_not_abandoned(self):
        # tier >= 2 on the thread root itself has no parent -> no
        # identifiable directed_at target.
        messages = [_msg("m0", 0, "alice", None, ["dismissiveness"])]
        d = derive_thread("t17", 1.0, messages)
        assert d.reached_friction is True
        assert d.outcome == OUTCOME_INDETERMINATE
        assert d.abandoned_target_author is None


# --- Denominator flags used by thread_aggregate -------------------------------


class TestDenominatorFlags:
    def test_has_technical_disagreement_flag(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
        ]
        d = derive_thread("t18", 1.0, messages)
        assert d.has_technical_disagreement is True

        d2 = derive_thread("t19", 1.0, [_msg("m0", 0, "alice", None, [])])
        assert d2.has_technical_disagreement is False

    def test_participant_authors_is_the_full_distinct_set(self):
        messages = [
            _msg("m0", 0, "alice", None, []),
            _msg("m1", 1, "bob", "m0", ["technical_disagreement"]),
            _msg("m2", 2, "alice", "m1", ["acknowledgment"]),
        ]
        d = derive_thread("t20", 1.0, messages)
        assert d.participant_authors == frozenset({"alice", "bob"})

    def test_weight_is_carried_through_unchanged(self):
        d = derive_thread("t21", 3.5, [_msg("m0", 0, "alice", None, [])])
        assert d.weight == 3.5
