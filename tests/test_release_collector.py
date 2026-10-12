"""Tests for project_health.collectors.release (issue #135).

`list_ga_tags`/the tag-regex tests run against a small, deterministic real
git repository built fresh into `tmp_path` (never a network clone). The
archive cross-check tests use an injected `httpx.MockTransport` with a
retry-free `sleep_fn`, same offline pattern as `test_security_collector.py`.
"""

from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

import httpx
import pytest

from project_health.collectors.release import (
    CollectionError,
    ReleaseCollector,
    _ga_version,
    _parse_archive_listing,
    list_ga_tags,
)
from project_health.config import load_project


@pytest.fixture
def config():
    return load_project("projects/cassandra.yaml")


def _git_env() -> dict[str, str]:
    """Environment with a fixed git identity, so committing and creating
    annotated tags works on machines with no user.name/user.email configured
    (e.g. CI runners)."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    env = _git_env()
    subprocess.run(
        ["git", "init", "-b", "trunk"], cwd=repo_path, env=env, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo_path, check=True, capture_output=True
    )
    (repo_path / "README.md").write_text("cassandra\n")
    subprocess.run(
        ["git", "-C", str(repo_path), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", "Initial commit"],
        env=env,
        check=True,
        capture_output=True,
    )


def _tag(repo_path: Path, name: str, tagger_date: str, annotated: bool = True) -> None:
    """Create a tag at HEAD, dated `tagger_date` (e.g. "2024-09-05T20:19:17").

    `annotated=True` (the default, matching the real apache/cassandra
    repo's own GA tags, verified live) sets `GIT_COMMITTER_DATE` so the
    tag's own `creatordate` is deterministic; a lightweight tag instead
    falls back to the pointed-at commit's own (fixed, `_init_repo`-time)
    author date.
    """
    env = _git_env()
    env["GIT_COMMITTER_DATE"] = tagger_date
    env["GIT_AUTHOR_DATE"] = tagger_date
    args = ["git", "-C", str(repo_path), "tag"]
    if annotated:
        args += ["-a", name, "-m", f"tag {name}"]
    else:
        args += [name]
    subprocess.run(args, env=env, check=True, capture_output=True)


@pytest.fixture
def tagged_repo(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    # Real GA releases (two different release lines + one early "-final").
    _tag(repo, "cassandra-5.0.0", "2024-09-05T20:19:17")
    _tag(repo, "cassandra-5.0.1", "2024-10-01T08:43:00")
    _tag(repo, "cassandra-3.1", "2016-01-15T10:00:00")
    _tag(repo, "cassandra-0.3.0-final", "2010-03-13T00:00:00")
    # Pre-releases -- must be excluded.
    _tag(repo, "cassandra-5.0-rc1", "2024-08-26T14:12:00")
    _tag(repo, "cassandra-6.0-alpha1", "2026-01-01T00:00:00")
    _tag(repo, "cassandra-4.0-beta2", "2021-01-01T00:00:00")
    # Known packaging-only duplicate shapes -- must be excluded (verified
    # live against the real repo, collectors/release.py's module docstring).
    _tag(repo, "cassandra-0.7.6-2", "2011-05-20T08:21:47")
    _tag(repo, "cassandra-2.1.0-deb", "2014-09-18T11:45:20")
    return repo


@pytest.fixture
def major_only_tagged_repo(tmp_path):
    """A repo whose GA tags are bare major versions only (issue #170,
    verified live for apache/cassandra-easy-stress -- `v9`/`v8`/`v7`), plus
    one pre-release tag that must still be excluded."""
    repo = tmp_path / "easy-stress-repo"
    _init_repo(repo)
    _tag(repo, "v7", "2023-01-10T00:00:00")
    _tag(repo, "v8", "2024-06-01T00:00:00")
    _tag(repo, "v9", "2025-03-20T00:00:00")
    _tag(repo, "v10-rc1", "2026-01-01T00:00:00")
    return repo


class TestGaVersionRegex:
    @pytest.mark.parametrize(
        ("tag_name", "expected"),
        [
            ("cassandra-5.0.2", "5.0.2"),
            ("cassandra-3.1", "3.1"),
            ("cassandra-0.3.0-final", "0.3.0"),
            ("cassandra-5.0-rc1", None),
            ("cassandra-5.0-rc2", None),
            ("cassandra-4.1-alpha1", None),
            ("cassandra-6.0-alpha2", None),
            ("cassandra-4.0-beta2", None),
            ("cassandra-0.7.6-2", None),
            ("cassandra-2.1.0-deb", None),
            ("other-repo-5.0.2", None),
        ],
    )
    def test_ga_version(self, tag_name, expected):
        assert _ga_version(tag_name, "cassandra-") == expected

    @pytest.mark.parametrize(
        ("tag_name", "expected"),
        [
            # issue #170: apache/cassandra-easy-stress's own GA tags are
            # bare major versions (verified live, `config.ReleasesConfig.
            # ga_major_only`'s docstring) -- no minor/patch component.
            ("v9", "9"),
            ("v8", "8"),
            ("v10-rc1", None),  # pre-release suffix still excluded
            ("v9.1", None),  # a dotted version is NOT a bare major
            ("other-9", None),  # wrong prefix
        ],
    )
    def test_ga_version_major_only(self, tag_name, expected):
        assert _ga_version(tag_name, "v", ga_major_only=True) == expected


class TestListGaTags:
    def test_excludes_prereleases_and_packaging_duplicates(self, tagged_repo):
        tags = list_ga_tags(tagged_repo, "cassandra-")
        names = {t.tag_name for t in tags}
        assert names == {
            "cassandra-5.0.0",
            "cassandra-5.0.1",
            "cassandra-3.1",
            "cassandra-0.3.0-final",
        }

    def test_strips_final_suffix_from_version(self, tagged_repo):
        tags = {t.tag_name: t for t in list_ga_tags(tagged_repo, "cassandra-")}
        assert tags["cassandra-0.3.0-final"].version == "0.3.0"

    def test_derives_major_minor(self, tagged_repo):
        tags = {t.tag_name: t for t in list_ga_tags(tagged_repo, "cassandra-")}
        assert tags["cassandra-5.0.0"].major_minor == "5.0"
        assert tags["cassandra-3.1"].major_minor == "3.1"

    def test_release_date_is_tag_creatordate(self, tagged_repo):
        tags = {t.tag_name: t for t in list_ga_tags(tagged_repo, "cassandra-")}
        assert tags["cassandra-5.0.0"].release_date == date(2024, 9, 5)
        assert tags["cassandra-5.0.1"].release_date == date(2024, 10, 1)

    def test_sorted_by_release_date(self, tagged_repo):
        tags = list_ga_tags(tagged_repo, "cassandra-")
        dates = [t.release_date for t in tags]
        assert dates == sorted(dates)


class TestListGaTagsMajorOnly:
    """issue #170: `ga_major_only=True` threads through `list_ga_tags` to
    `_ga_version`, matching apache/cassandra-easy-stress's real tag shape."""

    def test_matches_bare_major_tags_and_excludes_prerelease(self, major_only_tagged_repo):
        tags = list_ga_tags(major_only_tagged_repo, "v", ga_major_only=True)
        names = {t.tag_name for t in tags}
        assert names == {"v7", "v8", "v9"}

    def test_version_and_major_minor_are_the_bare_integer(self, major_only_tagged_repo):
        tags = {
            t.tag_name: t
            for t in list_ga_tags(major_only_tagged_repo, "v", ga_major_only=True)
        }
        assert tags["v9"].version == "9"
        assert tags["v9"].major_minor == "9"

    def test_without_the_flag_no_tags_match_at_all(self, major_only_tagged_repo):
        # The ordinary `major.minor[.patch]` pattern never matches a bare
        # integer suffix -- confirms `ga_major_only` isn't a no-op.
        assert list_ga_tags(major_only_tagged_repo, "v", ga_major_only=False) == []


class TestArchiveListingParser:
    def test_parses_version_directories_only(self):
        html = """
        <a href="5.0.0/">5.0.0/</a>              2024-09-05 18:25    -
        <a href="5.0-rc1/">5.0-rc1/</a>           2024-07-18 20:22    -
        <a href="5.0.1/">5.0.1/</a>              2024-10-01 08:43    -
        """
        versions = _parse_archive_listing(html)
        assert versions == {
            "5.0.0": date(2024, 9, 5),
            "5.0.1": date(2024, 10, 1),
        }


def _archive_transport(html: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=html)

    return httpx.MockTransport(handler)


def _offline_collector(config, transport: httpx.MockTransport) -> ReleaseCollector:
    return ReleaseCollector(config, transport=transport, max_retries=1, sleep_fn=lambda s: None)


ARCHIVE_HTML = """
<a href="5.0.0/">5.0.0/</a>              2024-09-05 18:25    -
<a href="5.0.1/">5.0.1/</a>              2024-10-01 08:43    -
<a href="3.1/">3.1/</a>                  2016-01-15 10:00    -
"""


class TestCollect:
    def test_collects_one_row_per_ga_tag(self, config, tagged_repo):
        collector = _offline_collector(config, _archive_transport(ARCHIVE_HTML))
        result = collector.collect(
            repo_path=tagged_repo,
            repo_label="apache/cassandra",
            tag_prefix="cassandra-",
            snapshot_id="snap-1",
        )
        assert result.release_count == 4
        assert result.release.num_rows == 4

    def test_archive_verified_true_when_directory_exists(self, config, tagged_repo):
        collector = _offline_collector(config, _archive_transport(ARCHIVE_HTML))
        result = collector.collect(
            repo_path=tagged_repo,
            repo_label="apache/cassandra",
            tag_prefix="cassandra-",
            snapshot_id="snap-1",
        )
        rows = {row["release_id"]: row for row in result.release.to_pylist()}
        assert rows["cassandra-5.0.0"]["archive_verified"] is True
        assert rows["cassandra-5.0.0"]["archive_date"] == date(2024, 9, 5)

    def test_archive_verified_false_when_directory_missing(self, config, tagged_repo):
        # 0.3.0-final's archive directory is plainly named "0.3.0" (no
        # "-final" suffix), and isn't in this fixture's ARCHIVE_HTML.
        collector = _offline_collector(config, _archive_transport(ARCHIVE_HTML))
        result = collector.collect(
            repo_path=tagged_repo,
            repo_label="apache/cassandra",
            tag_prefix="cassandra-",
            snapshot_id="snap-1",
        )
        rows = {row["release_id"]: row for row in result.release.to_pylist()}
        assert rows["cassandra-0.3.0-final"]["archive_verified"] is False
        assert rows["cassandra-0.3.0-final"]["archive_date"] is None

    def test_release_date_source_is_always_git_tag(self, config, tagged_repo):
        collector = _offline_collector(config, _archive_transport(ARCHIVE_HTML))
        result = collector.collect(
            repo_path=tagged_repo,
            repo_label="apache/cassandra",
            tag_prefix="cassandra-",
            snapshot_id="snap-1",
        )
        sources = {row["release_date_source"] for row in result.release.to_pylist()}
        assert sources == {"git_tag"}

    def test_archive_outage_never_fails_collection(self, config, tagged_repo):
        """A failed archive cross-check must never block the primary,
        sufficient git-tag signal (module docstring)."""
        collector = _offline_collector(config, _archive_transport("", status=500))
        result = collector.collect(
            repo_path=tagged_repo,
            repo_label="apache/cassandra",
            tag_prefix="cassandra-",
            snapshot_id="snap-1",
        )
        assert result.release_count == 4
        assert result.archive_checked is False
        for row in result.release.to_pylist():
            assert row["archive_verified"] is None
            assert row["archive_date"] is None

    def test_every_row_shares_repo_and_snapshot(self, config, tagged_repo):
        collector = _offline_collector(config, _archive_transport(ARCHIVE_HTML))
        result = collector.collect(
            repo_path=tagged_repo,
            repo_label="apache/cassandra",
            tag_prefix="cassandra-",
            snapshot_id="snap-1",
        )
        for row in result.release.to_pylist():
            assert row["repo"] == "apache/cassandra"
            assert row["source_snapshot_id"] == "snap-1"


class TestConfigWiring:
    def test_archive_url_comes_from_project_config(self, config):
        collector = _offline_collector(config, _archive_transport(ARCHIVE_HTML))
        assert collector._archive_url == "https://archive.apache.org/dist/cassandra/"

    def test_archive_url_none_resolves_to_none_not_cassandra_default(self):
        """issue #170: a project with `releases.archive_url: null` (e.g.
        projects/easy-stress.yaml, verified live to have no
        archive.apache.org/dist directory of its own) must never fall back
        to `DEFAULT_ARCHIVE_URL` -- Cassandra's own archive listing."""
        easy_stress_config = load_project("projects/easy-stress.yaml")
        collector = _offline_collector(easy_stress_config, _archive_transport(ARCHIVE_HTML))
        assert collector._archive_url is None


class TestArchiveUrlNoneSkipsCrossCheck:
    """issue #170: `archive_url: null` must SKIP the archive cross-check
    entirely -- never attempt a request at all, let alone fall back to
    Cassandra's own archive listing."""

    def test_fetch_archive_versions_returns_none_without_a_network_call(self):
        easy_stress_config = load_project("projects/easy-stress.yaml")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made when archive_url is None")

        collector = _offline_collector(easy_stress_config, httpx.MockTransport(handler))
        assert collector.fetch_archive_versions() is None

    def test_collect_reports_archive_checked_false_and_every_row_unverified(
        self, major_only_tagged_repo
    ):
        easy_stress_config = load_project("projects/easy-stress.yaml")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made when archive_url is None")

        collector = _offline_collector(easy_stress_config, httpx.MockTransport(handler))
        result = collector.collect(
            repo_path=major_only_tagged_repo,
            repo_label="apache/cassandra-easy-stress",
            tag_prefix="v",
            snapshot_id="snap-1",
            ga_major_only=True,
        )
        assert result.release_count == 3  # v7, v8, v9 -- v10-rc1 excluded
        assert result.archive_checked is False
        for row in result.release.to_pylist():
            assert row["archive_verified"] is None
            assert row["archive_date"] is None


class TestRetryAndErrors:
    def test_5xx_exhausted_retries_means_fetch_archive_versions_returns_none(self, config):
        collector = _offline_collector(config, _archive_transport("", status=500))
        assert collector.fetch_archive_versions() is None

    def test_get_with_retry_raises_collection_error_directly(self, config):
        collector = _offline_collector(config, _archive_transport("", status=500))
        with pytest.raises(CollectionError):
            collector._get_with_retry(collector._archive_url)
