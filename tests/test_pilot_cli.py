"""Tests for the `pilot-classify`/`pilot-evaluate` CLI wiring (issue #47).

Mirrors `tests/test_label_cli.py`'s pattern: argument parsing and the
D18 "never inside the public repo" safety-refusal path are covered without
making a real Jev call or starting anything long-running. `pilot-classify`'s
actual classification behavior is covered by `tests/test_pilot_classify.py`;
this file only checks CLI-level wiring (flags, the repo-root guard, and that
`--public-out` is deliberately exempt from that guard, since it's meant to
land inside the public repo at `docs/pilot/...`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_health import cli
from project_health.classify.questions import MESSAGE_LEVEL_LABELS


def _write_corpus(path: Path) -> Path:
    item = {
        "id": "item-1",
        "stratum": "prevalence",
        "source": "mailing_list",
        "archive_url": "https://example.invalid/thread/1",
        "text": "Agreed, committing this today.",
        "parent_text": None,
        "checksum": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")
    return path


class TestPilotClassifyParsing:
    def test_subcommand_is_registered_with_defaults(self):
        parser = cli._build_parser()
        args = parser.parse_args(["pilot-classify", "--corpus", "c.jsonl", "--out", "o"])
        assert args.command == "pilot-classify"
        assert args.corpus == "c.jsonl"
        assert args.out == "o"
        assert args.concurrency == 4
        assert args.classifier_version == "1.0.0"
        assert args.dotenv is None

    def test_accepts_optional_flags(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "pilot-classify",
                "--corpus",
                "c.jsonl",
                "--out",
                "o",
                "--concurrency",
                "8",
                "--monthly-cap-usd",
                "5.0",
                "--classifier-version",
                "2.0.0",
                "--dotenv",
                "x.env",
            ]
        )
        assert args.concurrency == 8
        assert args.monthly_cap_usd == 5.0
        assert args.classifier_version == "2.0.0"
        assert args.dotenv == "x.env"

    @pytest.mark.parametrize("missing_flag", ["--corpus", "--out"])
    def test_missing_required_flag_errors(self, missing_flag):
        all_args = ["pilot-classify", "--corpus", "c.jsonl", "--out", "o"]
        filtered = [a for a in all_args if a != missing_flag]
        # Drop the value that followed the removed flag too.
        idx = all_args.index(missing_flag)
        filtered = all_args[:idx] + all_args[idx + 2 :]
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(filtered)

    def test_refuses_when_corpus_is_inside_the_public_repo(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        corpus = _write_corpus(repo_root / "corpus.jsonl")
        out = tmp_path / "outside" / "out"

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
        parser = cli._build_parser()
        args = parser.parse_args(["pilot-classify", "--corpus", str(corpus), "--out", str(out)])
        exit_code = cli._cmd_pilot_classify(args)
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "--corpus" in err

    def test_refuses_when_out_is_inside_the_public_repo(self, tmp_path: Path, monkeypatch, capsys):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        corpus = _write_corpus(tmp_path / "outside" / "corpus.jsonl")
        out = repo_root / "out"

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
        parser = cli._build_parser()
        args = parser.parse_args(["pilot-classify", "--corpus", str(corpus), "--out", str(out)])
        exit_code = cli._cmd_pilot_classify(args)
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "--out" in err


class TestPilotEvaluateParsing:
    def test_subcommand_is_registered_and_labels_can_repeat(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "pilot-evaluate",
                "--corpus",
                "c.jsonl",
                "--results",
                "r",
                "--labels",
                "l1.jsonl",
                "--labels",
                "l2.jsonl",
                "--private-out",
                "p.md",
                "--public-out",
                "pub.md",
            ]
        )
        assert args.command == "pilot-evaluate"
        assert args.labels == ["l1.jsonl", "l2.jsonl"]
        assert args.private_out == "p.md"
        assert args.public_out == "pub.md"
        assert args.seed == 47
        assert args.bootstrap_iterations == 1000

    def test_missing_labels_errors(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "pilot-evaluate",
                    "--corpus",
                    "c.jsonl",
                    "--results",
                    "r",
                    "--private-out",
                    "p.md",
                    "--public-out",
                    "pub.md",
                ]
            )

    def test_refuses_when_a_labels_path_is_inside_the_public_repo(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        corpus = _write_corpus(tmp_path / "outside" / "corpus.jsonl")
        results = tmp_path / "outside" / "results"
        results.mkdir(parents=True)
        inside_labels = repo_root / "labels.jsonl"
        inside_labels.write_text("", encoding="utf-8")
        private_out = tmp_path / "outside" / "private.md"

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "pilot-evaluate",
                "--corpus",
                str(corpus),
                "--results",
                str(results),
                "--labels",
                str(inside_labels),
                "--private-out",
                str(private_out),
                "--public-out",
                "docs/pilot/x.md",
            ]
        )
        exit_code = cli._cmd_pilot_evaluate(args)
        assert exit_code == 2
        assert "--labels" in capsys.readouterr().err

    def test_refuses_when_private_out_is_inside_the_public_repo(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        corpus = _write_corpus(tmp_path / "outside" / "corpus.jsonl")
        results = tmp_path / "outside" / "results"
        results.mkdir(parents=True)
        labels = tmp_path / "outside" / "labels.jsonl"
        labels.write_text("", encoding="utf-8")
        private_out = repo_root / "private.md"

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "pilot-evaluate",
                "--corpus",
                str(corpus),
                "--results",
                str(results),
                "--labels",
                str(labels),
                "--private-out",
                str(private_out),
                "--public-out",
                "docs/pilot/x.md",
            ]
        )
        exit_code = cli._cmd_pilot_evaluate(args)
        assert exit_code == 2
        assert "--private-out" in capsys.readouterr().err

    def test_public_out_is_exempt_from_the_repo_guard_and_the_command_succeeds(
        self, tmp_path: Path, monkeypatch
    ):
        """`--public-out` is meant to land inside the public repo
        (`docs/pilot/...`) -- unlike every other pilot-evaluate path, it must
        NOT be refused just because it resolves inside `repo_root`."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        corpus = _write_corpus(tmp_path / "outside" / "corpus.jsonl")

        results = tmp_path / "outside" / "results"
        results.mkdir(parents=True)
        record = {
            "record_id": "11111111-1111-1111-1111-111111111111",
            "message_id": "item-1",
            "thread_id": "item-1",
            "source": "mailing_list",
            "classifier_version": "1.0.0",
            "question_set_version": "1",
            "model_id": "jev-1.13.0",
            "input_hash": "a" * 64,
            "classified_at": "2026-09-25T12:00:00Z",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "labels": {label_id: {"probability": 0.5} for label_id in MESSAGE_LEVEL_LABELS},
            "tone_intensity": None,
            "sentiment_polarity": None,
            "human_reviewed": False,
            "human_label_id": None,
            "superseded_by": None,
        }
        (results / "classifications.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        labels = tmp_path / "outside" / "labels.jsonl"
        label_row = {
            "item_id": "item-1",
            "rater": "r1",
            "labels": {label_id: "no" for label_id in MESSAGE_LEVEL_LABELS},
            "tone": 0,
            "note": None,
            "seconds": 10.0,
            "saved_at": "2026-01-01T00:00:00Z",
            "corpus_checksum": "x",
            "label_set_version": 1,
            "question_set_version": 1,
        }
        labels.write_text(json.dumps(label_row) + "\n", encoding="utf-8")

        private_out = tmp_path / "outside" / "private.md"
        public_out = repo_root / "docs" / "pilot" / "report.md"

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "pilot-evaluate",
                "--corpus",
                str(corpus),
                "--results",
                str(results),
                "--labels",
                str(labels),
                "--private-out",
                str(private_out),
                "--public-out",
                str(public_out),
                "--bootstrap-iterations",
                "10",
            ]
        )
        exit_code = cli._cmd_pilot_evaluate(args)
        assert exit_code == 0
        assert private_out.exists()
        assert public_out.exists()
