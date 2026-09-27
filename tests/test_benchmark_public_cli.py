"""Tests for the `benchmark-public` CLI wiring (issue #89).

Mirrors `tests/test_pilot_cli.py`'s pattern: argument parsing and the D18-style
"never inside the public repo" safety-refusal path for `--cache-dir`. The
actual download/classify/report behavior is covered end-to-end (with mocked
transports) by `tests/test_benchmark_public_runner.py`; this file adds one
full pass through `cli._cmd_benchmark_public` itself (not just `run_benchmark`
directly) to prove the CLI plumbing -- registry/mapping loading, report
writing -- is wired correctly.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import httpx2
import pytest
import yaml

from project_health import cli
from project_health.classify.questions import MESSAGE_LEVEL_LABELS


class TestBenchmarkPublicParsing:
    def test_subcommand_is_registered_with_defaults(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            ["benchmark-public", "--cache-dir", "c", "--public-out", "o.md"]
        )
        assert args.command == "benchmark-public"
        assert args.cache_dir == "c"
        assert args.public_out == "o.md"
        assert args.concurrency == 4
        assert args.monthly_cap_usd == 10.0
        assert args.classifier_version == "1.0.0"
        assert args.dotenv is None

    @pytest.mark.parametrize("missing_flag", ["--cache-dir", "--public-out"])
    def test_missing_required_flag_errors(self, missing_flag):
        all_args = ["benchmark-public", "--cache-dir", "c", "--public-out", "o.md"]
        idx = all_args.index(missing_flag)
        filtered = all_args[:idx] + all_args[idx + 2 :]
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(filtered)

    def test_refuses_when_cache_dir_is_inside_the_public_repo(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        cache_dir = repo_root / "cache"
        public_out = tmp_path / "outside" / "out.md"

        monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
        parser = cli._build_parser()
        args = parser.parse_args(
            ["benchmark-public", "--cache-dir", str(cache_dir), "--public-out", str(public_out)]
        )
        exit_code = cli._cmd_benchmark_public(args)
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "--cache-dir" in err


def _build_toxicr_xlsx(path: Path, rows: list[tuple[str, int]]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["message", "is_toxic"])
    for message, is_toxic in rows:
        ws.append([message, is_toxic])
    wb.save(path)
    return path.read_bytes()


def _jev_transport() -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        answers = {label: {"type": "noul", "noul": 0.5} for label in MESSAGE_LEVEL_LABELS}
        answers["tone_intensity"] = {
            "type": "score",
            "score": 1.0,
            "confidence": 0.5,
            "legend": {0: "n", 1: "f", 2: "s", 3: "h", 4: "a"},
            "probabilities": {0: 0.2, 1: 0.2, 2: 0.2, 3: 0.2, 4: 0.2},
        }
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        )

    return httpx2.MockTransport(handler)


def test_cmd_benchmark_public_end_to_end(tmp_path: Path, monkeypatch) -> None:
    xlsx_path = tmp_path / "source.xlsx"
    content = _build_toxicr_xlsx(xlsx_path, [("thanks, looks good", 0), ("you idiot", 1)])
    sha256 = hashlib.sha256(content).hexdigest()

    registry_path = tmp_path / "registry.yaml"
    mapping_path = tmp_path / "mapping.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "datasets": [
                    {
                        "id": "toy",
                        "name": "Toy",
                        "citation": "c",
                        "license": "CC0",
                        "status": "working",
                        "files": [
                            {
                                "url": "https://example.org/t.xlsx",
                                "sha256": sha256,
                                "filename": "t.xlsx",
                            }
                        ],
                        "loader": "load_toxicr",
                        "source_venue": "github_pr_comment",
                        "source_venue_rationale": "r",
                        "target_n": 10,
                        "seed": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    mapping_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mappings": {
                    "toy": [
                        {
                            "our_label": "hostility",
                            "raw_field": "is_toxic",
                            "positive_values": [1],
                            "strength": "partial",
                            "gating": False,
                            "notes": "n",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    cache_dir = tmp_path / "outside" / "cache"
    public_out = tmp_path / "repo" / "docs" / "benchmark" / "public-v1.md"

    monkeypatch.setattr(cli, "find_public_repo_root", lambda: None)  # no repo guard to fight

    import project_health.benchmark_public.runner as runner_module

    original_run_benchmark = runner_module.run_benchmark

    def patched_run_benchmark(**kwargs):
        kwargs["transport"] = httpx.MockTransport(download_handler)
        kwargs["async_transport"] = _jev_transport()
        kwargs["api_key"] = "test-key"
        return original_run_benchmark(**kwargs)

    monkeypatch.setattr(cli, "run_benchmark", patched_run_benchmark)

    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "benchmark-public",
            "--cache-dir",
            str(cache_dir),
            "--public-out",
            str(public_out),
            "--registry",
            str(registry_path),
            "--mapping",
            str(mapping_path),
        ]
    )
    exit_code = cli._cmd_benchmark_public(args)
    assert exit_code == 0
    assert public_out.is_file()
    markdown = public_out.read_text(encoding="utf-8")
    assert "Toy" in markdown
    assert "idiot" not in markdown
