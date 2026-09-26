"""Tests for the labeling server's pure request logic (issue #46).

These exercise `build_state_payload` / `validate_save_payload` /
`make_label_record` / `build_app_state` directly -- the real code the HTTP
handler calls -- with no sockets involved, per `tests/conftest.py`'s
autouse network block. The actual `BaseHTTPRequestHandler` wiring
(`LabelRequestHandler`, `make_server`) is verified by hand against a real
running server (see the issue's acceptance criteria), not by this suite.

Also covers the orchestrator's correction (2026-09-25): the rater's tone
scale and message-level label list must come from `classify/
questions_v1.yaml` at runtime, not be hard-coded, and must match it exactly
so a human rating and a Jev classification answer the same question.

All corpus/message text here is synthetic, never real Cassandra content.
"""

from __future__ import annotations

import json

import pytest

from project_health.label.label_set import load_label_set
from project_health.label.question_set import load_question_set_summary
from project_health.label.server import (
    LabelQuestionMismatchError,
    SavePayloadError,
    _assert_labels_match_questions,
    build_app_state,
    build_state_payload,
    make_label_record,
    validate_save_payload,
)
from project_health.label.store import append_label_record, read_label_records

_SYNTHETIC_ITEMS = [
    {
        "id": "item-1",
        "stratum": "prevalence",
        "source": "mailing_list",
        "archive_url": "https://example.invalid/thread/1",
        "text": "I don't think a single lock will hold up under this write pattern.",
        "parent_text": None,
        "checksum": "aaa111",
    },
    {
        "id": "item-2",
        "stratum": "rare_label_enrichment",
        "source": "jira_comment",
        "archive_url": "https://example.invalid/issue/2",
        "text": "Thanks for digging into this, good catch.",
        "parent_text": "Here's the flame graph showing the hot path.",
        "checksum": "bbb222",
    },
]


def _write_corpus(path, items=_SYNTHETIC_ITEMS):
    with open(path, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item))
            fh.write("\n")
    return path


@pytest.fixture
def app(tmp_path):
    corpus_path = _write_corpus(tmp_path / "corpus.jsonl")
    labels_path = tmp_path / "labels.jsonl"
    return build_app_state(corpus_path=corpus_path, labels_path=labels_path, rater="pmcfadin")


def _all_labels_marked(mark="no"):
    label_set = load_label_set()
    return {label.id: mark for label in label_set.ratable}


class TestBuildStatePayload:
    def test_first_item_is_item_1_and_progress_is_zero_of_total(self, app):
        payload = build_state_payload(app)
        assert payload["item"]["id"] == "item-1"
        assert payload["progress"] == {"done": 0, "total": 2}
        assert payload["complete"] is False

    def test_item_view_is_blind_no_stratum_no_checksum(self, app):
        payload = build_state_payload(app)
        assert "stratum" not in payload["item"]
        assert "checksum" not in payload["item"]
        assert set(payload["item"]) == {"id", "source", "text", "parent_text", "archive_url"}

    def test_response_never_contains_any_jev_style_output(self, app):
        payload = build_state_payload(app)
        blob = json.dumps(payload)
        for forbidden in ("jev", "classifier", "probability", "confidence"):
            assert forbidden not in blob.lower()

    def test_labels_list_is_message_level_only(self, app):
        payload = build_state_payload(app)
        assert len(payload["labels"]) == 12
        ids = {label["id"] for label in payload["labels"]}
        assert "escalation" not in ids  # thread-level, never shown for rating

    def test_complete_when_all_items_labeled(self, app):
        app.records["item-1"] = {"item_id": "item-1"}
        app.records["item-2"] = {"item_id": "item-2"}
        payload = build_state_payload(app)
        assert payload["complete"] is True
        assert payload["item"] is None
        assert payload["progress"] == {"done": 2, "total": 2}

    def test_skips_to_second_item_when_first_is_labeled(self, app):
        app.records["item-1"] = {"item_id": "item-1"}
        payload = build_state_payload(app)
        assert payload["item"]["id"] == "item-2"
        assert payload["progress"] == {"done": 1, "total": 2}

    def test_tone_levels_come_from_questions_v1_yaml_verbatim(self, app):
        """Orchestrator correction: tone options shown to the rater must be
        loaded from classify/questions_v1.yaml's tone_intensity levels, not
        hard-coded, with each description shown verbatim."""
        question_set = load_question_set_summary()
        payload = build_state_payload(app)
        assert payload["tone_levels"] == [level.to_dict() for level in question_set.tone_levels]
        # Specifically: 5 levels (0-4), not a hard-coded 0-2 scale.
        assert [level["level"] for level in payload["tone_levels"]] == [0, 1, 2, 3, 4]
        for level in payload["tone_levels"]:
            assert level["description"]  # verbatim, non-empty

    def test_question_set_version_is_included(self, app):
        question_set = load_question_set_summary()
        payload = build_state_payload(app)
        assert payload["question_set_version"] == question_set.version


