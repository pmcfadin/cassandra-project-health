"""GitHub PR collector (ARCHITECTURE.md §2.2 `SourceCollector`; issue #51).

Collects **metadata only** for `apache/cassandra` and the related repos
configured under `pull_requests.repos` (`projects/<id>.yaml`), via the GitHub
GraphQL API — no PR body or comment text is ever fetched into a persisted
column. A PR's title is hashed (`pr.title_hash`, sha256 hex) immediately on
receipt and the raw string is discarded; nothing downstream of
`_normalize_pr` ever sees it, except a structured extraction of any
`CASSANDRA-NNNNN`-style ticket key it names (`pr.linked_issue_keys`, issue
#102) -- a small, non-content fact (the same kind of thing
`collectors/reviewer_trailer.py::extract_issue_keys` already pulls out of a
git commit message), not the title text. This mirrors DECISIONS.md D1:
PR/comment *bodies* are Phase 2a (classification) territory, not Phase 1.

Emits three normalized tables (ARCHITECTURE.md §3, `schema/tables.py`):

- ``pr`` — one row per pull request, `author_raw_type='github_login'` /
  `author_raw_value` carrying the raw GitHub login (`*_identity_id` left
  `null` for identity resolution, #6, to fill in later); `linked_issue_keys`
  (issue #102) is the JIRA ticket key(s) the title named, used by
  review-responsiveness to find "PRs whose title names the ticket."
- ``pr_review`` — one row per `PullRequestReview` node, `reviewer_raw_value`
  = the reviewing user's login.
- ``pr_comment`` — one row per comment, either a general PR-conversation
  comment (`comment_type='issue_comment'`) or a comment attached to a review
  (`comment_type='review_comment'`, `review_id` set to the parent review).

Bot accounts (`dependabot`, `github-actions[bot]`, ...) are excluded from
each table independently, per `bot_patterns` entries with `field:
github_login` (`projects/<id>.yaml`) — a bot-authored PR is dropped from
`pr`, but a human review/comment on that same PR is still collected, and
vice versa.

## Watermark strategy — an opaque GraphQL cursor, not a timestamp

Unlike `collectors/jira.py`'s JQL `updated >= "<literal>"` filter, GitHub's
GraphQL `pullRequests` connection has no server-side "since" argument
(verified via schema introspection, 2026-09-25) — a repo's PRs are always
walked from the connection's own ordering. This collector orders the
connection by `{field: UPDATED_AT, direction: ASC}` and stores
`pageInfo.endCursor` itself as `next_watermark`; because Relay-style cursors
encode a position in *that specific ordering*, passing the stored cursor
back in as `after` on the next run resumes exactly where this run's fetch
window left off — including any older PR whose `updatedAt` has since moved
past the cursor's position (a new review or comment bumps `updatedAt`,
re-sorting that PR to reappear ahead of the cursor). This is the same
"re-pull touched records fully" contract ARCHITECTURE.md §4.3 describes for
JIRA, achieved without needing a literal date filter at all. The watermark
is stored **per repo** (`state/watermarks.json` key `f"github_pr:{repo}"`,
via `project_health.storage.read_watermark`/`write_watermark`), since each
configured repo's PR history sorts and cursors independently.

## Rate-limit budgeting — stop cleanly, never hammer

Every query requests `rateLimit { remaining resetAt cost }` alongside the
real data (free — it reflects usage *after* this call's own cost, not an
extra call). After each page, if `remaining` has dropped to or below
`rate_limit_floor`, this collector stops paginating **that repo**
immediately and marks it `'rate_limited'` in the returned
`GitHubRepoOutcome` — the rows already collected this run are kept and
returned (never discarded), and the repo's watermark only advances to the
last page actually processed, so the next run resumes cleanly rather than
re-fetching. Once one repo hits the floor, every repo after it in
`pull_requests.repos` is skipped outright (`status='skipped'`, watermark
untouched) rather than spending the little budget left on a partial second
repo — the whole point of a shared 5,000/hr GraphQL budget (DATA-SOURCES.md
§3) is that *some* repo finishing cleanly beats *every* repo finishing half
finished. A hard GraphQL/HTTP failure (`CollectionError`, after
`max_retries` is exhausted) only marks that one repo `'failed'` and moves on
to the next repo — a single flaky repo shouldn't block the others the way a
shared rate-limit budget legitimately should.

## `pr_issue_link` historical backfill (issue #105)

`pr.linked_issue_keys` (issue #102) only gets populated on a PR row written
after that column existed; every PR already on the data branch before then
has it null, and the watermark strategy above means the ordinary incremental
collector will never re-visit one of those PRs again on its own (nothing
about it moves `updatedAt`). `fetch_pr_issue_link_backfill_page` and
`schema/tables.py`'s `PR_ISSUE_LINK` table (a new table, deliberately not a
column added to the existing `pr` table -- see that schema's own docstring)
close that gap: `pipeline._collect_github_pr_issue_link_backfill` drives a
separate, budgeted, resumable page-cursor walk of each configured repo's
full PR history (ascending `createdAt`), fetching only `number`/`title` per
page and discarding the title immediately after extracting any ticket key
from it, same as the ordinary collector's own `title_hash`/
`linked_issue_keys` handling.

## Known limitation: nested pagination is single-page

`reviews`, a review's `comments`, and a PR's `comments` are each fetched as
one page (`REVIEWS_PAGE_SIZE`/`REVIEW_COMMENTS_PAGE_SIZE`/
`PR_COMMENTS_PAGE_SIZE` below) rather than being paginated to exhaustion.
Real Cassandra PRs verified 2026-09-25 rarely approach these caps (a
handful of reviews/comments per PR is typical), but a PR that legitimately
exceeds one of these caps will have its overflow reviews/comments silently
missing unless that PR's `updatedAt` moves again later (at which point the
whole PR, including a fresh full page of its nested connections, is
re-fetched) — flagged here rather than solved, per the same "collect
imperfectly but honestly, not silently" discipline `collectors/jira.py`
documents for its own watermark precision limit.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pyarrow as pa

from project_health.collectors.retry import exponential_backoff, is_transient_body_error
from project_health.collectors.reviewer_trailer import extract_issue_keys
from project_health.config import BotPattern, ProjectConfig
from project_health.schema import get_schema, validate

# --- Tuning constants (issue #51) -------------------------------------------

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_RETRIES = 5
DEFAULT_MIN_REQUEST_INTERVAL = 0.25
DEFAULT_TIMEOUT = 30.0
# 5,000 req/hr shared budget (DATA-SOURCES.md §3, `GH_PAT`) is often shared
# across several collectors/repos in one nightly run (ARCHITECTURE.md §7.4)
# -- stop well short of exhausting it outright.
DEFAULT_RATE_LIMIT_FLOOR = 500

# See module docstring "Known limitation: nested pagination is single-page".
REVIEWS_PAGE_SIZE = 100
REVIEW_COMMENTS_PAGE_SIZE = 50
PR_COMMENTS_PAGE_SIZE = 100

# --- `pr_issue_link` historical backfill (issue #105) ------------------------
#
# A separate, much cheaper query than `_QUERY` above -- `number`/`title` only,
# no nested reviews/comments -- since the backfill's only job is discovering
# which JIRA ticket key(s) a *pre-existing* PR's title named
# (`schema/tables.py` PR_ISSUE_LINK docstring). Ordered by `CREATED_AT` (not
# `UPDATED_AT` like `_QUERY`): the backfill's cursor is walked exactly once,
# start to finish, per repo, so there's no "re-visit a PR whose activity
# moved" concern `_QUERY`'s own ordering choice exists to serve -- creation
# order is simpler and, being immutable, gives a cursor that's always safe to
# resume from.
PR_ISSUE_LINK_BACKFILL_PAGE_SIZE = 100

_PR_ISSUE_LINK_BACKFILL_QUERY = """
query($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  rateLimit { remaining resetAt cost }
  repository(owner: $owner, name: $name) {
    pullRequests(first: $pageSize, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title }
    }
  }
}
"""

GRAPHQL_ENDPOINT = "https://api.github.com"

_QUERY = """
query($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  rateLimit { remaining resetAt cost }
  repository(owner: $owner, name: $name) {
    pullRequests(first: $pageSize, after: $cursor, orderBy: {field: UPDATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        state
        title
        isDraft
        merged
        additions
        deletions
        changedFiles
        author { login }
        createdAt
        updatedAt
        closedAt
        mergedAt
        reviews(first: %(reviews_page_size)d) {
          nodes {
            id
            state
            submittedAt
            author { login }
            comments(first: %(review_comments_page_size)d) {
              nodes { id createdAt author { login } }
            }
          }
        }
        comments(first: %(pr_comments_page_size)d) {
          nodes { id createdAt author { login } }
        }
      }
    }
  }
}
""" % {
    "reviews_page_size": REVIEWS_PAGE_SIZE,
    "review_comments_page_size": REVIEW_COMMENTS_PAGE_SIZE,
    "pr_comments_page_size": PR_COMMENTS_PAGE_SIZE,
}

# Shared PR-node field selection (identical shape `_QUERY` already requests)
# -- factored out so the three "bounded recency window" queries below
# (issue #145 fixup, orchestrator review of PR #147: a fresh peer's first
# backfill must reach *recent* months, not oldest-first history) request
# exactly the same fields `_normalize_pr`/`_normalize_review`/
# `_normalize_comment` already know how to read, with no shape drift.
_PR_NODE_FIELDS = """
        id
        number
        state
        title
        isDraft
        merged
        additions
        deletions
        changedFiles
        author { login }
        createdAt
        updatedAt
        closedAt
        mergedAt
        reviews(first: %(reviews_page_size)d) {
          nodes {
            id
            state
            submittedAt
            author { login }
            comments(first: %(review_comments_page_size)d) {
              nodes { id createdAt author { login } }
            }
          }
        }
        comments(first: %(pr_comments_page_size)d) {
          nodes { id createdAt author { login } }
        }
