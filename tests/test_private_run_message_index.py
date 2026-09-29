"""Tests for project_health.private_run.message_index (issue #114, build
item 1: the per-message private index)."""

from __future__ import annotations

import json

from project_health.private_run.identity import hash_author
from project_health.private_run.message_index import build_entry, write_message_index


class TestBuildEntry:
    def test_hashes_the_author_never_stores_it_raw(self):
        entry = build_entry(
            call_id="mail:<m1@example.org>",
            venue="mailing_list",
            quarter="2024Q1",
            thread_key="t1",
            position=0,
            posted_at="2024-01-01T00:00:00+00:00",
            parent_call_id=None,
            author_raw="alice@example.org",
            salt="salt1",
            input_hash="deadbeef",
        )
        assert entry["author_key"] == hash_author("alice@example.org", "salt1")
        # `call_id` legitimately carries the raw dev@ Message-ID (ids are
        # fine in this private, --out-only index -- see module docstring);
        # what must never appear anywhere is the raw *author* string.
        assert entry["author_key"] != "alice@example.org"
        non_call_id_fields = {k: v for k, v in entry.items() if k != "call_id"}
        assert "alice@example.org" not in json.dumps(non_call_id_fields)
        assert entry["thread_key"] == "t1"
        assert entry["input_hash"] == "deadbeef"


class TestWriteMessageIndex:
    def test_writes_one_json_line_per_entry_sorted_by_call_id(self, tmp_path):
        entries = [
            build_entry(
                call_id="mail:b",
                venue="mailing_list",
                quarter="2024Q1",
                thread_key="t1",
                position=1,
                posted_at="2024-01-01T00:01:00+00:00",
                parent_call_id="mail:a",
                author_raw="bob@example.org",
                salt="salt1",
                input_hash="hash-b",
            ),
            build_entry(
                call_id="mail:a",
                venue="mailing_list",
                quarter="2024Q1",
                thread_key="t1",
                position=0,
                posted_at="2024-01-01T00:00:00+00:00",
                parent_call_id=None,
                author_raw="alice@example.org",
                salt="salt1",
                input_hash="hash-a",
            ),
        ]
        path = write_message_index(tmp_path, entries)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        parsed = [json.loads(line) for line in lines]
        assert [p["call_id"] for p in parsed] == ["mail:a", "mail:b"]

    def test_never_contains_raw_message_text_or_author(self, tmp_path):
        entries = [
            build_entry(
                call_id="mail:a",
                venue="mailing_list",
                quarter="2024Q1",
                thread_key="t1",
                position=0,
                posted_at="2024-01-01T00:00:00+00:00",
                parent_call_id=None,
                author_raw="alice@example.org",
                salt="salt1",
                input_hash="hash-a",
            )
        ]
        path = write_message_index(tmp_path, entries)
        raw = path.read_text(encoding="utf-8")
        assert "alice" not in raw
        assert "example.org" not in raw

    def test_rewrite_replaces_rather_than_appends(self, tmp_path):
        first_entries = [
            build_entry(
                call_id="mail:a",
                venue="mailing_list",
                quarter="2024Q1",
                thread_key="t1",
                position=0,
                posted_at="2024-01-01T00:00:00+00:00",
                parent_call_id=None,
                author_raw="alice@example.org",
                salt="salt1",
                input_hash="hash-a",
            )
        ]
        write_message_index(tmp_path, first_entries)

        second_entries = [
            build_entry(
                call_id="mail:z",
                venue="mailing_list",
                quarter="2024Q2",
                thread_key="t2",
                position=0,
                posted_at="2024-04-01T00:00:00+00:00",
                parent_call_id=None,
                author_raw="carol@example.org",
                salt="salt1",
                input_hash="hash-z",
            )
        ]
        path = write_message_index(tmp_path, second_entries)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["call_id"] == "mail:z"
