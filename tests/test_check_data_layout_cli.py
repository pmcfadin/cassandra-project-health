"""Tests for the `project-health check-data-layout` CLI command (issue #176).

Mirrors `tests/test_list_projects_cli.py`'s pattern: calls `cli.main([...])`
directly and inspects `capsys`/the return code, rather than shelling out to
the installed console script. The underlying `data_layout.assert_cassandra_
migrated`/`assert_peers_migrated` logic has its own direct unit tests
(`tests/test_data_layout.py`) -- these only check the CLI wiring: argument
parsing, exit codes, and that the error message reaches stderr.
"""

from __future__ import annotations

from project_health import cli


def test_exits_zero_on_a_fresh_root(tmp_path, capsys):
    exit_code = cli.main(["check-data-layout", "--root", str(tmp_path)])

    assert exit_code == 0
    assert "OK" in capsys.readouterr().out


def test_exits_zero_on_an_already_migrated_root(tmp_path, capsys):
    (tmp_path / "projects" / "cassandra").mkdir(parents=True)
    (tmp_path / "peers").mkdir()

    exit_code = cli.main(["check-data-layout", "--root", str(tmp_path)])

    assert exit_code == 0
    assert "OK" in capsys.readouterr().out


def test_exits_nonzero_with_a_clear_message_on_unmigrated_cassandra_data(tmp_path, capsys):
    (tmp_path / "raw").mkdir()

    exit_code = cli.main(["check-data-layout", "--root", str(tmp_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "pre-migration Cassandra data" in err
    assert "migrate_data_layout.py" in err


def test_exits_nonzero_on_unmigrated_peers_data_even_with_cassandra_migrated(tmp_path, capsys):
    (tmp_path / "projects" / "cassandra").mkdir(parents=True)
    (tmp_path / "raw" / "peers" / "kafka").mkdir(parents=True)

    exit_code = cli.main(["check-data-layout", "--root", str(tmp_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "pre-migration peer-context data" in err
