"""Tests for project_health.peers.git_clone (issue #145)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from project_health.peers import git_clone as git_clone_module
from project_health.peers.git_clone import (
    cleanup,
    clone_shallow_bare,
    du_bytes,
    github_clone_url,
    shallow_since_date,
)


def test_github_clone_url():
    assert github_clone_url("apache", "kafka") == "https://github.com/apache/kafka.git"


def test_shallow_since_date_48_months_back():
    as_of = datetime(2026, 10, 9, tzinfo=timezone.utc)
    result = shallow_since_date(as_of, 48)
    # 48 * 30 = 1440 days before 2026-10-09.
    assert result == "2022-10-30"


def test_shallow_since_date_zero_lookback_is_as_of_date():
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert shallow_since_date(as_of, 0) == "2026-01-01"


def test_du_bytes_sums_file_sizes(tmp_path):
    (tmp_path / "a.txt").write_text("hello")  # 5 bytes
    (tmp_path / "b.txt").write_text("world!")  # 6 bytes
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("xx")  # 2 bytes
    assert du_bytes(tmp_path) == 13


def test_du_bytes_nonexistent_path_is_zero(tmp_path):
    assert du_bytes(tmp_path / "does-not-exist") == 0


def test_cleanup_removes_directory(tmp_path):
    target = tmp_path / "clone"
    target.mkdir()
    (target / "file").write_text("x")
    cleanup(target)
    assert not target.exists()


def test_cleanup_nonexistent_path_does_not_raise(tmp_path):
    cleanup(tmp_path / "does-not-exist")


def test_clone_shallow_bare_retries_transient_failure(tmp_path, monkeypatch):
    """Real-run regression (2026-10-09): apache/datafusion's clone failed
    twice in a row with a transient network timeout, not anything
    repo-specific -- a retry should recover from a failure that clears up
    by the next attempt."""
    calls = {"count": 0}

    def fake_run(args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 2:
            raise subprocess.CalledProcessError(
                128, args, output="", stderr="fatal: Recv failure: Operation timed out"
            )
        return subprocess.CompletedProcess(args, 0)

    sleeps: list[float] = []
    monkeypatch.setattr(git_clone_module.subprocess, "run", fake_run)

    clone_shallow_bare(
        "https://github.com/apache/datafusion.git",
        tmp_path / "clone",
        branch="main",
        shallow_since="2022-01-01",
        max_retries=3,
        sleep_fn=sleeps.append,
    )
    assert calls["count"] == 2
    assert len(sleeps) == 1


def test_clone_shallow_bare_raises_after_exhausting_retries(tmp_path, monkeypatch):
    def always_fails(args, **kwargs):
        raise subprocess.CalledProcessError(128, args, output="", stderr="fatal: still down")

    monkeypatch.setattr(git_clone_module.subprocess, "run", always_fails)

    try:
        clone_shallow_bare(
            "https://github.com/apache/datafusion.git",
            tmp_path / "clone",
            branch="main",
            shallow_since="2022-01-01",
            max_retries=2,
            sleep_fn=lambda _seconds: None,
        )
        raised = False
    except subprocess.CalledProcessError:
        raised = True
    assert raised
