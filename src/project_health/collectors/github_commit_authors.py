"""GitHub commit-author association collector (D6; issue #52 fixup cycle 1).

Cross-references each git commit author's raw `git_email` against the
GitHub account GitHub itself asserts committed it, via the GraphQL
`repository.ref.target.history` connection's `author.user.login` field --
an EXACT, platform-asserted fact, not a guess, for `config.repos[0]` (the
same repo `collectors/git.py` walks for `contribution_event`).

This is the missing link that makes GitHub-profile-`company` seeding (D6,
`collectors/github_profile.py`) actually useful for most real Cassandra
contributors: the overwhelming majority of commit-email domains
(gmail.com, apache.org, personal domains) can never be resolved by
`org_domains.yaml`'s reviewed domain map, but many of those same emails DO
belong to a GitHub account whose profile names an employer. Verified live
against `apache/cassandra`, 2026-09-25: e.g. `calebrackliffe@gmail.com`
(unresolvable by domain) is GitHub login `maedhroz`, whose public profile
`company` field is `"Apple"`.

Emits one raw table, `github_commit_author` (schema/tables.py): one row per
`(sha, email, login)` triple where GitHub found a linked account for that
commit's author email. A commit whose author has no linked GitHub account
(`author.user` is `null`) is not written -- there is nothing to cache.
`normalize.identity.link_github_commit_authors` turns the accumulated table
into automated, high-confidence (`confidence='high'`) `identity_link` rows.

## Watermark -- an opaque GraphQL cursor, mirroring collectors/github.py

`history`'s Relay cursor encodes a position in the ref's own commit
traversal (verified live, 2026-09-25: `endCursor` looks like
`"<oid> <index>"`), walked newest-first from the ref's tip. Storing the
last page's `endCursor` as this table's own watermark
(`storage.read_watermark(..., table="github_commit_author")`, its own
per-table key per issue #53's "backfill gap" fix, since this table is added
well after `git`'s own watermark already exists) lets a later run resume
from that same position even after the branch tip has moved forward: the
new tip's history still contains every commit the cursor's position
identifies (git history before a commit never changes), so resuming from
the stored cursor walks exactly the commits added since, without
re-fetching everything already cached. A brand-new data dir walks the
*entire* branch history once (32,376 commits / 100 per page = 324 pages for
`apache/cassandra`'s trunk, verified 2026-09-25).

## Budget -- 'partial' is an expected, self-healing state, not a failure

`max_pages`, when set, stops the walk after that many pages regardless of
`hasNextPage` -- this project's "API use must be budgeted per run and
incremental" rule. Reaching either that budget or the proactive
`rate_limit_floor` stop (same mechanism as `collectors/github.py`) marks
this run's outcome `'partial'` (not `'failed'` or `'rate_limited'`): by
design, expected on a large first backfill, and self-healing since the next
run's stored cursor picks up exactly where this one left off. `'rate_limited'`
is reserved for the *reactive* case -- GitHub's GraphQL API itself returning
a `RATE_LIMITED` error despite the proactive floor (an unexpected, tighter
squeeze than planned for) -- and `'failed'` for a hard, non-rate-limit
`CollectionError` after retries are exhausted.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pyarrow as pa

from project_health.collectors.github import (
    CollectionError,
    RateLimitExhausted,
    resolve_github_token,
)
from project_health.config import ProjectConfig
from project_health.schema import get_schema, validate

DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_RETRIES = 5
DEFAULT_MIN_REQUEST_INTERVAL = 0.25
DEFAULT_TIMEOUT = 30.0
# Same shared-5,000/hr-budget reasoning as collectors/github.py.
DEFAULT_RATE_LIMIT_FLOOR = 500
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 20.0

GRAPHQL_ENDPOINT = "https://api.github.com"

_QUERY = """
query($owner: String!, $name: String!, $branch: String!, $cursor: String, $pageSize: Int!) {
  rateLimit { remaining resetAt cost }
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $branch) {
      target {
        ... on Commit {
          history(first: $pageSize, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              author { email user { login } }
            }
          }
        }
      }
    }
  }
}
"""


def _exponential_backoff(attempt: int) -> float:
    exp = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


@dataclass(frozen=True)
class GitHubCommitAuthorOutcome:
    """Outcome of one `GitHubCommitAuthorCollector.collect()` call."""

    repo: str
    # status: 'ok' | 'partial' | 'rate_limited' | 'failed' (module docstring)
    status: str
    next_watermark: str | None
    commits_seen: int
    associations_found: int
    pages_fetched: int
    error: str | None = None


@dataclass(frozen=True)
class GitHubCommitAuthorCollectionResult:
    associations: pa.Table
    outcome: GitHubCommitAuthorOutcome


class GitHubCommitAuthorCollector:
    """Walks one repo's default branch commit history for author->login
    associations (module docstring). `source_id` distinct from
    `collectors/github.py`'s `'github_pr'` -- this is its own source with
    its own watermark, wired into `pipeline.py` independently.
    """

    source_id = "github_commit_authors"

    def __init__(
        self,
        config: ProjectConfig,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        rate_limit_floor: int = DEFAULT_RATE_LIMIT_FLOOR,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
        token_resolver: Callable[[], str | None] = resolve_github_token,
    ) -> None:
        if not config.repos:
            raise ValueError(
                "config.repos is required for GitHubCommitAuthorCollector "
                "(the same repo the git collector walks)"
            )
        repo_cfg = config.repos[0]
        self._owner = repo_cfg.owner
        self._name = repo_cfg.name
        self._branch = repo_cfg.default_branch

        self._page_size = page_size
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._rate_limit_floor = rate_limit_floor
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._last_request_at: float | None = None

        resolved_token = token if token is not None else token_resolver()
        headers = {"Accept": "application/vnd.github+json"}
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"

        self._client = httpx.Client(
            base_url=GRAPHQL_ENDPOINT, transport=transport, timeout=timeout, headers=headers
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubCommitAuthorCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- HTTP plumbing (mirrors collectors/github.py's pattern) -------------

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
            delay = _exponential_backoff(attempt)
        self._sleep_fn(delay)

    def _post_with_retry(self, payload: dict) -> dict:
        attempt = 0
        while True:
            attempt += 1
            self._respect_rate_limit()
            try:
                response = self._client.post("/graphql", json=payload)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub GraphQL request timed out after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub GraphQL request failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue

            if response.status_code in (403, 429) or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub GraphQL request failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            body = response.json()
            errors = body.get("errors")
            if errors:
                error_types = {e.get("type") for e in errors if isinstance(e, dict)}
                if "RATE_LIMITED" in error_types:
                    raise RateLimitExhausted(f"GitHub GraphQL primary rate limit hit: {errors}")
                raise CollectionError(f"GitHub GraphQL returned error(s): {errors}")
            return body["data"]

    # --- Collection ------------------------------------------------------

    def collect(
        self,
        watermark: str | None,
        snapshot_id: str,
        max_pages: int | None = None,
    ) -> GitHubCommitAuthorCollectionResult:
        cursor = watermark
        rows: list[dict] = []
        commits_seen = 0
        pages = 0
        status = "ok"
        error: str | None = None

        while True:
            if max_pages is not None and pages >= max_pages:
                status = "partial"
                error = f"max_pages budget ({max_pages}) reached"
                break

            payload = {
                "query": _QUERY,
                "variables": {
                    "owner": self._owner,
                    "name": self._name,
                    "branch": self._branch,
                    "cursor": cursor,
                    "pageSize": self._page_size,
                },
            }
            try:
                data = self._post_with_retry(payload)
            except RateLimitExhausted as exc:
                status = "rate_limited"
                error = str(exc)
                break
            except CollectionError as exc:
                status = "failed"
                error = str(exc)
                break

            pages += 1
            rate_limit = data.get("rateLimit") or {}
            remaining = rate_limit.get("remaining")

            ref = (data.get("repository") or {}).get("ref")
            if ref is None:
                status = "failed"
                error = f"branch {self._branch!r} not found on {self._owner}/{self._name}"
                break
            history = ref["target"]["history"]

            for node in history["nodes"]:
                commits_seen += 1
                oid = node["oid"]
                author = node.get("author") or {}
                email = author.get("email")
                user = author.get("user")
                login = user.get("login") if user else None
                if email and login:
                    rows.append(
                        {
                            "sha": oid,
                            "email": email,
                            "login": login,
                            "source_snapshot_id": snapshot_id,
                        }
                    )

            page_info = history["pageInfo"]
            cursor = page_info.get("endCursor") or cursor

            if remaining is not None and remaining <= self._rate_limit_floor:
                status = "partial"
                error = (
                    f"rate limit budget floor reached ({remaining} remaining "
                    f"<= floor {self._rate_limit_floor})"
                )
                break

            if not page_info["hasNextPage"]:
                break

        table = validate(
            "github_commit_author", _rows_to_table(rows, get_schema("github_commit_author"))
        )
        outcome = GitHubCommitAuthorOutcome(
            repo=f"{self._owner}/{self._name}",
            status=status,
            next_watermark=cursor,
            commits_seen=commits_seen,
            associations_found=len(rows),
            pages_fetched=pages,
            error=error,
        )
        return GitHubCommitAuthorCollectionResult(associations=table, outcome=outcome)