class TestLabelsMatchQuestionSet:
    """Orchestrator correction: the message-level labels shown to the rater
    must be identical to the 12 Noul labels in questions_v1.yaml."""

    def test_ratable_label_ids_equal_question_set_label_ids(self):
        label_set = load_label_set()
        question_set = load_question_set_summary()
        ratable_ids = {label.id for label in label_set.ratable}
        assert ratable_ids == set(question_set.message_label_ids)
        assert len(ratable_ids) == 12

    def test_assert_labels_match_questions_passes_for_the_real_packaged_files(self):
        label_set = load_label_set()
        question_set = load_question_set_summary()
        _assert_labels_match_questions(label_set, question_set)  # must not raise

    def test_assert_labels_match_questions_raises_on_mismatch(self):
        label_set = load_label_set()
        question_set = load_question_set_summary()
        mismatched_ids = tuple(question_set.message_label_ids[:-1]) + ("something_else",)
        bad_question_set = question_set.__class__(
            version=question_set.version,
            message_label_ids=mismatched_ids,
            tone_levels=question_set.tone_levels,
        )
        with pytest.raises(LabelQuestionMismatchError):
            _assert_labels_match_questions(label_set, bad_question_set)

    def test_build_app_state_raises_on_mismatch(self, tmp_path):
        corpus_path = _write_corpus(tmp_path / "corpus.jsonl")
        labels_path = tmp_path / "labels.jsonl"
        bad_question_set_path = tmp_path / "questions.yaml"
        bad_question_set_path.write_text(
            "version: 1\n"
            "labels:\n"
            "  - {id: not_a_real_label}\n"
            "score:\n"
            "  id: tone_intensity\n"
            "  levels:\n"
            "    - {level: 0, name: calm, description: 'calm.'}\n",
            encoding="utf-8",
        )
        with pytest.raises(LabelQuestionMismatchError):
            build_app_state(
                corpus_path=corpus_path,
                labels_path=labels_path,
                rater="pmcfadin",
                question_set_path=bad_question_set_path,
            )


