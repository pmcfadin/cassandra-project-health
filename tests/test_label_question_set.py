"""Tests for the labeling tool's read-only view of `classify/questions_v1.
yaml` (issue #46, orchestrator correction 2026-09-25).

This module must read the *same* file `classify/questions.py` validates and
builds `typesafe_sdk` objects from, without importing that module (no
`typesafe_sdk` dependency for the labeling tool -- D22).
"""

from __future__ import annotations

import pytest
import yaml

from project_health.label.question_set import (
    DEFAULT_QUESTION_SET_PATH,
    QuestionSetReadError,
    ToneLevel,
    load_question_set_summary,
)


def test_default_path_points_at_classify_questions_v1_yaml():
    assert DEFAULT_QUESTION_SET_PATH.name == "questions_v1.yaml"
    assert DEFAULT_QUESTION_SET_PATH.parent.name == "classify"
    assert DEFAULT_QUESTION_SET_PATH.is_file()


def test_loads_real_packaged_file():
    summary = load_question_set_summary()
    assert summary.version == 1
    assert len(summary.message_label_ids) == 12
    assert len(summary.tone_levels) == 5


def test_tone_levels_are_0_through_4_in_order():
    summary = load_question_set_summary()
    assert [level.level for level in summary.tone_levels] == [0, 1, 2, 3, 4]
    assert summary.tone_level_numbers() == frozenset({0, 1, 2, 3, 4})


def test_tone_level_descriptions_are_nonempty_and_match_yaml_verbatim():
    with open(DEFAULT_QUESTION_SET_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    raw_levels = {item["level"]: item["description"] for item in raw["score"]["levels"]}
    summary = load_question_set_summary()
    for level in summary.tone_levels:
        assert level.description == raw_levels[level.level]


def test_message_label_ids_match_yaml_order_and_content():
    with open(DEFAULT_QUESTION_SET_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    expected_ids = tuple(item["id"] for item in raw["labels"])
    summary = load_question_set_summary()
    assert summary.message_label_ids == expected_ids


class TestLoadQuestionSetSummaryErrors:
    def test_missing_version_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("labels: []\n", encoding="utf-8")
        with pytest.raises(QuestionSetReadError):
            load_question_set_summary(path)

    def test_empty_labels_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("version: 1\nlabels: []\n", encoding="utf-8")
        with pytest.raises(QuestionSetReadError):
            load_question_set_summary(path)

    def test_missing_score_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(
            "version: 1\nlabels:\n  - {id: foo}\n",
            encoding="utf-8",
        )
        with pytest.raises(QuestionSetReadError):
            load_question_set_summary(path)

    def test_empty_levels_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(
            "version: 1\nlabels:\n  - {id: foo}\nscore:\n  id: tone_intensity\n  levels: []\n",
            encoding="utf-8",
        )
        with pytest.raises(QuestionSetReadError):
            load_question_set_summary(path)

    def test_levels_are_sorted_regardless_of_yaml_order(self, tmp_path):
        path = tmp_path / "ok.yaml"
        path.write_text(
            "version: 1\n"
            "labels:\n"
            "  - {id: foo}\n"
            "score:\n"
            "  id: tone_intensity\n"
            "  levels:\n"
            "    - {level: 1, name: b, description: 'b.'}\n"
            "    - {level: 0, name: a, description: 'a.'}\n",
            encoding="utf-8",
        )
        summary = load_question_set_summary(path)
        assert summary.tone_levels == (
            ToneLevel(level=0, name="a", description="a."),
            ToneLevel(level=1, name="b", description="b."),
        )
