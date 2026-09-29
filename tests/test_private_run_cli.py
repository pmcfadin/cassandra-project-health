"""Tests for the `private-run` CLI wiring (issue #110).

Mirrors `tests/test_pilot_cli.py`'s pattern: argument parsing and the
`--out` safety-refusal path (mirroring D18's "never inside the public
repo" guard) are covered without making a real Jev call or touching the
network. `run_private_run`'s own behavior is covered by `tests/
test_private_run_runner.py`; this file only checks CLI-level wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from project_health import cli


def _write_minimal_project_yaml(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "project": {"id": "example", "display_name": "Example"},
                "reviewer_extraction": {
                    "commit_trailer": {"type": "commit_message_regex", "pattern": ".*"},
                    "jira_fields": {"type": "jira_custom_field"},
                },
                "mailing_lists": {"type": "ponymail", "domain": "example.org", "lists": ["dev"]},
                "issue_tracker": {
                    "type": "jira",
                    "base_url": "https://issues.example.org/jira",
                    "project_key": "EXAMPLE",
                },
            }
        )
    )
    return path


class TestPrivateRunParsing:
    def test_subcommand_is_registered_with_defaults(self):
        parser = cli._build_parser()
        args = parser.parse_args(["private-run", "--project", "p.yaml", "--data-dir", "d"])
        assert args.command == "private-run"
        assert args.project == "p.yaml"
        assert args.data_dir == "d"
        assert args.out == str(Path.home() / "project-health-private")
        assert args.dotenv is None
        assert args.sample_only is False
        assert args.quarters is None
        assert args.concurrency == 4
        assert args.monthly_cap_usd == 25.0
        assert args.seed == 110
        assert args.k == 60
        assert args.classifier_version == "1.0.0"

    def test_accepts_optional_flags(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "private-run",
                "--project",
                "p.yaml",
                "--data-dir",
                "d",
                "--out",
                "o",
                "--dotenv",
                "x.env",
                "--sample-only",
                "--quarters",
                "2024Q1,2025Q1",
                "--concurrency",
                "8",
                "--monthly-cap-usd",
                "5.0",
                "--seed",
                "42",
                "--k",
                "30",
                "--classifier-version",
                "2.0.0",
            ]
        )
        assert args.out == "o"
        assert args.dotenv == "x.env"
        assert args.sample_only is True
        assert args.quarters == "2024Q1,2025Q1"
        assert args.concurrency == 8
        assert args.monthly_cap_usd == 5.0
        assert args.seed == 42
        assert args.k == 30
        assert args.classifier_version == "2.0.0"

    @pytest.mark.parametrize("missing_flag", ["--project", "--data-dir"])
    def test_missing_required_flag_errors(self, missing_flag):
        all_args = ["private-run", "--project", "p.yaml", "--data-dir", "d"]
        idx = all_args.index(missing_flag)
        filtered = all_args[:idx] + all_args[idx + 2 :]
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(filtered)


class TestPrivateRunOutDirGuard:
    def test_refuses_when_out_is_inside_the_public_repo(self, tmp_path: Path, monkeypatch, capsys):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        project_yaml = _write_minimal_project_yaml(tmp_path / "outside" / "project.yaml")
        data_dir = tmp_path / "outside" / "data"
        data_dir.mkdir(parents=True)
        out = repo_root / "out"

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "private-run",
                "--project",
                str(project_yaml),
                "--data-dir",
                str(data_dir),
                "--out",
                str(out),
            ]
        )
        exit_code = cli._cmd_private_run(args)
        assert exit_code == 2
        assert "--out" in capsys.readouterr().err

    def test_out_outside_repo_proceeds_to_config_loading(self, tmp_path: Path, monkeypatch):
        """A safe `--out` should pass the guard and reach project-config
        loading (proven here by a config missing mailing_lists/issue_tracker
        producing the *next* distinct error, not the guard's)."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        project_yaml = tmp_path / "outside" / "bare.yaml"
        project_yaml.parent.mkdir(parents=True)
        project_yaml.write_text(
            yaml.safe_dump(
                {
                    "project": {"id": "bare", "display_name": "Bare"},
                    "reviewer_extraction": {
                        "commit_trailer": {"type": "commit_message_regex", "pattern": ".*"},
                        "jira_fields": {"type": "jira_custom_field"},
                    },
                }
            )
        )
        data_dir = tmp_path / "outside" / "data"
        data_dir.mkdir(parents=True)
        out = tmp_path / "outside" / "out"

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "private-run",
                "--project",
                str(project_yaml),
                "--data-dir",
                str(data_dir),
                "--out",
                str(out),
            ]
        )
        exit_code = cli._cmd_private_run(args)
        assert exit_code == 2  # missing mailing_lists/issue_tracker, not the out-dir guard


class TestPrivateRunSampleOnlyEndToEnd:
    def test_sample_only_runs_through_the_cli_with_no_network(self, tmp_path, monkeypatch):
        import pyarrow as pa

        from project_health import storage

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: None)
        project_yaml = _write_minimal_project_yaml(tmp_path / "project.yaml")
        data_dir = tmp_path / "data"

        table = pa.table(
            {
                "thread_id": pa.array(["t1"], type=pa.string()),
                "list": pa.array(["dev"], type=pa.string()),
                "root_message_id": pa.array(["t1-root"], type=pa.string()),
                "started_at": pa.array(
                    [
                        __import__("datetime").datetime(
                            2024, 1, 1, tzinfo=__import__("datetime").timezone.utc
                        )
                    ],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "last_activity_at": pa.array(
                    [
                        __import__("datetime").datetime(
                            2024, 1, 1, tzinfo=__import__("datetime").timezone.utc
                        )
                    ],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "message_count": pa.array([1], type=pa.int64()),
                "source_snapshot_id": pa.array(["snap"], type=pa.string()),
            }
        )
        storage.write_partition(
            data_dir, "ponymail", "message_thread", "2026-09-25", "run-1", table
        )

        out = tmp_path / "out"
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "private-run",
                "--project",
                str(project_yaml),
                "--data-dir",
                str(data_dir),
                "--out",
                str(out),
                "--sample-only",
                "--quarters",
                "2024Q1",
            ]
        )
        exit_code = cli._cmd_private_run(args)
        assert exit_code == 0
        assert (out / "sample_manifest.json").is_file()
