"""Tests for the "never run against the public repo checkout" guard (issue #46).

`find_public_repo_root` itself shells out to `git`, which is exercised
separately (`test_find_public_repo_root_finds_this_checkout` below, marked
so it's skipped if `git` isn't on PATH); every other test injects an
explicit `repo_root` so behavior is deterministic regardless of the
environment `pytest` runs in.
"""

from __future__ import annotations

import shutil

import pytest

from project_health.label.safety import UnsafePathError, assert_outside_repo, find_public_repo_root


class TestAssertOutsideRepo:
    def test_raises_for_a_path_equal_to_repo_root(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        with pytest.raises(UnsafePathError):
            assert_outside_repo(repo_root, repo_root, label="corpus")

    def test_raises_for_a_nested_path(self, tmp_path):
        repo_root = tmp_path / "repo"
        nested = repo_root / "labels" / "rater.jsonl"
        nested.parent.mkdir(parents=True)
        with pytest.raises(UnsafePathError):
            assert_outside_repo(nested, repo_root, label="labels")

    def test_allows_a_sibling_path(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        outside = tmp_path / "private-benchmark" / "corpus.jsonl"
        resolved = assert_outside_repo(outside, repo_root, label="corpus")
        assert resolved == outside.resolve()

    def test_none_repo_root_skips_the_check(self, tmp_path):
        anywhere = tmp_path / "anything.jsonl"
        resolved = assert_outside_repo(anywhere, None, label="corpus")
        assert resolved == anywhere.resolve()

    def test_error_message_names_the_flag_but_not_file_contents(self, tmp_path):
        repo_root = tmp_path / "repo"
        target = repo_root / "corpus.jsonl"
        repo_root.mkdir()
        with pytest.raises(UnsafePathError) as exc_info:
            assert_outside_repo(target, repo_root, label="corpus")
        assert "--corpus" in str(exc_info.value)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_find_public_repo_root_finds_this_checkout():
    from pathlib import Path

    root = find_public_repo_root(Path(__file__).resolve().parent)
    assert root is not None
    assert (root / "pyproject.toml").is_file()


def test_find_public_repo_root_returns_none_outside_any_checkout(tmp_path):
    root = find_public_repo_root(tmp_path)
    assert root is None