""" % {
    "reviews_page_size": REVIEWS_PAGE_SIZE,
    "review_comments_page_size": REVIEW_COMMENTS_PAGE_SIZE,
    "pr_comments_page_size": PR_COMMENTS_PAGE_SIZE,
}

# Pass A (issue #145 fixup): newest-created-first, so a project with no
# prior collection history reaches *recent* months immediately rather than
# walking oldest-first from the beginning of the repo's whole history
# (verified live, 2026-10-09: `orderBy: {field: CREATED_AT, direction:
# DESC}` returns the newest PR first). The caller stops paginating once a
# page's oldest node falls before the comparison window's start -- this
# query itself applies no date filter; it's a plain, unbounded cursor walk
# the caller bounds by inspecting `createdAt`.
_PRS_CREATED_DESC_QUERY = (
    """
query($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  rateLimit { remaining resetAt cost }
  repository(owner: $owner, name: $name) {
    pullRequests(first: $pageSize, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {"""
    + _PR_NODE_FIELDS
    + """      }
    }
  }
}
"""
)

# Pass B (issue #145 fixup): every currently-open PR, regardless of age --
# an open-PR backlog reconstruction needs to know about a long-lived open
# PR created long before the comparison window even starts (verified live,
# 2026-10-09: apache/kafka has an open PR created 2024-06-18, well outside
# any 36-month window as of this writing). No date bound at all; the
# caller pages to exhaustion (`hasNextPage: false`) or the rate-limit floor.
_OPEN_PRS_QUERY = (
    """
