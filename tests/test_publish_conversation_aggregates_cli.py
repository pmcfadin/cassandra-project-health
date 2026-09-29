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
