"""Tests for the `project-health list-projects` CLI command (issue #169).

Mirrors `tests/test_label_cli.py`'s pattern: calls `cli.main([...])`
directly and inspects `capsys`, rather than shelling out to the installed
console script.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from project_health import cli


def _write_index(path: Path, entries: list[dict]) -> Path:
    path.write_text(yaml.safe_dump(entries))
    return path


def _write_minimal_project(path: Path, project_id: str) -> Path:
    path.write_text(
        yaml.safe_dump({"project": {"id": project_id, "display_name": project_id}})
    )
    return path


def test_prints_id_and_config_path_for_enabled_entries_only(tmp_path, capsys):
    _write_minimal_project(tmp_path / "a.yaml", "a")
    _write_minimal_project(tmp_path / "b.yaml", "b")
    index_path = _write_index(
        tmp_path / "index.yaml",
        [
            {"id": "a", "config": "a.yaml", "enabled": True},
            {"id": "b", "config": "b.yaml", "enabled": False},
        ],
    )

    exit_code = cli.main(["list-projects", "--index", str(index_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines == [f"a\t{tmp_path / 'a.yaml'}"]


def test_preserves_index_order(tmp_path, capsys):
    _write_minimal_project(tmp_path / "first.yaml", "first")
    _write_minimal_project(tmp_path / "second.yaml", "second")
    index_path = _write_index(
        tmp_path / "index.yaml",
        [
            {"id": "first", "config": "first.yaml", "enabled": True},
            {"id": "second", "config": "second.yaml", "enabled": True},
        ],
    )

    exit_code = cli.main(["list-projects", "--index", str(index_path)])

    assert exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split("\t")[0] for line in lines] == ["first", "second"]


def test_default_index_path_lists_the_enabled_entries(capsys):
    """Run from the repo root (pytest's default cwd) against the real
    `projects/index.yaml` -- every project in epic #168 is enabled, in
    index order, Cassandra first."""
    exit_code = cli.main(["list-projects"])

    assert exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    ids = [
        "cassandra", "java-driver", "gocql-driver", "python-driver", "cpp-driver",
        "nodejs-driver", "sidecar", "analytics", "accord", "easy-stress", "ccm",
    ]
    assert lines == [f"{i}\t{Path(f'projects/{i}.yaml')}" for i in ids]


def test_missing_index_file_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        cli.main(["list-projects", "--index", str(tmp_path / "does-not-exist.yaml")])