query($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  rateLimit { remaining resetAt cost }
  repository(owner: $owner, name: $name) {
    pullRequests(
      first: $pageSize, after: $cursor, states: [OPEN]
      orderBy: {field: CREATED_AT, direction: ASC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {"""
    + _PR_NODE_FIELDS
    + """      }
    }
  }
}
"""
)

# Pass C (issue #145 fixup): PRs *created before* the comparison window but
# *closed inside it* matter for `change_request_closure_ratio_pr` (a closed
# count bucketed by closed-month) -- pass A's created-date bound alone would
# miss them entirely. Uses GitHub's search API (`is:pr closed:>=<date>`,
# which matches a merge the same as a plain close -- merging closes a PR)
# rather than a second `orderBy: UPDATED_AT DESC` walk: search's own
# `closed:` qualifier is an exact, server-side filter on the field this
# pass actually cares about, where `updatedAt` would also catch irrelevant
# non-closing updates (a label change, a new comment) with no bound on how
# many such updates to page through before reaching ones that matter.
# Verified live, 2026-10-09: cost is a flat 1 point regardless of result
# count, far cheaper than a nested-connection `pullRequests` page.
_PR_SEARCH_QUERY = (
    """
query($searchQuery: String!, $cursor: String, $pageSize: Int!) {
  rateLimit { remaining resetAt cost }
  search(query: $searchQuery, type: ISSUE, first: $pageSize, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {"""
    + _PR_NODE_FIELDS
    + """      }
    }
  }
}
"""
)


class CollectionError(Exception):
    """Raised when a GitHub GraphQL request fails after exhausting retries."""


class RateLimitExhausted(CollectionError):
    """Raised when GitHub's GraphQL API itself reports the primary rate limit hit.

    Distinct from the proactive `rateLimit.remaining <= floor` stop (which
    never raises — it just stops paginating cleanly): this is GitHub telling
    us we're already over budget via a `RATE_LIMITED` GraphQL error, which
    the collector still handles by stopping cleanly rather than propagating.
    """


# --- Token resolution (issue #51) -------------------------------------------


def _gh_auth_token() -> str | None:
    """`gh auth token` subprocess fallback, for local (non-CI) runs."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token or None


