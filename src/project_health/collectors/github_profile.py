"""GitHub profile company-field collector (D6; issue #52).

Fetches the public `company` field from a GitHub user's profile
(``GET /users/{login}``) for ``github_login`` identifiers seen in commits or
PRs, once per login, caching results in the ``github_profile`` raw table
(``schema/tables.py``) -- a login already present in that accumulated table
is never re-fetched (D6: "fetched once per login and cached as a raw
table"). Deduping "already cached" logins against new ones is the caller's
job (``pipeline.py``), by reading the accumulated ``github_profile`` table
before calling this collector -- this module only ever fetches the list of
logins it's handed.

Budgeted and incremental, per this project's API-budget rule: ``collect()``
takes ``max_profiles`` and stops after that many *new* fetches; a GitHub-side
rate limit (403/429 with ``X-RateLimit-Remaining: 0``) stops the run cleanly
-- whatever profiles were already fetched this call are still returned,
never discarded, and ``GitHubProfileCollectionResult.rate_limited`` tells the
caller to mark this run's source status accordingly rather than raising.

A 404 (deleted/renamed login) still counts as "fetched": it's cached with
``company = None`` so this collector doesn't retry a dead login forever.

Unauthenticated requests are rate-limited at 60/hour; a ``GITHUB_TOKEN``
(``token=``) raises that to 5,000/hour, matching the budget this project's
other GitHub-facing collection (the nightly run) already assumes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pyarrow as pa

from project_health.collectors.retry import exponential_backoff, is_transient_body_error
from project_health.schema import get_schema, validate

DEFAULT_MAX_RETRIES = 5
# GitHub's own unauthenticated budget is much tighter than JIRA's -- pace
# requests a bit more conservatively than jira.py's collector by default.
DEFAULT_MIN_REQUEST_INTERVAL = 0.25
DEFAULT_TIMEOUT = 15.0


class CollectionError(Exception):
    """Raised when a GitHub profile request fails after exhausting retries."""


@dataclass(frozen=True)
class GitHubProfileCollectionResult:
    """Output of one `GitHubProfileCollector.collect()` call."""

    profiles: pa.Table
    profiles_collected: int
    # True iff a GitHub rate limit was hit before every requested login was
    # fetched -- the run should stop cleanly, not raise (this project's
    # "API use must ... stop cleanly with a partial state" rule).
    rate_limited: bool


class GitHubProfileCollector:
    """Fetches `GET /users/{login}` for a batch of GitHub logins."""

    source_id = "github_profile"

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
        token: str | None = None,
    ) -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url="https://api.github.com",
            transport=transport,
            timeout=timeout,
            headers=headers,
        )
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._last_request_at: float | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubProfileCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _respect_rate_limit(self) -> None:
        if self._min_request_interval <= 0:
            return
        now = self._clock()
        if self._last_request_at is not None:
            wait = self._min_request_interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
        self._last_request_at = self._clock()

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = None
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = exponential_backoff(attempt)
        self._sleep_fn(delay)

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code == 403:
            return response.headers.get("X-RateLimit-Remaining") == "0"
        return False

    def collect(
        self,
        logins: list[str],
        *,
        snapshot_id: str,
        max_profiles: int | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> GitHubProfileCollectionResult:
        """Fetch `company` for each of `logins`, stopping after
        `max_profiles` new fetches or a GitHub rate limit, whichever comes
        first. `logins` should already be deduped by the caller against
        previously-cached logins (this collector always (re-)fetches every
        login it's given).
        """
        rows: list[dict] = []
        rate_limited = False

        for login in logins:
            if max_profiles is not None and len(rows) >= max_profiles:
                break

            attempt = 0
            while True:
                attempt += 1
                self._respect_rate_limit()
                try:
                    response = self._client.get(f"/users/{login}")
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= self._max_retries:
                        raise CollectionError(
                            f"GitHub profile request for {login!r} failed after "
                            f"{attempt} attempt(s): {exc}"
                        ) from exc
                    self._backoff(attempt, retry_after=None)
                    continue

                if self._is_rate_limited(response):
                    rate_limited = True
                    break

                if response.status_code == 404:
                    rows.append(
                        {
                            "login": login,
                            "company": None,
                            "fetched_at": now_fn(),
                            "source_snapshot_id": snapshot_id,
                        }
                    )
                    break

                if response.status_code >= 500:
                    if attempt >= self._max_retries:
                        raise CollectionError(
                            f"GitHub profile request for {login!r} failed after "
                            f"{attempt} attempt(s): HTTP {response.status_code}"
                        )
                    self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                    continue

                response.raise_for_status()
                try:
                    payload = response.json()
                except Exception as exc:
                    if not is_transient_body_error(exc):
                        raise
                    # issue #86: a truncated/undecodable profile body is
                    # retried exactly like a 5xx.
                    if attempt >= self._max_retries:
                        raise CollectionError(
                            f"GitHub profile request for {login!r} returned an "
                            f"undecodable response body after {attempt} attempt(s): {exc}"
                        ) from exc
                    self._backoff(attempt, retry_after=None)
                    continue
                rows.append(
                    {
                        "login": login,
                        "company": payload.get("company"),
                        "fetched_at": now_fn(),
                        "source_snapshot_id": snapshot_id,
                    }
                )
                break

            if rate_limited:
                break

        schema = get_schema("github_profile")
        table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
        return GitHubProfileCollectionResult(
            profiles=validate("github_profile", table),
            profiles_collected=len(rows),
            rate_limited=rate_limited,
        )
