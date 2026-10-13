"""GitHub Issues collector (ARCHITECTURE.md §2.2 `SourceCollector`; issue
#174, epic #168 6a).

Collects **metadata only** for every repo configured under a project's
`github_issues.repos` (`projects/<id>.yaml`'s `GitHubIssuesConfig`, issue
#169) via the GitHub GraphQL `repository.issues` connection -- deliberately
never `pullRequests` (that's `collectors/github.py`'s own table). Some
subprojects (`easy-stress`) use GitHub Issues as their ONLY tracker; others
(`gocql-driver`, `ccm`) have it enabled ALONGSIDE a JIRA `issue_tracker:`.

Emits two normalized tables (ARCHITECTURE.md §3, `schema/tables.py`):

- ``gh_issue`` -- one row per issue, `author_raw_type='github_login'` /
  `author_raw_value` carrying the raw GitHub login (`*_identity_id` left
  `null` for identity resolution, #6), plus `labels` (label names only --
  never the issue title/body, D1) and `state`/created/closed timestamps.
- ``gh_issue_comment`` -- one row per `IssueComment` node: author and
  timestamp only, same "metadata only" discipline as JIRA's `issue_comment`
  table -- the comment *body* is never fetched into a persisted column.

Bot accounts (`dependabot`, `github-actions[bot]`, ...) are excluded from
each table independently, per `bot_patterns` entries with
`field: github_login` (`projects/<id>.yaml`) -- a bot-authored issue is
dropped from `gh_issue`, but a human comment on that same issue is still
collected, and vice versa. This is `collectors/github.py`'s own
collection-time bot-filtering convention (not JIRA's "collect raw, filter at
metrics-compute time" convention) -- deliberate, since this is a GitHub
collector reusing GitHub conventions.

## Watermark strategy -- identical to `collectors/github.py`'s own

Same opaque-GraphQL-cursor strategy as the PR collector's own module
docstring: the `issues` connection is ordered
`{field: UPDATED_AT, direction: ASC}`, and `pageInfo.endCursor` is stored as
`next_watermark` per repo (`state/watermarks.json` key
`f"github_issues:{repo}"`). Passing that cursor back in as `after` resumes
exactly where the prior run's fetch window left off, including any older
issue whose `updatedAt` has since moved past the cursor's position (a new
comment or label bumps `updatedAt`, re-sorting that issue to reappear ahead
of the cursor).

## Rate-limit budgeting -- reuses `collectors/github.py`'s own floor helpers

This collector does NOT re-derive the "stop cleanly once the shared GraphQL
budget's floor is reached" arithmetic -- it imports and calls
`collectors.github.github_rate_limit_effective_floor`/
`github_rate_limit_floor_reached` directly (the exact same free functions
`GitHubCollector.effective_floor`/`.floor_reached` themselves delegate to),
so the two collectors' "some repo finishing cleanly beats every repo
finishing half finished" stop behavior can never drift apart. Once one repo
hits the floor, every repo after it in `github_issues.repos` is skipped
outright (`status='skipped'`), exactly like `collectors/github.py`'s own
multi-repo budget.

## Known limitation: comment pagination is single-page

`comments`, like `collectors/github.py`'s own `reviews`/`comments`
connections, is fetched as one page per issue (`COMMENTS_PAGE_SIZE` below)
rather than paginated to exhaustion -- flagged here rather than solved, per
this project's "collect imperfectly but honestly, not silently" discipline.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pyarrow as pa

from project_health.collectors.github import (
    GRAPHQL_ENDPOINT,
    CollectionError,
    RateLimitExhausted,
    github_rate_limit_effective_floor,
    github_rate_limit_floor_reached,
    resolve_github_token,
)
from project_health.collectors.retry import exponential_backoff, is_transient_body_error
from project_health.config import BotPattern, ProjectConfig
from project_health.schema import get_schema, validate

# --- Tuning constants (issue #174) ------------------------------------------

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_RETRIES = 5
DEFAULT_MIN_REQUEST_INTERVAL = 0.25
DEFAULT_TIMEOUT = 30.0
# Same shared 5,000 req/hr GraphQL budget `collectors/github.py` documents
# (DATA-SOURCES.md §3, `GH_PAT`) -- a project with `github_issues` enabled
# alongside `pull_requests` (gocql-driver, ccm) spends from the same pool
# across both collectors in one nightly run.
DEFAULT_RATE_LIMIT_FLOOR = 500

# See module docstring "Known limitation: comment pagination is single-page".
LABELS_PAGE_SIZE = 20
COMMENTS_PAGE_SIZE = 50

GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  rateLimit { limit remaining resetAt cost }
  repository(owner: $owner, name: $name) {
    issues(first: $pageSize, after: $cursor, orderBy: {field: UPDATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        state
        author { login }
        createdAt
        updatedAt
        closedAt
        labels(first: %(labels_page_size)d) {
          nodes { name }
        }
        comments(first: %(comments_page_size)d) {
          nodes { id createdAt author { login } }
        }
      }
    }
  }
}
""" % {
    "labels_page_size": LABELS_PAGE_SIZE,
    "comments_page_size": COMMENTS_PAGE_SIZE,
}