def resolve_github_token(
    env: Mapping[str, str] | None = None,
    gh_auth_token: Callable[[], str | None] = _gh_auth_token,
) -> str | None:
    """Resolve a GitHub token: `GH_PAT`, else `GITHUB_TOKEN`, else `gh auth token`.

    `env` and `gh_auth_token` are injectable so tests can exercise every
    branch of this precedence without touching real environment variables or
    spawning a real subprocess. Returns `None` (never raises) if nothing is
    available -- an unauthenticated `GitHubCollector` can still work against
    public repos, just at GitHub's much lower unauthenticated rate limit.
    """
    env = env if env is not None else os.environ
    for key in ("GH_PAT", "GITHUB_TOKEN"):
        value = env.get(key)
        if value:
            return value
    return gh_auth_token()


# --- Timestamp / hashing helpers --------------------------------------------


def _parse_gh_timestamp(value: str) -> datetime:
    """Parse a GitHub GraphQL `DateTime` scalar (`2026-09-25T21:38:41Z`) to aware UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _hash_title(title: str) -> str:
    """sha256 hex digest of a PR title -- metadata only, per module docstring."""
    return hashlib.sha256(title.encode("utf-8")).hexdigest()


# --- Bot filtering (issue #51) -----------------------------------------------


def _is_bot_login(login: str | None, bot_patterns: list[BotPattern]) -> bool:
    import re

    if not login:
        return False
    return any(
        re.search(pattern.regex, login)
        for pattern in bot_patterns
        if pattern.field == "github_login"
    )


# --- Row normalization --------------------------------------------------


def _normalize_pr(node: dict, repo_label: str, source_snapshot_id: str) -> dict:
    author = node.get("author") or {}
    closed_at = node.get("closedAt")
    merged_at = node.get("mergedAt")
    title = node.get("title") or ""
    return {
        "repo": repo_label,
        "number": node["number"],
        "state": node["state"],
        "is_draft": node["isDraft"],
        "merged": node["merged"],
        "author_identity_id": None,
        "author_raw_type": "github_login",
        "author_raw_value": author.get("login"),
        "title_hash": _hash_title(title),
        # issue #102: every `CASSANDRA-NNNNN`-style ticket key named in the
        # title, extracted before the raw title is discarded (see module
        # docstring and schema/tables.py `PR.linked_issue_keys`) -- reuses the
        # same regex the git-commit-trailer parser already uses so a PR title
        # and a commit trailer's "for CASSANDRA-N" clause parse identically.
        "linked_issue_keys": list(extract_issue_keys(title)),
        "created_at": _parse_gh_timestamp(node["createdAt"]),
        "updated_at": _parse_gh_timestamp(node["updatedAt"]),
        "closed_at": _parse_gh_timestamp(closed_at) if closed_at else None,
        "merged_at": _parse_gh_timestamp(merged_at) if merged_at else None,
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "changed_files": node.get("changedFiles"),
        "source_snapshot_id": source_snapshot_id,
    }


def _normalize_pr_issue_link_rows(
    number: int, title: str | None, repo_label: str, source_snapshot_id: str
) -> list[dict]:
    """One `pr_issue_link` row per ticket key named in `title` (issue #105).

    Mirrors `_normalize_pr`'s `linked_issue_keys` extraction exactly (same
    `extract_issue_keys` call), but for the historical backfill's standalone
    `(number, title)` fetch rather than a full PR node -- `title` is never
    returned to any caller past this function (schema/tables.py PR_ISSUE_LINK
    docstring: "metadata only, D1"). An empty list for a title with no
    recognizable ticket key.
    """
    return [
        {
            "repo": repo_label,
            "number": number,
            "issue_key": key,
            "source_snapshot_id": source_snapshot_id,
        }
        for key in extract_issue_keys(title or "")
    ]


def _normalize_review(
    node: dict, repo_label: str, pr_number: int, source_snapshot_id: str
) -> dict:
    reviewer = node.get("author") or {}
    submitted_at = node.get("submittedAt")
    return {
        "review_id": node["id"],
        "repo": repo_label,
        "pr_number": pr_number,
        "reviewer_identity_id": None,
        "reviewer_raw_type": "github_login",
        "reviewer_raw_value": reviewer.get("login"),
        "state": node["state"],
        "submitted_at": _parse_gh_timestamp(submitted_at) if submitted_at else None,
        "source_snapshot_id": source_snapshot_id,
    }


def _normalize_comment(
    node: dict,
    repo_label: str,
    pr_number: int,
    comment_type: str,
    review_id: str | None,
    source_snapshot_id: str,
) -> dict:
    author = node.get("author") or {}
    return {
        "comment_id": node["id"],
        "repo": repo_label,
        "pr_number": pr_number,
        "review_id": review_id,
        "comment_type": comment_type,
        "author_identity_id": None,
        "author_raw_type": "github_login",
        "author_raw_value": author.get("login"),
        "created_at": _parse_gh_timestamp(node["createdAt"]),
        "source_snapshot_id": source_snapshot_id,
    }


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


# --- Results --------------------------------------------------------------


@dataclass(frozen=True)
class GitHubRepoOutcome:
    """Per-repo outcome of one `GitHubCollector.collect()` call."""

    repo: str
    # status: 'ok' | 'rate_limited' | 'failed' | 'skipped'
    status: str
    # opaque GraphQL cursor (see module docstring), or the input watermark
    # unchanged if this repo made no progress this run
    next_watermark: str | None
    pr_count: int
    review_count: int
    comment_count: int
    bot_prs_excluded: int
    bot_reviews_excluded: int
    bot_comments_excluded: int
    error: str | None = None


@dataclass(frozen=True)
class GitHubCollectionResult:
    """Output of one `GitHubCollector.collect()` run, across all configured repos."""

    prs: pa.Table
    reviews: pa.Table
    comments: pa.Table
    repos: dict[str, GitHubRepoOutcome] = field(default_factory=dict)
    # status: 'ok' if every configured repo's outcome was 'ok'; 'partial' if the
    # rest were only 'rate_limited'/'skipped' (a clean, resumable backfill-in-
    # progress -- issue #54, pipeline.py review note on issue #51: a large
    # first backfill legitimately spans several nightly runs via each repo's
    # own watermark, and that isn't the same thing as a failure); 'failed' if
    # any repo hit a hard `CollectionError` (ARCHITECTURE.md §7.3 -- callers
    # use this to record source-level status without losing the partial data
    # this run did collect).
    status: str = "ok"


# --- Collector ----------------------------------------------------------


class GitHubCollector:
    """GitHub PR/review/comment collector -- `SourceCollector` (ARCHITECTURE.md §2.2).

    Reads the repo list from `config.pull_requests.repos` (`["owner/name",
    ...]`) and the bot-login patterns from `config.bot_patterns` (entries
    with `field: github_login`). Metadata only -- see module docstring.
    """

    source_id = "github_pr"

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
        pull_requests = config.pull_requests
        repos = getattr(pull_requests, "repos", None) if pull_requests else None
        if not repos:
            raise ValueError(
                "config.pull_requests.repos is required for GitHubCollector "
                "(a list of 'owner/name' strings)"
            )
        self._repos: list[str] = list(repos)
        self._bot_patterns = list(config.bot_patterns)

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

    @property
    def rate_limit_floor(self) -> int:
        """This collector's configured `rate_limit_floor` (module docstring
        "Rate-limit budgeting") -- exposed read-only so a caller driving its
        own paginated loop against the same GraphQL budget (issue #105's
        `pr_issue_link` backfill, `pipeline._collect_github_pr_issue_link_
        backfill`) can stop at the same floor `collect()` itself respects,
        without reaching into a private attribute.
        """
        return self._rate_limit_floor

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- HTTP plumbing (mirrors collectors/jira.py's pattern) ---------------

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

    def _post_with_retry(self, payload: dict) -> dict:
        """POST one GraphQL query, retrying transient failures.

        Returns the response's `data` object. Raises `RateLimitExhausted` if
        GitHub's GraphQL API itself reports a `RATE_LIMITED` error, and
        `CollectionError` for any other unrecoverable failure -- both are
        caught by `_collect_repo`, which stops cleanly rather than
        propagating (module docstring, "Rate-limit budgeting").
        """
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

            # Secondary rate limit / abuse detection surfaces as 403 with a
            # Retry-After header; primary limit and 5xx both retry the same way.
            if response.status_code in (403, 429) or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub GraphQL request failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            try:
                body = response.json()
            except Exception as exc:
                if not is_transient_body_error(exc):
                    raise
                # issue #86: a truncated/undecodable GraphQL body (seen live:
                # `json.JSONDecodeError` mid-string) is retried exactly like
                # a 5xx rather than propagating straight through.
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub GraphQL request returned an undecodable response body "
                        f"after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue
            errors = body.get("errors")
            if errors:
                error_types = {e.get("type") for e in errors if isinstance(e, dict)}
                if "RATE_LIMITED" in error_types:
                    raise RateLimitExhausted(f"GitHub GraphQL primary rate limit hit: {errors}")
                raise CollectionError(f"GitHub GraphQL returned error(s): {errors}")
            return body["data"]

    # --- Per-repo collection --------------------------------------------

    def _collect_repo(
        self,
        repo_label: str,
        watermark: str | None,
        source_snapshot_id: str,
        max_prs: int | None,
    ) -> tuple[list[dict], list[dict], list[dict], GitHubRepoOutcome]:
        owner, name = repo_label.split("/", 1)
        cursor = watermark
        last_cursor = watermark

        pr_rows: list[dict] = []
        review_rows: list[dict] = []
        comment_rows: list[dict] = []
        bot_prs = bot_reviews = bot_comments = 0
        fetched = 0
        status = "ok"
        error: str | None = None

        while True:
            payload = {
                "query": _QUERY,
                "variables": {
                    "owner": owner,
                    "name": name,
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

            rate_limit = data.get("rateLimit") or {}
            remaining = rate_limit.get("remaining")

            connection = data["repository"]["pullRequests"]
            for pr_node in connection["nodes"]:
                fetched += 1
                pr_number = pr_node["number"]
                author_login = (pr_node.get("author") or {}).get("login")
                if _is_bot_login(author_login, self._bot_patterns):
                    bot_prs += 1
                else:
                    pr_rows.append(_normalize_pr(pr_node, repo_label, source_snapshot_id))

                for review_node in pr_node["reviews"]["nodes"]:
                    reviewer_login = (review_node.get("author") or {}).get("login")
                    if _is_bot_login(reviewer_login, self._bot_patterns):
                        bot_reviews += 1
                    else:
                        review_rows.append(
                            _normalize_review(
                                review_node, repo_label, pr_number, source_snapshot_id
                            )
                        )
                    for comment_node in review_node["comments"]["nodes"]:
                        commenter_login = (comment_node.get("author") or {}).get("login")
                        if _is_bot_login(commenter_login, self._bot_patterns):
                            bot_comments += 1
                            continue
                        comment_rows.append(
                            _normalize_comment(
                                comment_node,
                                repo_label,
                                pr_number,
                                "review_comment",
                                review_node["id"],
                                source_snapshot_id,
                            )
                        )

                for comment_node in pr_node["comments"]["nodes"]:
                    commenter_login = (comment_node.get("author") or {}).get("login")
                    if _is_bot_login(commenter_login, self._bot_patterns):
                        bot_comments += 1
                        continue
                    comment_rows.append(
                        _normalize_comment(
                            comment_node,
                            repo_label,
                            pr_number,
                            "issue_comment",
                            None,
                            source_snapshot_id,
                        )
                    )

            page_info = connection["pageInfo"]
            last_cursor = page_info.get("endCursor") or last_cursor

            if max_prs is not None and fetched >= max_prs:
                break

            if remaining is not None and remaining <= self._rate_limit_floor:
                status = "rate_limited"
                error = (
                    f"rate limit budget floor reached ({remaining} remaining "
                    f"<= floor {self._rate_limit_floor})"
                )
                break

            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        outcome = GitHubRepoOutcome(
            repo=repo_label,
            status=status,
            next_watermark=last_cursor,
            pr_count=len(pr_rows),
            review_count=len(review_rows),
            comment_count=len(comment_rows),
            bot_prs_excluded=bot_prs,
            bot_reviews_excluded=bot_reviews,
            bot_comments_excluded=bot_comments,
            error=error,
        )
        return pr_rows, review_rows, comment_rows, outcome

    # --- `pr_issue_link` historical backfill (issue #105) -------------------

    def fetch_pr_issue_link_backfill_page(
        self,
        repo_label: str,
        cursor: str | None,
        page_size: int = PR_ISSUE_LINK_BACKFILL_PAGE_SIZE,
    ) -> dict:
        """Fetch one raw GraphQL page (`number`/`title` per PR node, plus
        `rateLimit`) for the `pr_issue_link` backfill.

        Returns the raw `data` payload -- same "collector returns raw
        payload, caller owns the page-budget loop and cursor persistence"
        split `collectors/jira.py::JiraCollector.fetch_patch_available_
        backfill_page` uses for its own resumable backfill -- so
        `pipeline._collect_github_pr_issue_link_backfill` can extract keys
        via `extract_issue_keys`/`_normalize_pr_issue_link_rows`, check
        `rateLimit.remaining` against `self.rate_limit_floor` exactly like
        `_collect_repo` does, and persist the resulting per-repo cursor.
        Raises `RateLimitExhausted`/`CollectionError` exactly like
        `_post_with_retry` -- the caller is expected to catch both, stopping
        cleanly rather than propagating (module docstring "Rate-limit
        budgeting").
        """
        owner, name = repo_label.split("/", 1)
        payload = {
            "query": _PR_ISSUE_LINK_BACKFILL_QUERY,
            "variables": {
                "owner": owner,
                "name": name,
                "cursor": cursor,
                "pageSize": page_size,
            },
        }
        return self._post_with_retry(payload)

    # --- Bounded-recency-window passes (issue #145 fixup) --------------------
    #
    # Three additional, independent page-at-a-time fetchers -- same "collector
    # returns raw payload, caller owns the page-budget loop/cursor" split as
    # `fetch_pr_issue_link_backfill_page` above -- that together let a caller
    # with no prior collection history (`peers/github.py`) reach a *recent*
    # window of PR activity without walking a whole repo's history
    # oldest-first first. See each query constant's own docstring for what
    # each pass is for and why it exists.

    def fetch_prs_created_desc_page(
        self, repo_label: str, cursor: str | None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> dict:
        """One page of `_PRS_CREATED_DESC_QUERY` (newest-created-first)."""
        owner, name = repo_label.split("/", 1)
        payload = {
            "query": _PRS_CREATED_DESC_QUERY,
            "variables": {"owner": owner, "name": name, "cursor": cursor, "pageSize": page_size},
        }
        return self._post_with_retry(payload)

    def fetch_open_prs_page(
        self, repo_label: str, cursor: str | None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> dict:
        """One page of `_OPEN_PRS_QUERY` (every currently-open PR)."""
        owner, name = repo_label.split("/", 1)
        payload = {
            "query": _OPEN_PRS_QUERY,
            "variables": {"owner": owner, "name": name, "cursor": cursor, "pageSize": page_size},
        }
        return self._post_with_retry(payload)

    def fetch_pr_search_page(
        self, search_query: str, cursor: str | None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> dict:
        """One page of `_PR_SEARCH_QUERY` (`search_query` is a full GitHub
        search-syntax string, e.g. `"repo:apache/kafka is:pr
        closed:>=2023-09-01"`)."""
        payload = {
            "query": _PR_SEARCH_QUERY,
            "variables": {"searchQuery": search_query, "cursor": cursor, "pageSize": page_size},
        }
        return self._post_with_retry(payload)

    # --- Top-level collection --------------------------------------------

    def collect(
        self,
        watermarks: dict[str, str | None] | None = None,
        snapshot_id: str | None = None,
        max_prs_per_repo: int | None = None,
    ) -> GitHubCollectionResult:
        """Collect PRs/reviews/comments for every configured repo.

        `watermarks` maps `"owner/name" -> prior next_watermark` (module
        docstring's opaque cursor); a repo missing from the mapping (or the
        mapping being `None`) starts a full backfill for that repo.
        `max_prs_per_repo`, when set, stops each repo's pagination early once
        that many PR nodes have been fetched (used by the live smoke check to
        stay bounded) without requesting a page beyond what's needed.

        Once one repo's outcome is `'rate_limited'`, every subsequent
        configured repo is recorded `'skipped'` with its watermark
        untouched -- see module docstring "Rate-limit budgeting".
        """
        snapshot_id = snapshot_id or str(uuid.uuid4())
        watermarks = watermarks or {}

        all_pr_rows: list[dict] = []
        all_review_rows: list[dict] = []
        all_comment_rows: list[dict] = []
        outcomes: dict[str, GitHubRepoOutcome] = {}
        budget_exhausted = False

        for repo_label in self._repos:
            if budget_exhausted:
                outcomes[repo_label] = GitHubRepoOutcome(
                    repo=repo_label,
                    status="skipped",
                    next_watermark=watermarks.get(repo_label),
                    pr_count=0,
                    review_count=0,
                    comment_count=0,
                    bot_prs_excluded=0,
                    bot_reviews_excluded=0,
                    bot_comments_excluded=0,
                    error="rate limit budget exhausted by an earlier repo this run",
                )
                continue

            pr_rows, review_rows, comment_rows, outcome = self._collect_repo(
                repo_label,
                watermarks.get(repo_label),
                snapshot_id,
                max_prs_per_repo,
            )
            all_pr_rows.extend(pr_rows)
            all_review_rows.extend(review_rows)
            all_comment_rows.extend(comment_rows)
            outcomes[repo_label] = outcome
            if outcome.status == "rate_limited":
                budget_exhausted = True

        prs_table = validate("pr", _rows_to_table(all_pr_rows, get_schema("pr")))
        reviews_table = validate(
            "pr_review", _rows_to_table(all_review_rows, get_schema("pr_review"))
        )
        comments_table = validate(
            "pr_comment", _rows_to_table(all_comment_rows, get_schema("pr_comment"))
        )

        statuses = {o.status for o in outcomes.values()}
        if statuses <= {"ok"}:
            overall_status = "ok"
        elif "failed" in statuses:
            overall_status = "failed"
        else:
            # only 'rate_limited'/'skipped' remain -- a clean, resumable
            # partial backfill, not a failure (see GitHubCollectionResult
            # docstring).
            overall_status = "partial"

        return GitHubCollectionResult(
            prs=prs_table,
            reviews=reviews_table,
            comments=comments_table,
            repos=outcomes,
            status=overall_status,
        )
