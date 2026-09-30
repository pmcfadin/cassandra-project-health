"""Tests for `project_health.private_run.publish`'s thread-level export
(issue #122, DECISIONS.md D27): `sanitize_threads`'s allowlist + hard-fail
checks, and `write_threads_snapshot`'s
`snapshots/conversation_patterns/threads-<run date>.json` writer.

All fixtures are synthetic.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from project_health.private_run.publish import (
    SanitizeError,
    _thread_hard_fail_scan,
    sanitize_threads,
    write_threads_snapshot,
)


def _row(**overrides) -> dict:
    base = {
        "venue": "mailing_list",
        "thread_key": "abc123",
        "url": "https://lists.apache.org/thread/abc123",
        "subject": "[DISCUSS] Something",
        "started_at": "2024-01-01T00:00:00+00:00",
        "quarter": "2024Q1",
        "n_messages": 3,
        "n_distinct_participants": 2,
        "outcome": {
            "escalation": False,
            "deescalation": False,
            "constructive_resolution": True,
            "abandonment_after_friction": False,
            "pile_on": False,
        },
        "peak_intensity_tier": 1,
        "label_counts": {"technical_disagreement": 2},
    }
    base.update(overrides)
    return base


class TestSanitizeThreads:
    def test_passthrough_of_allowlisted_fields(self):
        rows = [_row()]
        sanitized = sanitize_threads(rows)
        assert sanitized == rows

    def test_jira_url_allowed(self):
        rows = [
            _row(
                venue="jira_comment",
                thread_key="CASSANDRA-123",
                url="https://issues.apache.org/jira/browse/CASSANDRA-123",
                subject="Some bug",
            )
        ]
        sanitized = sanitize_threads(rows)
        assert sanitized[0]["url"] == "https://issues.apache.org/jira/browse/CASSANDRA-123"

    def test_missing_required_key_hard_fails(self):
        row = _row()
        del row["subject"]
        with pytest.raises(SanitizeError, match="missing required key"):
            sanitize_threads([row])

    def test_missing_outcome_key_hard_fails(self):
        row = _row()
        del row["outcome"]["pile_on"]
        with pytest.raises(SanitizeError, match="missing required key"):
            sanitize_threads([row])

    def test_disallowed_archive_mirror_hard_fails(self):
        row = _row(url="https://markmail.org/message/abc123")
        with pytest.raises(SanitizeError, match="not an allowed"):
            sanitize_threads([row])

    def test_mail_archive_mirror_hard_fails(self):
        row = _row(url="https://mail-archive.com/dev@cassandra.apache.org/msg12345.html")
        with pytest.raises(SanitizeError, match="not an allowed"):
            sanitize_threads([row])

    def test_raw_message_id_in_thread_key_hard_fails(self):
        row = _row(thread_key="<abc123@mail.gmail.com>")
        with pytest.raises(SanitizeError, match="Message-ID"):
            sanitize_threads([row])

    def test_email_address_in_non_subject_field_hard_fails(self):
        row = _row(thread_key="alice@example.org")
        with pytest.raises(SanitizeError, match="email address"):
            sanitize_threads([row])

    def test_email_address_in_subject_is_redacted_not_hard_failed(self):
        # Real, already-public JIRA/dev@ subjects legitimately mention an
        # email address (verified against a real private-run output: "Add
        # e.dimitrova@gmail.com to KEYS") -- rather than exempting
        # `subject` from the email check, the address is redacted to the
        # literal marker and publishing proceeds.
        row = _row(subject="Add e.dimitrova@gmail.com to KEYS")
        sanitized = sanitize_threads([row])
        assert sanitized[0]["subject"] == "Add [email] to KEYS"
        assert "e.dimitrova@gmail.com" not in sanitized[0]["subject"]

    def test_multiple_emails_in_subject_all_redacted(self):
        row = _row(subject="cc alice@example.org and bob@example.org please")
        sanitized = sanitize_threads([row])
        assert sanitized[0]["subject"] == "cc [email] and [email] please"

    def test_hard_fail_still_fires_on_an_unredacted_email_in_subject(self):
        # Defense in depth: `sanitize_threads`'s own `_copy_thread_row`
        # always redacts `subject` before this scan ever runs, so this
        # calls the scan directly on a row whose subject was never
        # redacted, proving the email check still applies to `subject`
        # (only the long-hex/id check is exempted there) rather than the
        # field being skipped by the scan wholesale.
        row = _row(subject="Add e.dimitrova@gmail.com to KEYS")
        with pytest.raises(SanitizeError, match="email address"):
            _thread_hard_fail_scan([row])

    def test_long_digit_string_in_subject_does_not_hard_fail(self):
        # A real JIRA NumberFormatException stack-trace subject can contain
        # a 32+ character digit string, which coincidentally matches the
        # generic "looks like a hash" pattern without being one.
        digits = "140804036566258204771707954633792970268"
        row = _row(subject=f'java.lang.NumberFormatException: "{digits}"')
        sanitized = sanitize_threads([row])
        assert digits in sanitized[0]["subject"]

    def test_long_hex_hash_hard_fails(self):
        row = _row(thread_key="a" * 32)
        with pytest.raises(SanitizeError, match="hash"):
            sanitize_threads([row])

    def test_extra_unknown_key_is_dropped_not_passed_through(self):
        row = _row()
        row["author_key"] = "should-never-appear"
        sanitized = sanitize_threads([row])
        assert "author_key" not in sanitized[0]

    def test_empty_url_hard_fails(self):
        # Fail closed: `runner.py` already skips a dev@ thread whose root
        # message has no resolvable permalink rather than emitting an
        # empty-url row, but the sanitizer doesn't rely on that upstream
        # discipline -- an empty url is refused here too, not silently
        # published.
        row = _row(url="")
        with pytest.raises(SanitizeError, match="not an allowed"):
            sanitize_threads([row])


class TestWriteThreadsSnapshot:
    def test_writes_sanitized_rows_to_dated_file(self, tmp_path):
        rows = [_row(), _row(thread_key="def456", url="https://lists.apache.org/thread/def456")]
        out_path = write_threads_snapshot(rows, tmp_path, run_date=date(2026, 9, 29))
        expected = tmp_path / "snapshots" / "conversation_patterns" / "threads-2026-09-29.json"
        assert out_path == expected
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["row_count"] == 2
        assert len(payload["threads"]) == 2
        assert payload["run_date"] == "2026-09-29"

    def test_writes_nothing_on_sanitize_error(self, tmp_path):
        row = _row(url="https://markmail.org/message/abc123")
        with pytest.raises(SanitizeError):
            write_threads_snapshot([row], tmp_path, run_date=date(2026, 9, 29))
        assert not (tmp_path / "snapshots").exists()