# --- Bot filtering (mirrors collectors/github.py's own) ---------------------


def _is_bot_login(login: str | None, bot_patterns: list[BotPattern]) -> bool:
    import re

    if not login:
        return False
    return any(
        re.search(pattern.regex, login)
        for pattern in bot_patterns
        if pattern.field == "github_login"
    )


# --- Timestamp parsing (mirrors collectors/github.py's own) -----------------


def _parse_gh_timestamp(value: str) -> datetime:
    """Parse a GitHub GraphQL `DateTime` scalar (`2026-09-25T21:38:41Z`) to aware UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


# --- Row normalization --------------------------------------------------


def _normalize_issue(node: dict, repo_label: str, source_snapshot_id: str) -> dict:
    author = node.get("author") or {}
    closed_at = node.get("closedAt")
    labels = [label["name"] for label in (node.get("labels") or {}).get("nodes", [])]
    return {
        "repo": repo_label,
        "number": node["number"],
        "state": node["state"],
        "author_identity_id": None,
        "author_raw_type": "github_login",
        "author_raw_value": author.get("login"),
        "labels": labels,
        "created_at": _parse_gh_timestamp(node["createdAt"]),
        "updated_at": _parse_gh_timestamp(node["updatedAt"]),
        "closed_at": _parse_gh_timestamp(closed_at) if closed_at else None,
        "source_snapshot_id": source_snapshot_id,
    }


def _normalize_comment(
    node: dict, repo_label: str, issue_number: int, source_snapshot_id: str
) -> dict:
    author = node.get("author") or {}
    return {
        "comment_id": node["id"],
        "repo": repo_label,
        "issue_number": issue_number,
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
class GitHubIssuesRepoOutcome:
    """Per-repo outcome of one `GitHubIssuesCollector.collect()` call."""

    repo: str
    # status: 'ok' | 'rate_limited' | 'failed' | 'skipped'
    status: str
    next_watermark: str | None
    issue_count: int
    comment_count: int
    bot_issues_excluded: int
    bot_comments_excluded: int
    error: str | None = None


@dataclass(frozen=True)
class GitHubIssuesCollectionResult:
    """Output of one `GitHubIssuesCollector.collect()` run, across all
    configured repos (mirrors `collectors.github.GitHubCollectionResult`'s
    own three-valued `status`)."""

    issues: pa.Table
    comments: pa.Table
    repos: dict[str, GitHubIssuesRepoOutcome] = field(default_factory=dict)
    status: str = "ok"


# --- Collector ----------------------------------------------------------


class GitHubIssuesCollector:
    """GitHub Issues collector -- `SourceCollector` (ARCHITECTURE.md §2.2).

    Reads the repo list from `config.github_issues.repos` (`["owner/name",
    ...]`) and the bot-login patterns from `config.bot_patterns` (entries
    with `field: github_login`) -- same config shape `collectors/github.py`
    reads for `pull_requests.repos`. Metadata only -- see module docstring.
    """

    source_id = "github_issues"

    def __init__(
        self,
        config: ProjectConfig,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        rate_limit_floor: int = DEFAULT_RATE_LIMIT_FLOOR,
        rate_limit_floor_fraction: float | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
        token_resolver: Callable[[], str | None] = resolve_github_token,
    ) -> None:
        github_issues = config.github_issues
        repos = getattr(github_issues, "repos", None) if github_issues else None
        if not repos:
            raise ValueError(
                "config.github_issues.repos is required for GitHubIssuesCollector "
                "(a list of 'owner/name' strings)"
            )
        self._repos: list[str] = list(repos)
        self._bot_patterns = list(config.bot_patterns)

        self._page_size = page_size
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._rate_limit_floor = rate_limit_floor
        self._rate_limit_floor_fraction = rate_limit_floor_fraction
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
        """This collector's configured `rate_limit_floor`."""
        return self._rate_limit_floor

    def effective_floor(self, rate_limit: dict | None) -> int:
        """The stop threshold for a page whose response carried `rate_limit`
        -- delegates to `collectors.github.github_rate_limit_effective_floor`
        (module docstring "Rate-limit budgeting")."""
        return github_rate_limit_effective_floor(
            rate_limit, self._rate_limit_floor, self._rate_limit_floor_fraction
        )

    def floor_reached(self, rate_limit: dict | None) -> bool:
        """True once `remaining` is at or below `effective_floor` --
        delegates to `collectors.github.github_rate_limit_floor_reached`."""
        return github_rate_limit_floor_reached(
            rate_limit, self._rate_limit_floor, self._rate_limit_floor_fraction
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubIssuesCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- HTTP plumbing (mirrors collectors/github.py's own pattern) --------

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
        """POST one GraphQL query, retrying transient failures. Returns the
        response's `data` object -- same retry/backoff/error-classification
        contract as `collectors.github.GitHubCollector._post_with_retry`."""
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
            try:
                body = response.json()
            except Exception as exc:
                if not is_transient_body_error(exc):
                    raise
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
        max_issues: int | None,
    ) -> tuple[list[dict], list[dict], GitHubIssuesRepoOutcome]:
        owner, name = repo_label.split("/", 1)
        cursor = watermark
        last_cursor = watermark

        issue_rows: list[dict] = []
        comment_rows: list[dict] = []
        bot_issues = bot_comments = 0
        fetched = 0
        status = "ok"
        error: str | None = None

        while True:
            payload = {
                "query": GRAPHQL_QUERY,
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

            connection = data["repository"]["issues"]
            for issue_node in connection["nodes"]:
                fetched += 1
                issue_number = issue_node["number"]
                author_login = (issue_node.get("author") or {}).get("login")
                if _is_bot_login(author_login, self._bot_patterns):
                    bot_issues += 1
                else:
                    issue_rows.append(_normalize_issue(issue_node, repo_label, source_snapshot_id))

                for comment_node in issue_node["comments"]["nodes"]:
                    commenter_login = (comment_node.get("author") or {}).get("login")
                    if _is_bot_login(commenter_login, self._bot_patterns):
                        bot_comments += 1
                        continue
                    comment_rows.append(
                        _normalize_comment(
                            comment_node, repo_label, issue_number, source_snapshot_id
                        )
                    )

            page_info = connection["pageInfo"]
            last_cursor = page_info.get("endCursor") or last_cursor

            if max_issues is not None and fetched >= max_issues:
                break

            if self.floor_reached(rate_limit):
                status = "rate_limited"
                error = (
                    f"rate limit budget floor reached ({remaining} remaining "
                    f"<= floor {self.effective_floor(rate_limit)})"
                )
                break

            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        outcome = GitHubIssuesRepoOutcome(
            repo=repo_label,
            status=status,
            next_watermark=last_cursor,
            issue_count=len(issue_rows),
            comment_count=len(comment_rows),
            bot_issues_excluded=bot_issues,
            bot_comments_excluded=bot_comments,
            error=error,
        )
        return issue_rows, comment_rows, outcome

    # --- Top-level collection --------------------------------------------

    def collect(
        self,
        watermarks: dict[str, str | None] | None = None,
        snapshot_id: str | None = None,
        max_issues_per_repo: int | None = None,
    ) -> GitHubIssuesCollectionResult:
        """Collect issues/comments for every configured repo.

        `watermarks` maps `"owner/name" -> prior next_watermark`; a repo
        missing from the mapping (or the mapping being `None`) starts a full
        backfill for that repo. `max_issues_per_repo`, when set, stops each
        repo's pagination early once that many issue nodes have been
        fetched (used by the live smoke check to stay bounded).

        Once one repo's outcome is `'rate_limited'`, every subsequent
        configured repo is recorded `'skipped'` with its watermark
        untouched -- see module docstring "Rate-limit budgeting".
        """
        snapshot_id = snapshot_id or str(uuid.uuid4())
        watermarks = watermarks or {}

        all_issue_rows: list[dict] = []
        all_comment_rows: list[dict] = []
        outcomes: dict[str, GitHubIssuesRepoOutcome] = {}
        budget_exhausted = False

        for repo_label in self._repos:
            if budget_exhausted:
                outcomes[repo_label] = GitHubIssuesRepoOutcome(
                    repo=repo_label,
                    status="skipped",
                    next_watermark=watermarks.get(repo_label),
                    issue_count=0,
                    comment_count=0,
                    bot_issues_excluded=0,
                    bot_comments_excluded=0,
                    error="rate limit budget exhausted by an earlier repo this run",
                )
                continue

            issue_rows, comment_rows, outcome = self._collect_repo(
                repo_label,
                watermarks.get(repo_label),
                snapshot_id,
                max_issues_per_repo,
            )
            all_issue_rows.extend(issue_rows)
            all_comment_rows.extend(comment_rows)
            outcomes[repo_label] = outcome
            if outcome.status == "rate_limited":
                budget_exhausted = True

        issues_table = validate("gh_issue", _rows_to_table(all_issue_rows, get_schema("gh_issue")))
        comments_table = validate(
            "gh_issue_comment",
            _rows_to_table(all_comment_rows, get_schema("gh_issue_comment")),
        )

        statuses = {o.status for o in outcomes.values()}
        if statuses <= {"ok"}:
            overall_status = "ok"
        elif "failed" in statuses:
            overall_status = "failed"
        else:
            overall_status = "partial"

        return GitHubIssuesCollectionResult(
            issues=issues_table,
            comments=comments_table,
            repos=outcomes,
            status=overall_status,
        )

