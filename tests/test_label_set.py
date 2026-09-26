"""Tests for the versioned label-set loader (COMMUNITY-HEALTH.md §1.2; issue #46).

The key invariant this file guards: `label_set_v1.yaml` (shipped as package
data) must match `docs/spec/COMMUNITY-HEALTH.md` §1.2's table **verbatim**.
If the spec doc is ever edited without regenerating the packaged file, the
first test below fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_health.label.label_set import (
    DEFAULT_LABEL_SET_PATH,
    LabelSetError,
    load_label_set,
    parse_label_table,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMMUNITY_HEALTH_PATH = _REPO_ROOT / "docs" / "spec" / "COMMUNITY-HEALTH.md"

_MESSAGE_LEVEL_IDS = {
    "technical_disagreement",
    "constructive_counterargument",
    "evidence_based_argument",
    "compromise_offer",
    "acknowledgment",
    "personal_attack",
    "hostility",
    "dismissiveness",
    "sarcasm",
    "gatekeeping",
    "status_authority_invocation",
    "resolution_marker",
}

_THREAD_LEVEL_IDS = {
    "escalation",
    "de-escalation",
    "pile-on",
    "resolution",
    "thread_abandonment",
    "newcomer_treatment",
}


def test_packaged_label_set_matches_community_health_spec_verbatim():
    """Independently re-parse the checked-out COMMUNITY-HEALTH.md §1.2 table
    and assert the packaged label_set_v1.yaml is a byte-for-byte match, field
    by field, for every one of the 18 rows."""
    spec_rows = parse_label_table(_COMMUNITY_HEALTH_PATH)
    label_set = load_label_set()

    assert len(spec_rows) == len(label_set.labels) == 18

    spec_by_id = {row.id: row for row in spec_rows}
    packaged_by_id = {label.id: label for label in label_set.labels}
    assert set(spec_by_id) == set(packaged_by_id)

    for label_id, spec_row in spec_by_id.items():
        packaged = packaged_by_id[label_id]
        assert packaged.number == spec_row.number, label_id
        assert packaged.unit == spec_row.unit, label_id
        assert packaged.origin == spec_row.origin, label_id
        assert packaged.definition == spec_row.definition, label_id


def test_default_label_set_path_is_the_packaged_file():
    assert DEFAULT_LABEL_SET_PATH.name == "label_set_v1.yaml"
    assert DEFAULT_LABEL_SET_PATH.is_file()


def test_label_set_version_is_1():
    label_set = load_label_set()
    assert label_set.version == 1


def test_ratable_labels_are_exactly_the_message_level_labels():
    label_set = load_label_set()
    ratable_ids = {label.id for label in label_set.ratable}
    assert ratable_ids == _MESSAGE_LEVEL_IDS
    assert len(label_set.ratable) == 12


def test_thread_level_labels_are_present_but_not_ratable():
    label_set = load_label_set()
    all_ids = {label.id for label in label_set.labels}
    assert _THREAD_LEVEL_IDS <= all_ids
    ratable_ids = {label.id for label in label_set.ratable}
    assert _THREAD_LEVEL_IDS.isdisjoint(ratable_ids)


def test_by_id_returns_none_for_unknown_id():
    label_set = load_label_set()
    assert label_set.by_id("not_a_real_label") is None
    assert label_set.by_id("hostility") is not None


class TestParseLabelTable:
    def test_missing_heading_raises(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Nothing here\n", encoding="utf-8")
        with pytest.raises(LabelSetError):
            parse_label_table(doc)

    def test_parses_a_minimal_table(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text(
            "### 1.2 Label reference table\n\n"
            "| # | Label | Unit | Origin | Definition |\n"
            "|---|---|---|---|---|\n"
            "| 1 | `foo` | message | LLM-classified | Foo does X. |\n"
            "\n"
            "Next paragraph.\n",
            encoding="utf-8",
        )
        rows = parse_label_table(doc)
        assert len(rows) == 1
        assert rows[0].number == 1
        assert rows[0].id == "foo"
        assert rows[0].unit == "message"
        assert rows[0].origin == "LLM-classified"
        assert rows[0].definition == "Foo does X."


class TestLoadLabelSet:
    def test_invalid_yaml_top_level_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(LabelSetError):
            load_label_set(path)

    def test_missing_version_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("labels: []\n", encoding="utf-8")
        with pytest.raises(LabelSetError):
            load_label_set(path)

    def test_duplicate_id_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(
            "version: 1\n"
            "labels:\n"
            "  - {number: 1, id: dup, unit: message, origin: x, definition: a}\n"
            "  - {number: 2, id: dup, unit: message, origin: x, definition: b}\n",
            encoding="utf-8",
        )
        with pytest.raises(LabelSetError):
            load_label_set(path)
