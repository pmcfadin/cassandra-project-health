"""Tests for project_health.private_run.identity (issue #114: the per-`--out`
author salt used only to hash author identity for `message_index.jsonl`)."""

from __future__ import annotations

from project_health.private_run.identity import (
    DEFAULT_SALT_FILENAME,
    hash_author,
    load_or_create_salt,
)


class TestLoadOrCreateSalt:
    def test_creates_a_salt_file_on_first_use(self, tmp_path):
        assert not (tmp_path / DEFAULT_SALT_FILENAME).is_file()
        salt = load_or_create_salt(tmp_path)
        assert salt
        assert (tmp_path / DEFAULT_SALT_FILENAME).is_file()

    def test_reuses_the_same_salt_across_calls(self, tmp_path):
        first = load_or_create_salt(tmp_path)
        second = load_or_create_salt(tmp_path)
        assert first == second

    def test_different_out_dirs_get_different_salts(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        assert load_or_create_salt(dir_a) != load_or_create_salt(dir_b)


class TestHashAuthor:
    def test_deterministic_for_the_same_input(self):
        assert hash_author("alice@example.org", "salt1") == hash_author(
            "alice@example.org", "salt1"
        )

    def test_different_salts_produce_different_hashes(self):
        assert hash_author("alice@example.org", "salt1") != hash_author(
            "alice@example.org", "salt2"
        )

    def test_case_and_whitespace_normalized(self):
        assert hash_author("Alice@Example.org", "salt1") == hash_author(
            " alice@example.org ", "salt1"
        )

    def test_never_contains_the_raw_author_string(self):
        digest = hash_author("alice@example.org", "some-salt")
        assert "alice" not in digest
        assert "example.org" not in digest

    def test_none_and_empty_author_hash_the_same(self):
        assert hash_author(None, "salt1") == hash_author("", "salt1")