class TestValidateSavePayload:
    def test_valid_payload_normalizes(self, app):
        body = {
            "item_id": "item-1",
            "labels": _all_labels_marked("yes"),
            "tone": 1,
            "note": "a note",
            "seconds": 12.3,
        }
        normalized = validate_save_payload(
            body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
        )
        assert normalized["item_id"] == "item-1"
        assert normalized["tone"] == 1
        assert normalized["seconds"] == 12.3

    def test_valid_payload_accepts_tone_4(self, app):
        """The real tone scale is 0-4 (five levels), not 0-2."""
        body = {"item_id": "item-1", "labels": _all_labels_marked(), "tone": 4, "seconds": 1}
        normalized = validate_save_payload(
            body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
        )
        assert normalized["tone"] == 4

    def test_unknown_item_id_rejected(self, app):
        body = {"item_id": "nope", "labels": _all_labels_marked(), "tone": 0, "seconds": 1}
        with pytest.raises(SavePayloadError):
            validate_save_payload(
                body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
            )

    def test_missing_label_rejected(self, app):
        labels = _all_labels_marked()
        del labels["hostility"]
        body = {"item_id": "item-1", "labels": labels, "tone": 0, "seconds": 1}
        with pytest.raises(SavePayloadError):
            validate_save_payload(
                body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
            )

    def test_unknown_label_id_rejected(self, app):
        labels = _all_labels_marked()
        labels["escalation"] = "yes"  # thread-level, not ratable
        body = {"item_id": "item-1", "labels": labels, "tone": 0, "seconds": 1}
        with pytest.raises(SavePayloadError):
            validate_save_payload(
                body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
            )

    def test_invalid_mark_rejected(self, app):
        labels = _all_labels_marked()
        labels["hostility"] = "maybe"
        body = {"item_id": "item-1", "labels": labels, "tone": 0, "seconds": 1}
        with pytest.raises(SavePayloadError):
            validate_save_payload(
                body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
            )

    @pytest.mark.parametrize("bad_tone", [-1, 5, "1", None, True])
    def test_invalid_tone_rejected(self, app, bad_tone):
        body = {"item_id": "item-1", "labels": _all_labels_marked(), "tone": bad_tone, "seconds": 1}
        with pytest.raises(SavePayloadError):
            validate_save_payload(
                body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
            )

    def test_negative_seconds_rejected(self, app):
        body = {"item_id": "item-1", "labels": _all_labels_marked(), "tone": 0, "seconds": -1}
        with pytest.raises(SavePayloadError):
            validate_save_payload(
                body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
            )

    def test_note_may_be_omitted(self, app):
        body = {"item_id": "item-1", "labels": _all_labels_marked(), "tone": 0, "seconds": 1}
        normalized = validate_save_payload(
            body, app.label_set, {"item-1", "item-2"}, app.question_set.tone_level_numbers()
        )
        assert normalized["note"] is None


class TestMakeLabelRecord:
    def test_record_has_exactly_the_required_fields(self):
        normalized = {
            "item_id": "item-1",
            "labels": {"hostility": "no"},
            "tone": 0,
            "note": None,
            "seconds": 5.0,
        }
        record = make_label_record(
            normalized,
            rater="pmcfadin",
            corpus_checksum="deadbeef",
            label_set_version=1,
            question_set_version=1,
            saved_at="2026-09-25T00:00:00+00:00",
        )
        assert set(record) == {
            "item_id",
            "rater",
            "labels",
            "tone",
            "note",
            "seconds",
            "saved_at",
            "corpus_checksum",
            "label_set_version",
            "question_set_version",
        }
        assert record["rater"] == "pmcfadin"
        assert record["corpus_checksum"] == "deadbeef"
        assert record["label_set_version"] == 1
        assert record["question_set_version"] == 1


class TestResumeAcrossRestarts:
    def test_reloading_app_state_after_save_resumes_at_next_item(self, tmp_path):
        corpus_path = _write_corpus(tmp_path / "corpus.jsonl")
        labels_path = tmp_path / "labels.jsonl"

        app1 = build_app_state(corpus_path=corpus_path, labels_path=labels_path, rater="pmcfadin")
        payload = build_state_payload(app1)
        assert payload["item"]["id"] == "item-1"

        normalized = validate_save_payload(
            {
                "item_id": "item-1",
                "labels": _all_labels_marked(),
                "tone": 0,
                "seconds": 3,
            },
            app1.label_set,
            {"item-1", "item-2"},
            app1.question_set.tone_level_numbers(),
        )
        record = make_label_record(
            normalized,
            rater="pmcfadin",
            corpus_checksum=app1.corpus_checksum,
            label_set_version=1,
            question_set_version=app1.question_set.version,
        )
        append_label_record(labels_path, record)

        # Simulate a full server restart: rebuild AppState from disk only.
        app2 = build_app_state(corpus_path=corpus_path, labels_path=labels_path, rater="pmcfadin")
        assert read_label_records(labels_path) == {"item-1": record}
        resumed_payload = build_state_payload(app2)
        assert resumed_payload["item"]["id"] == "item-2"
        assert resumed_payload["progress"] == {"done": 1, "total": 2}
