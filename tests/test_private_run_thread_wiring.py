"""Tests for the issue #114 wiring functions in project_health.private_run.
runner: `build_thread_derivations` and `build_newcomer_messages`. These are
the functions `run_private_run` itself calls to turn sampled/classified
pending messages into thread-level derivations and newcomer-directed
messages -- exercised directly here (real `_PendingMessage`/
`ClassificationRecord` objects, no HTTP mocking needed) so the join logic
between `runner.py`'s fetch-time bookkeeping and `thread_derive`/`newcomer`
is covered end to end. Entirely synthetic data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from project_health.classify.classifier import (
    ClassificationRecord,
    Label,
    NormalizedMessage,
    ParentContext,
    Usage,
)
from project_health.private_run.newcomer import DEFAULT_NEWCOMER_N
from project_health.private_run.runner import (
    JIRA_COMMENT_VENUE,
    MAILING_LIST_VENUE,
    THREAD_DERIVE_HEADLINE_CUTOFF,
    _PendingMessage,
    build_newcomer_messages,
    build_thread_derivations,
)
from project_health.private_run.thread_derive import OUTCOME_ABANDONED

_T0 = "2024-01-01T00:00:00+00:00"
_T1 = "2024-01-01T00:01:00+00:00"
_T2 = "2024-01-01T00:02:00+00:00"
_J0 = "2024-06-01T00:00:00+00:00"
_J1 = "2024-06-01T00:01:00+00:00"


def _record(labels: dict[str, float]) -> ClassificationRecord:
    return ClassificationRecord(
        message_id="unused",
        thread_id="unused",
        source="mailing_list",
        classifier_version="1.0.0",
        question_set_version="v1",
        model_id="jev-test",
        input_hash="unused",
        classified_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        usage=Usage(input_tokens=1, output_tokens=1),
        labels={label: Label(probability=p) for label, p in labels.items()},
    )


def _pending(
    call_id,
    thread_id,
    *,
    venue=MAILING_LIST_VENUE,
    quarter="2024Q1",
    weight=1.0,
    author_raw,
    order_index,
    posted_at,
    parent_call_id=None,
) -> _PendingMessage:
    return _PendingMessage(
        call_id=call_id,
        thread_id=thread_id,
        venue=venue,
        quarter=quarter,
        weight=weight,
        author_raw=author_raw,
        normalized=NormalizedMessage(
            message_id=call_id, thread_id=thread_id, source="mailing_list", text="x"
        ),
        context=ParentContext(text=None),
        order_index=order_index,
        posted_at=posted_at,
        parent_call_id=parent_call_id,
    )


class TestBuildThreadDerivations:
    def test_groups_by_venue_quarter_and_derives_the_thread(self):
        pending = [
            _pending("mail:m0", "t1", author_raw="alice", order_index=0, posted_at=_T0),
            _pending(
                "mail:m1",
                "t1",
                author_raw="bob",
                order_index=1,
                posted_at=_T1,
                parent_call_id="mail:m0",
            ),
            _pending(
                "mail:m2",
                "t1",
                author_raw="carol",
                order_index=2,
                posted_at=_T2,
                parent_call_id="mail:m1",
            ),
        ]
        records = {
            "mail:m0": _record({}),
            "mail:m1": _record({"technical_disagreement": 0.9}),
            "mail:m2": _record({"hostility": 0.9}),  # directed at bob (parent m1)
        }
        result = build_thread_derivations(pending, records, THREAD_DERIVE_HEADLINE_CUTOFF)
        assert set(result) == {(MAILING_LIST_VENUE, "2024Q1")}
        derivations = result[(MAILING_LIST_VENUE, "2024Q1")]
        assert len(derivations) == 1
        derivation = derivations[0]
        assert derivation.thread_key == "t1"
        assert derivation.reached_friction is True
        assert derivation.outcome == OUTCOME_ABANDONED
        assert derivation.abandoned_target_author == "bob"

    def test_unclassified_messages_are_excluded(self):
        pending = [
            _pending("mail:m0", "t1", author_raw="alice", order_index=0, posted_at=_T0),
            _pending(
                "mail:m1",
                "t1",
                author_raw="bob",
                order_index=1,
                posted_at=_T1,
                parent_call_id="mail:m0",
            ),
        ]
        # Only m0 got a classification record (e.g. a paused partial run) --
        # m1 must not appear in the derived thread at all.
        records = {"mail:m0": _record({})}
        result = build_thread_derivations(pending, records, THREAD_DERIVE_HEADLINE_CUTOFF)
        derivation = result[(MAILING_LIST_VENUE, "2024Q1")][0]
        assert derivation.n_messages == 1

    def test_separate_threads_and_venues_stay_separate(self):
        pending = [
            _pending(
                "mail:m0",
                "t1",
                venue=MAILING_LIST_VENUE,
                author_raw="a",
                order_index=0,
                posted_at="p0",
            ),
            _pending(
                "jira:m0",
                "EXAMPLE-1",
                venue=JIRA_COMMENT_VENUE,
                author_raw="a",
                order_index=0,
                posted_at="p0",
            ),
        ]
        records = {"mail:m0": _record({}), "jira:m0": _record({})}
        result = build_thread_derivations(pending, records, THREAD_DERIVE_HEADLINE_CUTOFF)
        assert set(result) == {(MAILING_LIST_VENUE, "2024Q1"), (JIRA_COMMENT_VENUE, "2024Q1")}

    def test_cutoff_changes_the_derived_tiers(self):
        pending = [
            _pending("mail:m0", "t1", author_raw="alice", order_index=0, posted_at=_T0),
            _pending(
                "mail:m1",
                "t1",
                author_raw="bob",
                order_index=1,
                posted_at=_T1,
                parent_call_id="mail:m0",
            ),
        ]
        records = {"mail:m0": _record({}), "mail:m1": _record({"hostility": 0.6})}
        at_05 = build_thread_derivations(pending, records, 0.5)[(MAILING_LIST_VENUE, "2024Q1")][0]
        at_07 = build_thread_derivations(pending, records, 0.7)[(MAILING_LIST_VENUE, "2024Q1")][0]
        assert at_05.reached_friction is True
        assert at_07.reached_friction is False


class TestBuildNewcomerMessages:
    def test_message_directed_at_a_newcomer_is_collected(self):
        pending = [
            _pending("mail:m0", "t1", author_raw="newbie", order_index=0, posted_at=_J0),
            _pending(
                "mail:m1",
                "t1",
                author_raw="responder",
                order_index=1,
                posted_at=_J1,
                parent_call_id="mail:m0",
            ),
        ]
        records = {"mail:m0": _record({}), "mail:m1": _record({"hostility": 0.9})}
        history = {MAILING_LIST_VENUE: {}}  # "newbie" has zero prior history -> newcomer
        result = build_newcomer_messages(
            pending, records, history, cutoff=0.5, newcomer_n=DEFAULT_NEWCOMER_N
        )
        messages = result[(MAILING_LIST_VENUE, "2024Q1")]
        assert len(messages) == 1
        assert messages[0].target_author == "newbie"
        assert messages[0].responder_author == "responder"
        assert messages[0].tier == 3  # hostility

    def test_message_directed_at_an_established_author_is_excluded(self):
        pending = [
            _pending("mail:m0", "t1", author_raw="veteran", order_index=0, posted_at=_J0),
            _pending(
                "mail:m1",
                "t1",
                author_raw="responder",
                order_index=1,
                posted_at=_J1,
                parent_call_id="mail:m0",
            ),
        ]
        records = {"mail:m0": _record({}), "mail:m1": _record({"hostility": 0.9})}
        # "veteran" has 5 prior messages, well above the default N=3.
        history = {
            MAILING_LIST_VENUE: {
                "veteran": [datetime(2020, 1, i, tzinfo=timezone.utc) for i in range(1, 6)]
            }
        }
        result = build_newcomer_messages(
            pending, records, history, cutoff=0.5, newcomer_n=DEFAULT_NEWCOMER_N
        )
        assert result == {}

    def test_root_message_with_no_parent_is_never_a_newcomer_directed_message(self):
        pending = [_pending("mail:m0", "t1", author_raw="newbie", order_index=0, posted_at=_J0)]
        records = {"mail:m0": _record({"hostility": 0.9})}
        history = {MAILING_LIST_VENUE: {}}
        result = build_newcomer_messages(pending, records, history, cutoff=0.5, newcomer_n=3)
        assert result == {}

    def test_newcomer_threshold_is_configurable(self):
        pending = [
            _pending("mail:m0", "t1", author_raw="author", order_index=0, posted_at=_J0),
            _pending(
                "mail:m1",
                "t1",
                author_raw="responder",
                order_index=1,
                posted_at=_J1,
                parent_call_id="mail:m0",
            ),
        ]
        records = {"mail:m0": _record({}), "mail:m1": _record({})}
        history = {MAILING_LIST_VENUE: {"author": [datetime(2024, 1, 1, tzinfo=timezone.utc)]}}
        # 1 prior message: newcomer under N=3 (default), not under N=1.
        result_default = build_newcomer_messages(
            pending, records, history, cutoff=0.5, newcomer_n=3
        )
        assert len(result_default[(MAILING_LIST_VENUE, "2024Q1")]) == 1

        result_n1 = build_newcomer_messages(pending, records, history, cutoff=0.5, newcomer_n=1)
        assert result_n1 == {}
