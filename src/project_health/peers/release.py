"""GA-release discovery + independent-source verification for peer repos
(issue #145).

## GA tag discovery -- reused pattern, new (no-clone) source

`collectors.release.list_ga_tags`/`_ga_version` already define exactly the
tag-inclusion rule issue #145 asks for ("GA release tags with a per-project
tag pattern," excluding alpha/beta/rc pre-release suffixes) -- this module
imports that same `_ga_version`/`_major_minor` logic rather than
reimplementing it, so a peer's tag matches the identical rule Cassandra's
own release collector uses.

What's new is the *source*: `list_ga_tags` reads a local git clone's
`refs/tags/`, but a peer's GA tags can live on maintenance/release branches
a disk-safe `--single-branch` shallow clone (`peers.git_clone`) never
fetches at all -- verified live during this issue's own research: Flink's
GA tags are `release-X.Y.Z` on release branches, not `master`. Fetching
every peer's tags via the GitHub REST Tags API instead (`fetch_ga_tags`,
below) sidesteps that gap entirely, costs nothing against the GraphQL point
budget `collect.py`'s PR collection shares (it's the REST API, a separate
~5,000 req/hr budget), and needs no local clone/disk at all.

## Independent-source verification

`verify_release_counts` cross-checks each peer's git-tag-derived per-year
GA count against a second, independent system, per `peers.yaml`'s
`release_verification` -- existence/timing cross-check only, same
"never authoritative" discipline `collectors.release`'s own
archive.apache.org check documents: the tag-derived date always wins,
this only reports whether a second system's own record roughly agrees.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import httpx

from project_health.collectors.release import _ga_version, _major_minor
from project_health.collectors.retry import exponential_backoff
from project_health.peers.config import (
    GithubReleasesVerification,
    JiraReleaseVerification,
    PeerProject,
    PypiReleaseVerification,
)

DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 30.0
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class GaTag:
    """One GA release, as derived from a single GitHub tag (mirrors
    `collectors.release.GaTag`'s shape so downstream code -- `release` raw
    table rows -- builds identically either way)."""

    tag_name: str
    version: str
    major_minor: str
    release_date: date


class CollectionError(Exception):
    pass


def _request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_retries: int,
    sleep_fn: Callable[[float], None],
) -> httpx.Response | None:
    """GET/paginate helper shared by every REST call in this module.
    Returns `None` (never raises) for a 404 -- several of these endpoints
    (GitHub Releases, PyPI) 404 cleanly for a project that doesn't publish
    through that channel at all (verified live for apache/datafusion's
    GitHub Releases, 2026-10-09), which this module treats as "zero items,"
    not a collection failure."""
    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.request(method, url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= max_retries:
                raise CollectionError(f"{url} failed after {attempt} attempt(s): {exc}") from exc
            sleep_fn(exponential_backoff(attempt))
            continue

        if response.status_code == 404:
            return None
        if response.status_code == 429 or response.status_code >= 500:
            if attempt >= max_retries:
                raise CollectionError(
                    f"{url} failed after {attempt} attempt(s): HTTP {response.status_code}"
                )
            sleep_fn(exponential_backoff(attempt))
            continue
        response.raise_for_status()
        return response


def fetch_ga_tags(
    owner: str,
    name: str,
    tag_prefix: str,
    *,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    sleep_fn: Callable[[float], None] = time.sleep,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[GaTag]:
    """Every GA tag for `owner/name`, via the GitHub REST Tags API
    (`GET /repos/{owner}/{name}/tags`, paginated) -- no local clone needed
    (module docstring).

    The Tags API's own `commit.sha` is already the *commit* a tag points
    to (annotated tags pre-peeled server-side), so one follow-up call per
    GA-pattern-matching tag (`GET /repos/{owner}/{name}/commits/{sha}`)
    gets that commit's own authored date -- used as `release_date` here
    (a disclosed simplification versus `collectors.release`'s tagger-date
    preference for an annotated tag: the gap between a commit's author
    date and its tag's own creatordate is typically hours, never material
    at monthly-bucket granularity for a context-only comparison page, D25).
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    tags: list[GaTag] = []
    with httpx.Client(
        base_url="https://api.github.com", transport=transport, timeout=timeout, headers=headers
    ) as client:
        page = 1
        while True:
            response = _request_with_retry(
                client,
                "GET",
                f"/repos/{owner}/{name}/tags?per_page={page_size}&page={page}",
                max_retries=max_retries,
                sleep_fn=sleep_fn,
            )
            if response is None:
                break
            items: list[dict[str, Any]] = response.json()
            if not items:
                break

            for item in items:
                version = _ga_version(item["name"], tag_prefix)
                if version is None:
                    continue
                sha = item["commit"]["sha"]
                commit_response = _request_with_retry(
                    client,
                    "GET",
                    f"/repos/{owner}/{name}/commits/{sha}",
                    max_retries=max_retries,
                    sleep_fn=sleep_fn,
                )
                if commit_response is None:
                    continue
                commit = commit_response.json()
                date_str = commit["commit"]["committer"]["date"]
                release_date = (
                    datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    .astimezone(timezone.utc)
                    .date()
                )
                tags.append(
                    GaTag(
                        tag_name=item["name"],
                        version=version,
                        major_minor=_major_minor(version),
                        release_date=release_date,
                    )
                )

            if len(items) < page_size:
                break
            page += 1

    tags.sort(key=lambda t: (t.release_date, t.tag_name))
    return tags


