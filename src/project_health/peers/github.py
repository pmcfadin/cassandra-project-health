"""GitHub PR/review/comment collection for peer repos (issue #145; fixup,
orchestrator review of PR #147).

Reuses `collectors.github.GitHubCollector` **unmodified** for its httpx
client/token/retry/rate-limit-floor machinery, plus its three additive
bounded-recency-window fetch methods (`fetch_prs_created_desc_page`/
`fetch_open_prs_page`/`fetch_pr_search_page`) and its private row
normalizers (`_normalize_pr`/`_normalize_review`/`_normalize_comment`/
`_is_bot_login`) -- no GraphQL/HTTP mechanics are reimplemented here.

## Why three passes, not `GitHubCollector.collect()`'s own ASC walk

The original version of this module called `GitHubCollector.collect()`,
whose `_QUERY` walks `orderBy: {field: UPDATED_AT, direction: ASC}` from a
stored cursor -- correct for Cassandra's own collection (which has been
running incrementally for months and is already caught up to "now"), but
wrong for a peer's *first-ever* run: with no prior watermark, that walk
starts at the OLDEST PR in the repo's whole history and would need to
page through years of history before ever reaching a recent month --
verified against the real run this fixup responds to (apache/kafka's
first-ever collection landed PRs from 2013-2017, nowhere near "now").
A peer-context comparison needs the *last 36 months*, not the repo's
oldest history, so this module instead runs three independent, narrower
passes per repo, matching exactly what the five comparison metrics need:

1. **`created_desc`** -- newest-created-first, stopped once a page's PRs
   fall before the comparison window's start. Reaches recent months
   immediately regardless of total repo history size.
2. **`open_prs`** -- every currently-open PR, any age. A PR opened long
   before the window can still be open today, and the open-PR-backlog
   reconstruction needs to know that (verified live: apache/kafka has an
   open PR created 2024-06-18, well outside a 36-month window as of this
   writing).
3. **`closed_search`** -- GitHub's search API, `is:pr closed:>=<window
   start date>`, for PRs created *before* the window but closed *inside*
   it (`change_request_closure_ratio_pr` needs this "closed" side; pass 1
   alone only ever sees PRs *created* in the window).

Each pass is independently resumable across runs (see `PassState` below);
`peers/collect.py` owns reading/writing each pass's persisted state via
`project_health.storage`'s watermark API (this module has no `storage`
dependency of its own -- pure GraphQL-paging mechanics plus the small
amount of stop-condition/resume-state logic those three passes need).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from project_health.collectors.github import (
    BotPattern,
    DEFAULT_PAGE_SIZE,
    GitHubCollector,
    RateLimitExhausted,
    CollectionError,
    _is_bot_login,
    _normalize_comment,
    _normalize_pr,
    _normalize_review,
    _parse_gh_timestamp,
)
from project_health.peers.config import BotPatternConfig, PeerProject

SEARCH_PAGE_SIZE = 100


@dataclass
class _PullRequestsSection:
    repos: list[str]


@dataclass
class _PeerGithubConfig:
    """Duck-typed stand-in for `project_health.config.ProjectConfig` --
    `GitHubCollector.__init__` only reads `.pull_requests.repos` and
    `.bot_patterns` off whatever it's given."""

    pull_requests: _PullRequestsSection
    bot_patterns: list[BotPattern] = field(default_factory=list)


def _to_bot_patterns(patterns: list[BotPatternConfig]) -> list[BotPattern]:
    return [BotPattern(field=p.field, regex=p.regex) for p in patterns]


def build_peer_github_config(
    peers: list[PeerProject], bot_patterns: list[BotPatternConfig]
) -> _PeerGithubConfig:
    """One duck-typed config covering every peer's repo -- constructs the
    single shared `GitHubCollector` instance every pass, for every peer,
    reuses (one client, one token, one rate-limit-floor check)."""
    return _PeerGithubConfig(
        pull_requests=_PullRequestsSection(repos=[peer.repo for peer in peers]),
        bot_patterns=_to_bot_patterns(bot_patterns),
    )


# --- Per-pass resume state --------------------------------------------------


