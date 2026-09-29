"""Tests for project_health.private_run.newcomer (issue #114: COMMUNITY-
HEALTH.md §2.3 rule 8 -- newcomer determination against the whole Phase-1
history, not just the sample)."""

from __future__ import annotations

from datetime import datetime, timezone

from project_health.private_run.newcomer import is_newcomer, prior_message_count


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestPriorMessageCount:
    def test_no_history_at_all_is_zero(self):
        assert prior_message_count({}, "alice@example.org", _ts(2024, 1, 1)) == 0

    def test_counts_only_strictly_before(self):
        history = {"alice@example.org": [_ts(2024, 1, 1), _ts(2024, 1, 2), _ts(2024, 1, 3)]}
        assert prior_message_count(history, "alice@example.org", _ts(2024, 1, 2)) == 1
        assert prior_message_count(history, "alice@example.org", _ts(2024, 1, 4)) == 3
        assert prior_message_count(history, "alice@example.org", _ts(2023, 12, 1)) == 0

    def test_accepts_iso_string_before_value(self):
        history = {"alice@example.org": [_ts(2024, 1, 1)]}
        assert prior_message_count(history, "alice@example.org", "2024-01-02T00:00:00+00:00") == 1

    def test_accepts_jira_style_timestamp_string(self):
        history = {"dave": [_ts(2024, 1, 1)]}
        assert prior_message_count(history, "dave", "2024-01-02T10:00:00.000+0000") == 1
        assert prior_message_count(history, "dave", "2023-12-31T10:00:00.000+0000") == 0


class TestIsNewcomer:
    def test_first_ever_message_is_a_newcomer(self):
        assert is_newcomer({}, "alice@example.org", _ts(2024, 1, 1), n=3) is True

    def test_fewer_than_n_prior_messages_is_a_newcomer(self):
        history = {"alice@example.org": [_ts(2024, 1, 1), _ts(2024, 1, 2)]}
        assert is_newcomer(history, "alice@example.org", _ts(2024, 1, 3), n=3) is True

    def test_n_or_more_prior_messages_is_not_a_newcomer(self):
        history = {
            "alice@example.org": [_ts(2024, 1, 1), _ts(2024, 1, 2), _ts(2024, 1, 3)]
        }
        assert is_newcomer(history, "alice@example.org", _ts(2024, 1, 4), n=3) is False

    def test_history_outside_the_sample_still_counts(self):
        # Simulates issue #114's core requirement: a prolific poster whose
        # *older* messages were never sampled into this run's threads must
        # still be recognized as an established contributor, not a
        # newcomer, at a later sampled message's time.
        history = {"prolific@example.org": [_ts(2020, i, 1) for i in range(1, 6)]}
        assert is_newcomer(history, "prolific@example.org", _ts(2024, 1, 1), n=3) is False

    def test_empty_author_is_never_a_newcomer(self):
        assert is_newcomer({}, "", _ts(2024, 1, 1)) is False

    def test_n_is_flag_configurable(self):
        history = {"alice@example.org": [_ts(2024, 1, 1)]}
        assert is_newcomer(history, "alice@example.org", _ts(2024, 1, 2), n=1) is False
        assert is_newcomer(history, "alice@example.org", _ts(2024, 1, 2), n=2) is True
