"""Tests for corpus loading and append-only label storage (issue #46).

All corpus/message text used here is synthetic (invented for this test
file), never real Cassandra mailing-list/JIRA content -- matching this
project's "quotations: default none" convention (COMMUNITY-HEALTH.md §7.4)
and issue #46's "tests use a synthetic corpus only."
"""

from __future__ import annotations

import json

import pytest

from project_health.label.store import (
    CorpusError,
    append_label_record,
    checksum_file,
    first_unlabeled_index,
    load_corpus,
    read_label_records,
)

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
    {
        "id": "item-3",
        "stratum": "prevalence",
        "source": "mailing_list",
        "archive_url": "https://example.invalid/thread/3",
        "text": "Agreed, going with your approach, committing this today.",
        "parent_text": "I'll drop the rename if we keep the restructuring.",
        "checksum": None,
    },
]


def _write_corpus(path, items=_SYNTHETIC_ITEMS):
    with open(path, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item))
            fh.write("\n")
    return path


class TestLoadCorpus:
    def test_loads_items_in_file_order(self, tmp_path):
        path = _write_corpus(tmp_path / "corpus.jsonl")
        items = load_corpus(path)
        assert [item.id for item in items] == ["item-1", "item-2", "item-3"]
        assert items[0].stratum == "prevalence"
        assert items[1].parent_text == "Here's the flame graph showing the hot path."
        assert items[2].checksum is None

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / "corpus.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_SYNTHETIC_ITEMS[0]))
            fh.write("\n\n")
            fh.write(json.dumps(_SYNTHETIC_ITEMS[1]))
            fh.write("\n")
        items = load_corpus(path)
        assert len(items) == 2

    def test_missing_field_raises_without_leaking_text(self, tmp_path):
        path = tmp_path / "corpus.jsonl"
        bad = dict(_SYNTHETIC_ITEMS[0])
        secret_text = bad.pop("text")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(bad))
            fh.write("\n")
        with pytest.raises(CorpusError) as exc_info:
            load_corpus(path)
        assert secret_text not in str(exc_info.value)
        assert "text" in str(exc_info.value)

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "corpus.jsonl"
        path.write_text("not json\n", encoding="utf-8")
        with pytest.raises(CorpusError):
            load_corpus(path)

    def test_duplicate_id_raises(self, tmp_path):
        path = _write_corpus(tmp_path / "corpus.jsonl", [_SYNTHETIC_ITEMS[0], _SYNTHETIC_ITEMS[0]])
        with pytest.raises(CorpusError):
            load_corpus(path)

    def test_empty_corpus_raises(self, tmp_path):
        path = tmp_path / "corpus.jsonl"
        path.write_text("", encoding="utf-8")
        with pytest.raises(CorpusError):
            load_corpus(path)


class TestChecksumFile:
    def test_same_content_same_checksum(self, tmp_path):
        a = _write_corpus(tmp_path / "a.jsonl")
        b = _write_corpus(tmp_path / "b.jsonl")
        assert checksum_file(a) == checksum_file(b)

    def test_different_content_different_checksum(self, tmp_path):
        a = _write_corpus(tmp_path / "a.jsonl")
        b = _write_corpus(tmp_path / "b.jsonl", _SYNTHETIC_ITEMS[:2])
        assert checksum_file(a) != checksum_file(b)


class TestLabelRecords:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert read_label_records(tmp_path / "does_not_exist.jsonl") == {}

    def test_append_then_read_round_trips(self, tmp_path):
        path = tmp_path / "labels.jsonl"
        record = {
            "item_id": "item-1",
            "rater": "pmcfadin",
            "labels": {"hostility": "no"},
            "tone": 0,
            "note": None,
            "seconds": 12.5,
            "saved_at": "2026-09-25T00:00:00+00:00",
            "corpus_checksum": "deadbeef",
            "label_set_version": 1,
        }
        append_label_record(path, record)
        records = read_label_records(path)
        assert records == {"item-1": record}

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "labels.jsonl"
        append_label_record(path, {"item_id": "item-1", "labels": {}})
        assert path.is_file()

    def test_append_is_append_only_never_truncates(self, tmp_path):
        path = tmp_path / "labels.jsonl"
        append_label_record(path, {"item_id": "item-1", "labels": {}})
        append_label_record(path, {"item_id": "item-2", "labels": {}})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    def test_latest_record_per_item_wins(self, tmp_path):
        path = tmp_path / "labels.jsonl"
        append_label_record(path, {"item_id": "item-1", "labels": {"hostility": "yes"}})
        append_label_record(path, {"item_id": "item-1", "labels": {"hostility": "no"}})
        records = read_label_records(path)
        assert records["item-1"]["labels"] == {"hostility": "no"}


class TestFirstUnlabeledIndex:
    def test_all_unlabeled_returns_zero(self, tmp_path):
        items = load_corpus(_write_corpus(tmp_path / "corpus.jsonl"))
        assert first_unlabeled_index(items, set()) == 0

    def test_resumes_after_first_two_labeled(self, tmp_path):
        items = load_corpus(_write_corpus(tmp_path / "corpus.jsonl"))
        assert first_unlabeled_index(items, {"item-1", "item-2"}) == 2

    def test_returns_none_when_fully_labeled(self, tmp_path):
        items = load_corpus(_write_corpus(tmp_path / "corpus.jsonl"))
        all_ids = {item.id for item in items}
        assert first_unlabeled_index(items, all_ids) is None
