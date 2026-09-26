"""Tests for the `project-health label` CLI wiring (issue #46).

Does not start a real server (that would need a live socket, which
`tests/conftest.py`'s autouse network block makes pointless to exercise
here) -- it covers argument parsing and the safety refusal path, which
`_cmd_label` runs *before* ever calling `make_server`.
"""

from __future__ import annotations

import json

import pytest

from project_health import cli


def _write_corpus(path):
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


def test_label_subcommand_is_registered():
    parser = cli._build_parser()
    args = parser.parse_args(
        ["label", "--corpus", "c.jsonl", "--labels", "l.jsonl", "--rater", "pmcfadin"]
    )
    assert args.command == "label"
    assert args.corpus == "c.jsonl"
    assert args.labels == "l.jsonl"
    assert args.rater == "pmcfadin"
    assert args.port == 8765
    assert args.no_browser is False


def test_label_subcommand_accepts_port_and_no_browser():
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "label",
            "--corpus",
            "c.jsonl",
            "--labels",
            "l.jsonl",
            "--rater",
            "pmcfadin",
            "--port",
            "9999",
            "--no-browser",
        ]
    )
    assert args.port == 9999
    assert args.no_browser is True


def test_empty_rater_is_rejected(tmp_path, capsys):
    corpus = _write_corpus(tmp_path / "corpus.jsonl")
    labels = tmp_path / "labels.jsonl"
    parser = cli._build_parser()
    args = parser.parse_args(
        ["label", "--corpus", str(corpus), "--labels", str(labels), "--rater", "   "]
    )
    exit_code = cli._cmd_label(args)
    assert exit_code == 2
    assert "--rater" in capsys.readouterr().err


def test_refuses_when_corpus_is_inside_the_public_repo(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    corpus = _write_corpus(repo_root / "corpus.jsonl")
    labels = tmp_path / "outside" / "labels.jsonl"

    monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
    parser = cli._build_parser()
    args = parser.parse_args(
        ["label", "--corpus", str(corpus), "--labels", str(labels), "--rater", "pmcfadin"]
    )
    exit_code = cli._cmd_label(args)
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--corpus" in err
    assert repo_root.name in err


def test_refuses_when_labels_is_inside_the_public_repo(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    corpus = _write_corpus(tmp_path / "outside" / "corpus.jsonl")
    labels = repo_root / "labels.jsonl"

    monkeypatch.setattr(cli, "find_public_repo_root", lambda: repo_root)
    parser = cli._build_parser()
    args = parser.parse_args(
        ["label", "--corpus", str(corpus), "--labels", str(labels), "--rater", "pmcfadin"]
    )
    exit_code = cli._cmd_label(args)
    assert exit_code == 2
    assert "--labels" in capsys.readouterr().err


def test_corpus_error_is_reported_without_raising(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "outside" / "corpus.jsonl"
    corpus.parent.mkdir(parents=True)
    corpus.write_text("not json\n", encoding="utf-8")
    labels = tmp_path / "outside" / "labels.jsonl"

    monkeypatch.setattr(cli, "find_public_repo_root", lambda: None)
    parser = cli._build_parser()
    args = parser.parse_args(
        ["label", "--corpus", str(corpus), "--labels", str(labels), "--rater", "pmcfadin"]
    )
    exit_code = cli._cmd_label(args)
    assert exit_code == 2
    assert "error" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("missing_flag", ["--corpus", "--labels", "--rater"])
def test_missing_required_flag_errors(missing_flag):
    all_args = ["label", "--corpus", "c.jsonl", "--labels", "l.jsonl", "--rater", "x"]
    filtered = []
    skip_next = False
    for token in all_args:
        if skip_next:
            skip_next = False
            continue
        if token == missing_flag:
            skip_next = True
            continue
        filtered.append(token)
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(filtered)