@dataclass(frozen=True)
class PassState:
    """Resumable state for one (peer repo, pass) pair, round-tripped
    through `project_health.storage.read_watermark`/`write_watermark` as a
    JSON string (opaque to `storage.py`, same "store a string, decode it
    yourself" convention `collectors/github.py`'s own opaque GraphQL cursor
    already uses for its own watermark).

    - `cursor`: resume point for a pass that hasn't reached its stop
      condition yet this backfill (budget ran out mid-page-walk). `None`
      means "start this pass fresh from the top" -- either it has never
      run, or it previously ran to completion (see `high_watermark`).
    - `high_watermark` (`created_desc` pass only): the newest `createdAt`
      ever observed, once this pass has completed its *first* full walk
      down to the comparison window's start. Every later run restarts
      fresh from the top but stops as soon as it reaches a PR at or before
      this date -- "catch up on what's new since last time," never
      "re-walk the whole window again." `open_prs`/`closed_search` have no
      equivalent: both are cheap to redo in full every run (bounded by
      "how many PRs are open right now" / "how many closed in the window,"
      not by total repo history), so a completed run of either simply
      clears `cursor` and lets the next run start fresh unconditionally.
    - `newest_seen`: the newest `createdAt` observed so far *during an
      still-incomplete* `created_desc` backfill -- carried forward across
      resumed runs (each resumed page walks toward *older* PRs, so this
      value must come from the very first page of the very first run, not
      be recomputed from whatever page a resume happens to be on) until
      the backfill finally completes, at which point it is promoted to
      `high_watermark`.
    """

    cursor: str | None = None
    high_watermark: str | None = None
    newest_seen: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "cursor": self.cursor,
                "high_watermark": self.high_watermark,
                "newest_seen": self.newest_seen,
            }
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "PassState":
        if raw is None:
            return cls()
        data = json.loads(raw)
        return cls(
            cursor=data.get("cursor"),
            high_watermark=data.get("high_watermark"),
            newest_seen=data.get("newest_seen"),
        )


@dataclass(frozen=True)
class PassResult:
    pr_rows: list[dict]
    review_rows: list[dict]
    comment_rows: list[dict]
    bot_prs_excluded: int
    bot_reviews_excluded: int
    bot_comments_excluded: int
    # status: 'completed' (reached its stop condition / exhausted) |
    # 'partial' (budget floor hit, resumable) | 'rate_limited' (GitHub's
    # own RATE_LIMITED error) | 'failed' (hard CollectionError)
    status: str
    next_state: PassState
    pages_fetched: int = 0
    # Pass C only: the search connection's own exact `issueCount` for this
    # run's query -- a sanity cross-check against `gh api search/issues`'
    # own `total_count` for the same query (issue #145 fixup acceptance).
    issue_count: int | None = None
    error: str | None = None


def _normalize_page_nodes(
    nodes: list[dict],
    repo_label: str,
    bot_patterns: list[BotPattern],
    source_snapshot_id: str,
) -> tuple[list[dict], list[dict], list[dict], int, int, int]:
    """Normalize one page's PR nodes into (pr_rows, review_rows,
    comment_rows, bot_prs_excluded, bot_reviews_excluded,
    bot_comments_excluded) -- shared by every pass, identical bot-exclusion
    and row-shape rules `collectors/github.py::GitHubCollector._collect_repo`
    already applies to its own `_QUERY` nodes (same node shape, `_PR_NODE_
    FIELDS`)."""
    pr_rows: list[dict] = []
    review_rows: list[dict] = []
    comment_rows: list[dict] = []
    bot_prs = bot_reviews = bot_comments = 0

    for pr_node in nodes:
        pr_number = pr_node["number"]
        author_login = (pr_node.get("author") or {}).get("login")
        if _is_bot_login(author_login, bot_patterns):
            bot_prs += 1
        else:
            pr_rows.append(_normalize_pr(pr_node, repo_label, source_snapshot_id))

        for review_node in pr_node["reviews"]["nodes"]:
            reviewer_login = (review_node.get("author") or {}).get("login")
            if _is_bot_login(reviewer_login, bot_patterns):
                bot_reviews += 1
            else:
                review_rows.append(
                    _normalize_review(review_node, repo_label, pr_number, source_snapshot_id)
                )
            for comment_node in review_node["comments"]["nodes"]:
                commenter_login = (comment_node.get("author") or {}).get("login")
                if _is_bot_login(commenter_login, bot_patterns):
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
            if _is_bot_login(commenter_login, bot_patterns):
                bot_comments += 1
                continue
            comment_rows.append(
                _normalize_comment(
                    comment_node, repo_label, pr_number, "issue_comment", None, source_snapshot_id
                )
            )

    return pr_rows, review_rows, comment_rows, bot_prs, bot_reviews, bot_comments