def _per_year_counts(dates: list[date]) -> dict[int, int]:
    return dict(Counter(d.year for d in dates))


@dataclass(frozen=True)
class VerificationResult:
    """Per-year GA counts from the git-tag-derived primary source versus an
    independent source, for the years both have any data -- `peers.
    pipeline`'s real-run report pastes this verbatim (issue #145
    acceptance: "verify per-year counts against an independent source ...
    and paste them")."""

    source_type: str
    tag_derived_by_year: dict[int, int]
    independent_by_year: dict[int, int]
    fetch_error: str | None = None


def _fetch_jira_release_dates(
    verification: JiraReleaseVerification,
    *,
    transport: httpx.BaseTransport | None,
    timeout: float,
    max_retries: int,
    sleep_fn: Callable[[float], None],
) -> list[date]:
    base_url = verification.base_url.rstrip("/")
    url = f"{base_url}/rest/api/2/project/{verification.project_key}/versions"
    with httpx.Client(transport=transport, timeout=timeout) as client:
        response = _request_with_retry(
            client, "GET", url, max_retries=max_retries, sleep_fn=sleep_fn
        )
    if response is None:
        return []
    versions: list[dict[str, Any]] = response.json()
    dates: list[date] = []
    for v in versions:
        if v.get("released") and v.get("releaseDate"):
            dates.append(date.fromisoformat(v["releaseDate"]))
    return dates


def _fetch_github_release_dates(
    peer: PeerProject,
    *,
    token: str | None,
    transport: httpx.BaseTransport | None,
    timeout: float,
    max_retries: int,
    sleep_fn: Callable[[float], None],
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[date]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    dates: list[date] = []
    with httpx.Client(
        base_url="https://api.github.com", transport=transport, timeout=timeout, headers=headers
    ) as client:
        page = 1
        while True:
            response = _request_with_retry(
                client,
                "GET",
                f"/repos/{peer.repo}/releases?per_page={page_size}&page={page}",
                max_retries=max_retries,
                sleep_fn=sleep_fn,
            )
            if response is None:
                break
            items: list[dict[str, Any]] = response.json()
            if not items:
                break
            for item in items:
                published_at = item.get("published_at")
                if published_at:
                    dates.append(
                        datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                        .astimezone(timezone.utc)
                        .date()
                    )
            if len(items) < page_size:
                break
            page += 1
    return dates


def _fetch_pypi_release_dates(
    verification: PypiReleaseVerification,
    *,
    transport: httpx.BaseTransport | None,
    timeout: float,
    max_retries: int,
    sleep_fn: Callable[[float], None],
) -> list[date]:
    url = f"https://pypi.org/pypi/{verification.package}/json"
    with httpx.Client(transport=transport, timeout=timeout) as client:
        response = _request_with_retry(
            client, "GET", url, max_retries=max_retries, sleep_fn=sleep_fn
        )
    if response is None:
        return []
    data = response.json()
    dates: list[date] = []
    for files in data.get("releases", {}).values():
        if not files:
            continue
        upload_time = files[0].get("upload_time_iso_8601")
        if upload_time:
            dates.append(
                datetime.fromisoformat(upload_time.replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .date()
            )
    return dates


def verify_release_counts(
    peer: PeerProject,
    tag_dates: list[date],
    *,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> VerificationResult:
    """Per-year GA counts from `tag_dates` (the primary, git-tag-derived
    source) versus `peer.release_verification`'s independent source.

    Never raises on a fetch failure -- `fetch_error` is set instead and
    `independent_by_year` is `{}`, matching `collectors.release`'s own
    "a cross-check outage is never fatal to this source" discipline.
    """
    verification = peer.release_verification
    try:
        if isinstance(verification, JiraReleaseVerification):
            independent_dates = _fetch_jira_release_dates(
                verification,
                transport=transport,
                timeout=timeout,
                max_retries=max_retries,
                sleep_fn=sleep_fn,
            )
        elif isinstance(verification, GithubReleasesVerification):
            independent_dates = _fetch_github_release_dates(
                peer,
                token=token,
                transport=transport,
                timeout=timeout,
                max_retries=max_retries,
                sleep_fn=sleep_fn,
            )
        elif isinstance(verification, PypiReleaseVerification):
            independent_dates = _fetch_pypi_release_dates(
                verification,
                transport=transport,
                timeout=timeout,
                max_retries=max_retries,
                sleep_fn=sleep_fn,
            )
        else:  # pragma: no cover - exhaustive per config.py's Union
            raise ValueError(f"unknown release_verification: {verification!r}")
    except CollectionError as exc:
        return VerificationResult(
            source_type=verification.type,
            tag_derived_by_year=_per_year_counts(tag_dates),
            independent_by_year={},
            fetch_error=str(exc),
        )

    return VerificationResult(
        source_type=verification.type,
        tag_derived_by_year=_per_year_counts(tag_dates),
        independent_by_year=_per_year_counts(independent_dates),
    )
