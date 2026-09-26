"""Tests for the Jev question set loader (COMMUNITY-HEALTH.md §1.2/§4, DECISIONS.md D17).

Offline only: no network access, no TYPESAFE_API_KEY, no calls to TypeSafe. These tests
check that `questions_v1.yaml` loads, is internally consistent, and yields the expected
`typesafe_sdk` question objects -- not that Jev answers correctly.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml
from typesafe_sdk import Noul, Score

from project_health.classify.questions import (
    DEFAULT_QUESTION_SET_PATH,
    MESSAGE_LEVEL_LABELS,
    THREAD_LEVEL_LABELS,
    QuestionSetError,
    load_question_set,
)

# COMMUNITY-HEALTH.md §1.2 table rows 13-18 -- must never be asked of the classifier.
THREAD_LEVEL_LABELS_FROM_SPEC = {
    "escalation",
    "de-escalation",
    "pile-on",
    "resolution",
    "thread_abandonment",
    "newcomer_treatment",
}


def _load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_default_question_set_loads():
    qs = load_question_set()
    assert qs.version == 1
    assert qs.model == "jev-1.13.0"


def test_every_message_level_label_covered_exactly_once():
    qs = load_question_set()
    assert set(qs.labels.keys()) == MESSAGE_LEVEL_LABELS
    assert len(qs.labels) == len(MESSAGE_LEVEL_LABELS) == 12


def test_no_thread_level_label_is_asked():
    qs = load_question_set()
    asked = set(qs.labels.keys())
    assert asked.isdisjoint(THREAD_LEVEL_LABELS_FROM_SPEC)
    assert asked.isdisjoint(THREAD_LEVEL_LABELS)


def test_every_label_has_null_threshold_and_valid_group():
    qs = load_question_set()
    for label_id, spec in qs.labels.items():
        assert spec["threshold"] is None, f"{label_id} threshold must be null pending #47"
        assert spec["label_group"] in {"reputational_harm", "friction", "argument"}
        assert spec["unit"] == "message"
        assert spec["primitive"] == "noul"
        assert spec["instructions"]
        assert "true" in spec["criteria"] and "false" in spec["criteria"]


def test_label_group_matches_section_6_4_gate_groups():
    qs = load_question_set()
    reputational_harm = {
        label for label, spec in qs.labels.items() if spec["label_group"] == "reputational_harm"
    }
    friction = {label for label, spec in qs.labels.items() if spec["label_group"] == "friction"}
    argument = {label for label, spec in qs.labels.items() if spec["label_group"] == "argument"}

    assert reputational_harm == {"personal_attack", "hostility", "gatekeeping"}
    assert friction == {"dismissiveness", "sarcasm", "status_authority_invocation"}
    assert argument == {
        "technical_disagreement",
        "constructive_counterargument",
        "evidence_based_argument",
        "compromise_offer",
        "acknowledgment",
        "resolution_marker",
    }


def test_tone_intensity_score_present_and_self_contained():
    qs = load_question_set()
    assert qs.score["id"] == "tone_intensity"
    assert qs.score["primitive"] == "score"
    levels = qs.score["levels"]
    assert 2 <= len(levels) <= 10
    for level in levels:
        assert level["description"]
        # Self-contained: no reference to a neighboring level's ordinal.
        assert "level " not in level["description"].lower()


def test_state_schema_has_required_fields_and_justifies_subject_topic():
    qs = load_question_set()
    schema = qs.state_schema
    assert "message.text" in schema
    assert schema["message.text"]["nullable"] is False
    assert "parent.text" in schema
    assert schema["parent.text"]["nullable"] is True
    assert "message.source" in schema
    assert set(schema["message.source"]["enum"]) == {
        "mailing_list",
        "jira_comment",
        "github_pr_comment",
    }
    # thread.subject_topic is documented but not referenced by any v1 question.
    assert "thread.subject_topic" in schema
    assert schema["thread.subject_topic"]["used_by"] == []
    referenced_fields = {ref for spec in qs.labels.values() for ref in spec["state_refs"]}
    referenced_fields |= set(qs.score["state_refs"])
    assert "thread.subject_topic" not in referenced_fields


def test_build_questions_returns_typesafe_sdk_objects():
    qs = load_question_set()
    questions = qs.build_questions()

    assert set(questions.keys()) == MESSAGE_LEVEL_LABELS | {"tone_intensity"}

    for label_id in MESSAGE_LEVEL_LABELS:
        noul = questions[label_id]
        assert isinstance(noul, Noul)
        assert noul.type == "noul"
        assert noul.instructions
        assert set(noul.criteria.keys()) == {"true", "false"}

    score = questions["tone_intensity"]
    assert isinstance(score, Score)
    assert score.type == "score"
    assert len(score.criteria) == len(qs.score["levels"])
    # Levels are passed low-to-high, matching the ordered rubric semantics of Score.
    assert score.criteria[0] == qs.score["levels"][0]["description"]
    assert score.criteria[-1] == qs.score["levels"][-1]["description"]


def test_missing_message_level_label_is_rejected(tmp_path):
    raw = _load_yaml(DEFAULT_QUESTION_SET_PATH)
    raw["labels"] = [item for item in raw["labels"] if item["id"] != "personal_attack"]
    bad_path = tmp_path / "questions_bad.yaml"
    bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(QuestionSetError, match="personal_attack"):
        load_question_set(bad_path)


def test_thread_level_label_is_rejected(tmp_path):
    raw = _load_yaml(DEFAULT_QUESTION_SET_PATH)
    raw["labels"].append(
        {
            "id": "escalation",
            "unit": "message",
            "primitive": "noul",
            "label_group": "friction",
            "threshold": None,
            "state_refs": ["message.text"],
            "instructions": "does this escalate?",
            "criteria": {"true": "yes", "false": "no"},
        }
    )
    bad_path = tmp_path / "questions_bad.yaml"
    bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(QuestionSetError, match="thread-level label"):
        load_question_set(bad_path)


def test_non_null_threshold_is_rejected(tmp_path):
    raw = _load_yaml(DEFAULT_QUESTION_SET_PATH)
    raw["labels"][0]["threshold"] = 0.8
    bad_path = tmp_path / "questions_bad.yaml"
    bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(QuestionSetError, match="threshold must be null"):
        load_question_set(bad_path)


def test_wrong_model_pin_is_rejected(tmp_path):
    raw = _load_yaml(DEFAULT_QUESTION_SET_PATH)
    raw["model"] = "jev-latest"
    bad_path = tmp_path / "questions_bad.yaml"
    bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(QuestionSetError, match="jev-1.13.0"):
        load_question_set(bad_path)


def test_wrong_version_is_rejected(tmp_path):
    raw = _load_yaml(DEFAULT_QUESTION_SET_PATH)
    raw["version"] = 2
    bad_path = tmp_path / "questions_bad.yaml"
    bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(QuestionSetError, match="version"):
        load_question_set(bad_path)


def test_invalid_label_group_is_rejected(tmp_path):
    raw = _load_yaml(DEFAULT_QUESTION_SET_PATH)
    raw["labels"][0]["label_group"] = "not_a_real_group"
    bad_path = tmp_path / "questions_bad.yaml"
    bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(QuestionSetError, match="label_group"):
        load_question_set(bad_path)


def test_duplicate_label_is_rejected(tmp_path):
    raw = _load_yaml(DEFAULT_QUESTION_SET_PATH)
    raw["labels"].append(dict(raw["labels"][0]))
    bad_path = tmp_path / "questions_bad.yaml"
    bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(QuestionSetError, match="more than once"):
        load_question_set(bad_path)


def test_top_level_must_be_a_mapping(tmp_path):
    bad_path = tmp_path / "not_a_mapping.yaml"
    bad_path.write_text(textwrap.dedent("- just\n- a\n- list\n"), encoding="utf-8")

    with pytest.raises(QuestionSetError, match="mapping"):
        load_question_set(bad_path)