def run_created_desc_pass(
    collector: GitHubCollector,
    repo_label: str,
    state: PassState,
    window_start: date,
    bot_patterns: list[BotPattern],
    source_snapshot_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> PassResult:
    """Pass 1: newest-created-first, stopped at `window_start` (module
    docstring). Restarts fresh from the top whenever `state.high_watermark`
    is set (a prior run already completed this pass once); otherwise
    resumes from `state.cursor` if one is stored (a prior run was cut off
    mid-walk before ever reaching `window_start`)."""
    stop_threshold = window_start
    if state.high_watermark is not None:
        hw_date = _parse_gh_timestamp(state.high_watermark).date()
        stop_threshold = max(window_start, hw_date)
        cursor = None  # restart from the top; high_watermark is the new floor
    else:
        cursor = state.cursor

    newest_seen = (
        _parse_gh_timestamp(state.newest_seen) if state.newest_seen else None
    )

    pr_rows: list[dict] = []
    review_rows: list[dict] = []
    comment_rows: list[dict] = []
    bot_prs = bot_reviews = bot_comments = 0
    pages = 0
    reached_stop = False
    status = "completed"
    error: str | None = None
    last_cursor = cursor

    while True:
        try:
            data = collector.fetch_prs_created_desc_page(repo_label, cursor, page_size)
        except RateLimitExhausted as exc:
            status, error = "rate_limited", str(exc)
            break
        except CollectionError as exc:
            status, error = "failed", str(exc)
            break

        pages += 1
        rate_limit = data.get("rateLimit") or {}
        remaining = rate_limit.get("remaining")
        connection = data["repository"]["pullRequests"]

        kept_nodes = []
        for node in connection["nodes"]:
            created_at = _parse_gh_timestamp(node["createdAt"])
            if newest_seen is None or created_at > newest_seen:
                newest_seen = created_at
            if created_at.date() < stop_threshold:
                reached_stop = True
                break  # DESC order: everything after this is also too old
            kept_nodes.append(node)

        p_rows, r_rows, c_rows, bp, br, bc = _normalize_page_nodes(
            kept_nodes, repo_label, bot_patterns, source_snapshot_id
        )
        pr_rows.extend(p_rows)
        review_rows.extend(r_rows)
        comment_rows.extend(c_rows)
        bot_prs += bp
        bot_reviews += br
        bot_comments += bc

        page_info = connection["pageInfo"]
        last_cursor = page_info.get("endCursor") or last_cursor

        if reached_stop or not page_info["hasNextPage"]:
            status = "completed"
            break
        if remaining is not None and remaining <= collector.rate_limit_floor:
            status = "partial"
            break
        cursor = page_info["endCursor"]

    if status == "completed":
        next_state = PassState(
            cursor=None,
            high_watermark=(newest_seen.isoformat() if newest_seen else state.high_watermark),
            newest_seen=None,
        )
    elif status == "partial":
        next_state = PassState(
            cursor=last_cursor,
            high_watermark=state.high_watermark,
            newest_seen=(newest_seen.isoformat() if newest_seen else state.newest_seen),
        )
    else:
        next_state = state  # rate_limited/failed: leave state untouched, retry next run

    return PassResult(
        pr_rows=pr_rows,
        review_rows=review_rows,
        comment_rows=comment_rows,
        bot_prs_excluded=bot_prs,
        bot_reviews_excluded=bot_reviews,
        bot_comments_excluded=bot_comments,
        status=status,
        next_state=next_state,
        pages_fetched=pages,
        error=error,
    )


def run_open_prs_pass(
    collector: GitHubCollector,
    repo_label: str,
    state: PassState,
    bot_patterns: list[BotPattern],
    source_snapshot_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> PassResult:
    """Pass 2: every currently-open PR, any age (module docstring). Always
    cheap to redo in full once completed -- `state.cursor` only matters for
    resuming a run the budget floor cut off mid-walk."""
    cursor = state.cursor
    pr_rows: list[dict] = []
    review_rows: list[dict] = []
    comment_rows: list[dict] = []
    bot_prs = bot_reviews = bot_comments = 0
    pages = 0
    status = "completed"
    error: str | None = None
    last_cursor = cursor

    while True:
        try:
            data = collector.fetch_open_prs_page(repo_label, cursor, page_size)
        except RateLimitExhausted as exc:
            status, error = "rate_limited", str(exc)
            break
        except CollectionError as exc:
            status, error = "failed", str(exc)
            break

        pages += 1
        rate_limit = data.get("rateLimit") or {}
        remaining = rate_limit.get("remaining")
        connection = data["repository"]["pullRequests"]

        p_rows, r_rows, c_rows, bp, br, bc = _normalize_page_nodes(
            connection["nodes"], repo_label, bot_patterns, source_snapshot_id
        )
        pr_rows.extend(p_rows)
        review_rows.extend(r_rows)
        comment_rows.extend(c_rows)
        bot_prs += bp
        bot_reviews += br
        bot_comments += bc

        page_info = connection["pageInfo"]
        last_cursor = page_info.get("endCursor") or last_cursor

        if not page_info["hasNextPage"]:
            status = "completed"
            break
        if remaining is not None and remaining <= collector.rate_limit_floor:
            status = "partial"
            break
        cursor = page_info["endCursor"]

    next_state = PassState(cursor=None if status == "completed" else last_cursor)
    return PassResult(
        pr_rows=pr_rows,
        review_rows=review_rows,
        comment_rows=comment_rows,
        bot_prs_excluded=bot_prs,
        bot_reviews_excluded=bot_reviews,
        bot_comments_excluded=bot_comments,
        status=status,
        next_state=next_state,
        pages_fetched=pages,
        error=error,
    )


def run_closed_search_pass(
    collector: GitHubCollector,
    repo_label: str,
    state: PassState,
    window_start: date,
    bot_patterns: list[BotPattern],
    source_snapshot_id: str,
    page_size: int = SEARCH_PAGE_SIZE,
) -> PassResult:
    """Pass 3: `is:pr closed:>=<window_start>` via GitHub's search API
    (module docstring) -- PRs created before the window but closed inside
    it, which pass 1's created-date bound alone would miss entirely."""
    search_query = f"repo:{repo_label} is:pr closed:>={window_start.isoformat()}"
    cursor = state.cursor
    pr_rows: list[dict] = []
    review_rows: list[dict] = []
    comment_rows: list[dict] = []
    bot_prs = bot_reviews = bot_comments = 0
    pages = 0
    status = "completed"
    error: str | None = None
    last_cursor = cursor
    issue_count: int | None = None

    while True:
        try:
            data = collector.fetch_pr_search_page(search_query, cursor, page_size)
        except RateLimitExhausted as exc:
            status, error = "rate_limited", str(exc)
            break
        except CollectionError as exc:
            status, error = "failed", str(exc)
            break

        pages += 1
        rate_limit = data.get("rateLimit") or {}
        remaining = rate_limit.get("remaining")
        connection = data["search"]
        issue_count = connection.get("issueCount")

        p_rows, r_rows, c_rows, bp, br, bc = _normalize_page_nodes(
            connection["nodes"], repo_label, bot_patterns, source_snapshot_id
        )
        pr_rows.extend(p_rows)
        review_rows.extend(r_rows)
        comment_rows.extend(c_rows)
        bot_prs += bp
        bot_reviews += br
        bot_comments += bc

        page_info = connection["pageInfo"]
        last_cursor = page_info.get("endCursor") or last_cursor

        if not page_info["hasNextPage"]:
            status = "completed"
            break
        if remaining is not None and remaining <= collector.rate_limit_floor:
            status = "partial"
            break
        cursor = page_info["endCursor"]

    next_state = PassState(cursor=None if status == "completed" else last_cursor)
    return PassResult(
        pr_rows=pr_rows,
        review_rows=review_rows,
        comment_rows=comment_rows,
        bot_prs_excluded=bot_prs,
        bot_reviews_excluded=bot_reviews,
        bot_comments_excluded=bot_comments,
        status=status,
        next_state=next_state,
        pages_fetched=pages,
        issue_count=issue_count,
        error=error,
    )
