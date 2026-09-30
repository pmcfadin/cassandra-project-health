"""Tests for the `publish-conversation-aggregates` CLI wiring (issue #118,
DECISIONS.md D26). Mirrors `tests/test_private_run_cli.py`'s pattern:
argument parsing plus the end-to-end success/refusal paths, using only
synthetic aggregates (never the real private aggregates.json).
"""

from __future__ import annotations

import json
from pathlib import Path

from project_health import cli

from tests.test_private_run_publish import _sample_aggregates


def _write_aggregates(path: Path, aggregates: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(aggregates))
    return path


class TestPublishConversationAggregatesParsing:
    def test_subcommand_is_registered(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "publish-conversation-aggregates",
                "--from",
                "agg.json",
                "--data-dir",
                "d",
            ]
        )
        assert args.command == "publish-conversation-aggregates"
        assert args.from_path == "agg.json"
        assert args.data_dir == "d"
        assert args.run_date is None

    def test_run_date_override_is_parsed(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "publish-conversation-aggregates",
                "--from",
                "agg.json",
                "--data-dir",
                "d",
                "--run-date",
                "2026-01-01",
            ]
        )
        assert args.run_date == "2026-01-01"


def _write_threads_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _sample_thread_row(**overrides) -> dict:
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


class TestPublishConversationAggregatesThreadsFlag:
    def test_threads_flag_is_parsed(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "publish-conversation-aggregates",
                "--from",
                "agg.json",
                "--data-dir",
                "d",
                "--threads",
                "threads.jsonl",
            ]
        )
        assert args.threads_path == "threads.jsonl"

    def test_threads_flag_defaults_to_none(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            ["publish-conversation-aggregates", "--from", "agg.json", "--data-dir", "d"]
        )
        assert args.threads_path is None

    def test_publishes_both_snapshot_and_threads(self, tmp_path: Path):
        agg_path = _write_aggregates(tmp_path / "agg.json", _sample_aggregates())
        threads_path = _write_threads_jsonl(
            tmp_path / "threads.jsonl", [_sample_thread_row()]
        )
        data_dir = tmp_path / "data"

        exit_code = cli.main(
            [
                "publish-conversation-aggregates",
                "--from",
                str(agg_path),
                "--data-dir",
                str(data_dir),
                "--threads",
                str(threads_path),
            ]
        )

        assert exit_code == 0
        agg_out = data_dir / "snapshots" / "conversation_patterns" / "2026-09-28.json"
        threads_out = data_dir / "snapshots" / "conversation_patterns" / "threads-2026-09-28.json"
        assert agg_out.is_file()
        assert threads_out.is_file()
        payload = json.loads(threads_out.read_text(encoding="utf-8"))
        assert payload["row_count"] == 1
        assert payload["threads"][0]["thread_key"] == "abc123"

    def test_threads_missing_file_exits_nonzero(self, tmp_path: Path):
        agg_path = _write_aggregates(tmp_path / "agg.json", _sample_aggregates())
        data_dir = tmp_path / "data"

        exit_code = cli.main(
            [
                "publish-conversation-aggregates",
                "--from",
                str(agg_path),
                "--data-dir",
                str(data_dir),
                "--threads",
                str(tmp_path / "does-not-exist.jsonl"),
            ]
        )
        assert exit_code != 0

    def test_threads_sanitize_failure_exits_nonzero(self, tmp_path: Path):
        agg_path = _write_aggregates(tmp_path / "agg.json", _sample_aggregates())
        threads_path = _write_threads_jsonl(
            tmp_path / "threads.jsonl",
            [_sample_thread_row(url="https://markmail.org/message/abc123")],
        )
        data_dir = tmp_path / "data"

        exit_code = cli.main(
            [
                "publish-conversation-aggregates",
                "--from",
                str(agg_path),
                "--data-dir",
                str(data_dir),
                "--threads",
                str(threads_path),
            ]
        )
        assert exit_code != 0
        # The aggregates snapshot (published first) is unaffected by a
        # threads-only failure; only the threads file is refused.
        assert (data_dir / "snapshots" / "conversation_patterns" / "2026-09-28.json").is_file()
        assert not (
            data_dir / "snapshots" / "conversation_patterns" / "threads-2026-09-28.json"
        ).exists()

    def test_threads_run_date_override(self, tmp_path: Path):
        agg_path = _write_aggregates(tmp_path / "agg.json", _sample_aggregates())
        threads_path = _write_threads_jsonl(
            tmp_path / "threads.jsonl", [_sample_thread_row()]
        )
        data_dir = tmp_path / "data"

        exit_code = cli.main(
            [
                "publish-conversation-aggregates",
                "--from",
                str(agg_path),
                "--data-dir",
                str(data_dir),
                "--threads",
                str(threads_path),
                "--run-date",
                "2026-03-15",
            ]
        )
        assert exit_code == 0
        assert (
            data_dir / "snapshots" / "conversation_patterns" / "threads-2026-03-15.json"
        ).is_file()


class TestPublishConversationAggregatesRun:
    def test_writes_snapshot_and_exits_zero(self, tmp_path: Path):
        agg_path = _write_aggregates(tmp_path / "agg.json", _sample_aggregates())
        data_dir = tmp_path / "data"

        exit_code = cli.main(
            [
                "publish-conversation-aggregates",
                "--from",
                str(agg_path),
                "--data-dir",
                str(data_dir),
            ]
        )

        assert exit_code == 0
        out_path = data_dir / "snapshots" / "conversation_patterns" / "2026-09-28.json"
        assert out_path.is_file()

    def test_missing_from_file_exits_nonzero(self, tmp_path: Path):
        exit_code = cli.main(
            [
                "publish-conversation-aggregates",
                "--from",
                str(tmp_path / "does-not-exist.json"),
                "--data-dir",
                str(tmp_path / "data"),
            ]
        )
        assert exit_code != 0

    def test_sanitize_failure_exits_nonzero_and_writes_nothing(self, tmp_path: Path):
        aggregates = _sample_aggregates()
        aggregates["scope_note"] = "contact admin@example.com"
        agg_path = _write_aggregates(tmp_path / "agg.json", aggregates)
        data_dir = tmp_path / "data"

        exit_code = cli.main(
            [
                "publish-conversation-aggregates",
                "--from",
                str(agg_path),
                "--data-dir",
                str(data_dir),
            ]
        )

        assert exit_code != 0
        assert not (data_dir / "snapshots" / "conversation_patterns").exists()

    def test_run_date_override_names_the_output_file(self, tmp_path: Path):
        agg_path = _write_aggregates(tmp_path / "agg.json", _sample_aggregates())
        data_dir = tmp_path / "data"

        exit_code = cli.main(
            [
                "publish-conversation-aggregates",
                "--from",
                str(agg_path),
                "--data-dir",
                str(data_dir),
                "--run-date",
                "2026-03-15",
            ]
        )

        assert exit_code == 0
        assert (
            data_dir / "snapshots" / "conversation_patterns" / "2026-03-15.json"
        ).is_file()
