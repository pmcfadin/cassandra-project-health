"""GA release collector for the release cadence dimension (issue #135).

Closes the gap `scoring.yaml`/`docs/spec/SCORING.md` already anticipated:
the release cadence dimension's only key metric, `release_frequency`
(target-range), plus its two supporting metrics, had no collector at all,
so that dimension always showed `insufficient_data` (`scoring/registry.py`'s
own docstring already flagged this: "release_frequency ... has no collector
yet").

## Primary source: git tags (reused clone, zero extra HTTP calls)

`collectors/git.py`'s `clone_or_fetch`/`GitCollector` already maintain a
full (blobless, no-checkout) local clone of `apache/cassandra` at the
pipeline's `workdir` (`pipeline._collect_git`) -- this module reuses that
exact clone (`clone_or_fetch` is idempotent; calling it again here is a
cheap no-op `git fetch` when `git` already ran this run, and makes this
source self-contained when `git` is *not* in the active source list).

GA releases are identified from annotated tags matching
``<tag_prefix><major>.<minor>[.<patch>][-final]`` (e.g.
``cassandra-5.0.2``, ``cassandra-3.1``, the early ``cassandra-0.3.0-final``),
excluding:

- pre-release suffixes (``-alpha``/``-beta``/``-rc``, any trailing digit) --
  METRICS.md §6's "RCs, alphas, and betas excluded by default."
- two verified packaging-only duplicate tags that do not match the pattern
  at all (``cassandra-0.7.6-2``, ``cassandra-2.1.0-deb`` -- re-tags of the
  same release for Debian packaging purposes, confirmed live 2026-10-09:
  both ``cassandra-0.7.6``/``cassandra-2.1.0`` already exist as their own,
  separately-tagged GA releases a few days earlier).

`release_date` is always the tag's own `creatordate` (git's term for an
annotated tag's tagger date, or the pointed-at commit's author date for a
lightweight tag) -- this is the PRIMARY, authoritative date for every
release-cadence metric. Verified live against the real apache/cassandra
repository, 2026-10-09: 259 GA tags; per-year GA counts 2023=10, 2024=13,
2025=15.

## Cross-check source: archive.apache.org (existence only, never dating)

`docs/spec/DATA-SOURCES.md` §3/§5: apache/cassandra publishes zero GitHub
Releases (git tags are GitHub's only release signal), and
``archive.apache.org/dist/cassandra/`` is the permanent, never-pruned
historical archive -- the correct cross-check for "was this GA release
actually published," as distinct from "when."

That "as distinct from when" is deliberate and verified, not a
simplification: fetching the live directory listing (2026-10-09) and
bucketing each GA version by the archive's own per-directory mtime instead
of the git tag date changes the 2025 count from 15 to 13 -- not because two
releases are missing, but because ``3.0.32`` and ``3.11.19`` (both real,
git-tagged 2025-02-07 releases) carry a directory mtime of
``2026-05-01 17:48``, identical to several *other* unrelated directories
(``5.0.7``, ``5.0.8``, ``6.0-alpha1``) that an evident bulk archive-side
maintenance operation touched on that date, long after those versions'
actual release. The directory's mere *existence* is reliable and used here
(`archive_verified`); its listed mtime (`archive_date`) is recorded for
transparency only and is NEVER the `release_date` or counted into any
metric. (Separately, four tagged GA versions -- `2.1.10`, `2.1.11`, `2.2.2`,
`2.2.3`, all 2015-2016 -- have no archive directory at all today, i.e.
`archive_verified=False`; consistent with this project's own history of
occasionally pulling a bad release shortly after shipping it, these stay
counted as GA releases since they were tagged and released at the time --
raw historical facts are never retroactively erased, D3 -- with the
cross-check disclosing the gap rather than silently dropping the row.)

A failure fetching `archive.apache.org` (network outage, non-200) is
never fatal to this source: every row's `archive_verified`/`archive_date`
is simply `None` for that run, logged, and the git-tag-derived rows -- the
primary, sufficient signal -- are still collected and returned.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.schema import get_schema, validate

DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0

DEFAULT_ARCHIVE_URL = "https://archive.apache.org/dist/cassandra/"

# A GA tag's suffix (after `<tag_prefix>`, e.g. after "cassandra-") must be a
# bare `major.minor[.patch]`, optionally followed by `-final` (the only two
# of the earliest releases, 0.3.0/0.4.0, that shipped under a `-final` tag
# alongside their own `-rc*`/`-beta*` tags -- never present on any tag from
# 0.4.1 onward).
_GA_SUFFIX_RE = re.compile(r"^(\d+\.\d+(?:\.\d+)?)(-final)?$")
# Pre-release suffix anywhere at the end of a tag's suffix -- checked before
# `_GA_SUFFIX_RE` so e.g. "5.0-rc1" is excluded outright rather than simply
# failing to match (it wouldn't match `_GA_SUFFIX_RE` either way, but this
# keeps the exclusion reason explicit and independent of that pattern).
_PRERELEASE_SUFFIX_RE = re.compile(r"(alpha|beta|rc)\d*$", re.IGNORECASE)

# Apache directory-listing autoindex row: `<a href="NAME/">NAME/</a>  YYYY-MM-DD HH:MM  ...`
_ARCHIVE_ROW_RE = re.compile(r'<a href="([^"/]+)/">[^<]*</a>\s+(\d{4}-\d{2}-\d{2})')


class CollectionError(Exception):
    """Raised when the archive cross-check request fails after retries."""


def _run_git(repo_path: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _ga_version(tag_name: str, tag_prefix: str) -> str | None:
    """The normalized version string (e.g. "5.0.2") if `tag_name` is a GA
    release tag under `tag_prefix`, else `None` -- see module docstring for
    the exact inclusion/exclusion rules."""
    if not tag_name.startswith(tag_prefix):
        return None
    suffix = tag_name[len(tag_prefix) :]
    if _PRERELEASE_SUFFIX_RE.search(suffix):
        return None
    match = _GA_SUFFIX_RE.match(suffix)
    if not match:
        return None
    return match.group(1)


def _major_minor(version: str) -> str:
    parts = version.split(".")
    return ".".join(parts[:2])


@dataclass(frozen=True)
class GaTag:
    """One GA release, as derived from a single git tag."""

    tag_name: str
    version: str
    major_minor: str
    release_date: date


def list_ga_tags(repo_path: str | Path, tag_prefix: str) -> list[GaTag]:
    """Every GA release tag in `repo_path`'s local clone, dated by each
    tag's own `creatordate` (git's tagger date for an annotated tag, or the
    pointed-at commit's author date for a lightweight one) -- the primary,
    authoritative release date (module docstring).

    `repo_path` must already be a cloned working copy (bare or not) --
    callers (`pipeline._collect_release`) run `clone_or_fetch` first, the
    same way `pipeline._collect_git` does for the ordinary commit walk.
    """
    repo_path = Path(repo_path)
    output = _run_git(
        repo_path,
        ["for-each-ref", "--format=%(refname:short)\x1f%(creatordate:iso-strict)", "refs/tags/"],
    )
    tags: list[GaTag] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        tag_name, _, date_str = line.partition("\x1f")
        version = _ga_version(tag_name, tag_prefix)
        if version is None:
            continue
        # The first 10 characters of an ISO-8601 (or ISO-strict, with a
        # trailing UTC-offset) timestamp are always `YYYY-MM-DD` -- the
        # calendar date is taken as-is, in whatever timezone the tag (or its
        # underlying commit) itself recorded, matching the archive
        # directory-listing dates' own convention of a bare calendar date
        # with no timezone normalization.
        release_date = date.fromisoformat(date_str[:10])
        tags.append(
            GaTag(
                tag_name=tag_name,
                version=version,
                major_minor=_major_minor(version),
                release_date=release_date,
            )
        )
    tags.sort(key=lambda t: (t.release_date, t.tag_name))
    return tags


def _parse_archive_listing(html: str) -> dict[str, date]:
    """`{version: directory_mtime_date}` for every version-shaped directory
    entry in `archive.apache.org/dist/cassandra/`'s autoindex HTML.

    Cross-check (existence) use only -- see module docstring for why this
    function's own dates are never used as a release's `release_date`.
    """
    versions: dict[str, date] = {}
    for name, date_str in _ARCHIVE_ROW_RE.findall(html):
        if re.match(r"^\d+\.\d+(\.\d+)?$", name):
            versions[name] = date.fromisoformat(date_str)
    return versions


def _exponential_backoff(attempt: int) -> float:
    import random

    exp = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


@dataclass(frozen=True)
class ReleaseCollectionResult:
    """Output of one `ReleaseCollector.collect()` run."""

    release: pa.Table
    release_count: int
    archive_checked: bool


class ReleaseCollector:
    """GA-release collector: git tags (primary) + archive.apache.org
    (existence cross-check). See module docstring."""

    source_id = "release"

    def __init__(
        self,
        config: ProjectConfig,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep_fn: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        releases_config = getattr(config, "releases", None)
        self._archive_url = getattr(releases_config, "archive_url", None) or DEFAULT_ARCHIVE_URL
        self._max_retries = max_retries
        self._sleep_fn = sleep_fn
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ReleaseCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get_with_retry(self, url: str) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"request to {url} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._sleep_fn(_exponential_backoff(attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"request to {url} failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._sleep_fn(_exponential_backoff(attempt))
                continue

            response.raise_for_status()
            return response

    def fetch_archive_versions(self) -> dict[str, date] | None:
        """`{version: mtime_date}` from the live archive listing, or `None`
        if the fetch failed after retries -- never raises (module
        docstring: a cross-check outage is never fatal to this source)."""
        try:
            response = self._get_with_retry(self._archive_url)
        except CollectionError:
            return None
        return _parse_archive_listing(response.text)

    def collect(
        self,
        *,
        repo_path: str | Path,
        repo_label: str,
        tag_prefix: str,
        snapshot_id: str,
    ) -> ReleaseCollectionResult:
        """List every GA tag in `repo_path`'s clone, cross-check each
        against the live archive listing, and return the validated
        `release` table.

        `repo_path` must already be cloned (caller's responsibility, same
        as `GitCollector.collect`) -- this never clones/fetches itself.
        """
        tags = list_ga_tags(repo_path, tag_prefix)
        archive_versions = self.fetch_archive_versions()
        collected_at = datetime.now(timezone.utc)

        rows: list[dict[str, Any]] = []
        for tag in tags:
            if archive_versions is None:
                archive_verified: bool | None = None
                archive_date: date | None = None
            else:
                archive_verified = tag.version in archive_versions
                archive_date = archive_versions.get(tag.version)
            rows.append(
                {
                    "release_id": tag.tag_name,
                    "tag_name": tag.tag_name,
                    "version": tag.version,
                    "major_minor": tag.major_minor,
                    "release_date": tag.release_date,
                    "release_date_source": "git_tag",
                    "archive_verified": archive_verified,
                    "archive_date": archive_date,
                    "repo": repo_label,
                    "source_snapshot_id": snapshot_id,
                    "collected_at": collected_at,
                }
            )

        table = validate("release", _rows_to_table(rows, get_schema("release")))
        return ReleaseCollectionResult(
            release=table,
            release_count=len(rows),
            archive_checked=archive_versions is not None,
        )
