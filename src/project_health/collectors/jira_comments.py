"""JIRA-comment CI-evidence collector for `pre-commit-ci-evidence` (issue #36).

`governance-policy.yaml`'s `pre-commit-ci-evidence` rule's `check_method`
(`evidence_source: jira_comment_ci_mention`) requires searching an issue's
JIRA comments for any of a fixed set of CI-related terms
(`ci_summary`, `results_details`, `circleci`, `ci-cassandra.apache.org`,
`pre-ci.cassandra.apache.org`, `jenkins`, `butler`, `.build/run-ci`).

Scope is deliberately narrow (issue #36: "limited to what the
pre-commit-ci-evidence rule needs"): this collector fetches comments for a
caller-supplied list of issue keys (the keys actually referenced by the
commits being scored this run — never a blanket "all CASSANDRA issues"
crawl) and stores **comment metadata plus the matched CI URL only, never
comment bodies**. A comment's full text is read once, in memory, to search
for a term/URL match, and is never written to any returned object, log
line, or persisted table.

Politeness matches `collectors/jira.py`'s JIRA collector: ≤2 req/s
(`DEFAULT_MIN_REQUEST_INTERVAL = 0.5`), exponential backoff with jitter on
429/5xx, same anonymous unauthenticated ASF JIRA access
(`docs/spec/DATA-SOURCES.md` §2). This is a separate, smaller collector
class rather than an extension of `JiraCollector` — the two hit different
endpoints (`/issue/{key}/comment` vs. `/search`) for different purposes and
have no shared state.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx

# Per governance-policy.yaml `pre-commit-ci-evidence.check_method`
# (`evidence_source: jira_comment_ci_mention`) — kept as a code constant
# because the policy's `detail` field is prose, not a structured list; a
# term-list change here is itself a policy-scoring behavior change and
# should be reviewed like one.
CI_EVIDENCE_TERMS: tuple[str, ...] = (
    "ci_summary",
    "results_details",
    "circleci",
    "ci-cassandra.apache.org",
    "pre-ci.cassandra.apache.org",
    "jenkins",
    "butler",
    ".build/run-ci",
)

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_RETRIES = 5
# ≤2 req/s politeness cap (issue #36 scope, same bound as collectors/jira.py).
DEFAULT_MIN_REQUEST_INTERVAL = 0.5
DEFAULT_TIMEOUT = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 20.0

_URL_RE = re.compile(r"https?://\S+")


class CollectionError(Exception):
    """Raised when a JIRA comments request fails after exhausting retries."""


@dataclass(frozen=True)
class CommentCIEvidence:
    """One matched CI-evidence comment. Never carries the comment body —
    only metadata (id, author, created date) and the matched term/URL."""

    issue_key: str
    comment_id: str
    comment_author: str | None
    comment_created_at: str | None
    matched_term: str
    matched_url: str | None = None


def _find_ci_evidence(body: str, terms: tuple[str, ...]) -> tuple[str, str | None] | None:
    """Search `body` (read once, discarded by the caller) for the first
    matching term (case-insensitive substring). Returns `(term, url)` where
    `url` is the first `http(s)://` URL in the body that itself contains the
    matched term, or any URL in the body if none does, or `None` if the body
    has no URL at all. Returns `None` if no term matches."""
    lowered = body.lower()
    for term in terms:
        if term.lower() not in lowered:
            continue
        urls = _URL_RE.findall(body)
        term_url = next((u for u in urls if term.lower() in u.lower()), None)
        return term, term_url or (urls[0] if urls else None)
    return None


class JiraCommentsCollector:
    """Fetches comments for specific issue keys and extracts CI evidence.

    `base_url` is the JIRA instance root (e.g.
    `https://issues.apache.org/jira`, `config.issue_tracker.base_url`).
    """

    source_id = "jira_comments"

    def __init__(
        self,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
        terms: tuple[str, ...] = CI_EVIDENCE_TERMS,
    ) -> None:
        self._page_size = page_size
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._last_request_at: float | None = None
        self._terms = terms
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=timeout
        )
        # Every physical HTTP attempt (including retries) — a per-run API
        # budget (issue #36 fixup cycle 1, `pipeline.py`'s `_collect_governance`)
        # reads this to stop cleanly before hammering ASF JIRA, rather than
        # relying on a rate-limit error to find out after the fact.
        self.call_count = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JiraCommentsCollector":
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
            exp = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
            delay = exp + random.uniform(0, exp * 0.25)
        self._sleep_fn(delay)

    def _get_with_retry(self, path: str, params: dict) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            self._respect_rate_limit()
            self.call_count += 1
            try:
                response = self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"JIRA comments request to {path} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue

            if response.status_code == 404:
                return response  # issue not found / no comments — caller handles
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"JIRA comments request to {path} failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            return response

    def fetch_ci_evidence(self, issue_key: str) -> CommentCIEvidence | None:
        """The first CI-evidence match among `issue_key`'s comments (oldest
        first, JIRA's default comment order), or `None` if no comment
        matches any term in `self._terms` (or the issue has no comments /
        doesn't exist).
        """
        start_at = 0
        while True:
            response = self._get_with_retry(
                f"/rest/api/2/issue/{issue_key}/comment",
                {"startAt": start_at, "maxResults": self._page_size},
            )
            if response.status_code == 404:
                return None

            payload = response.json()
            comments = payload.get("comments", [])
            for comment in comments:
                body = comment.get("body") or ""
                match = _find_ci_evidence(body, self._terms)
                if match is not None:
                    term, url = match
                    author = (comment.get("author") or {}).get("name")
                    return CommentCIEvidence(
                        issue_key=issue_key,
                        comment_id=str(comment.get("id")),
                        comment_author=author,
                        comment_created_at=comment.get("created"),
                        matched_term=term,
                        matched_url=url,
                    )

            total = payload.get("total", len(comments))
            start_at += len(comments)
            if not comments or start_at >= total:
                return None

    def fetch_ci_evidence_for_issues(
        self, issue_keys: Iterable[str]
    ) -> dict[str, CommentCIEvidence]:
        """`issue_key -> CommentCIEvidence` for every key in `issue_keys`
        (de-duplicated) that has at least one matching comment."""
        evidence: dict[str, CommentCIEvidence] = {}
        for issue_key in dict.fromkeys(issue_keys):
            found = self.fetch_ci_evidence(issue_key)
            if found is not None:
                evidence[issue_key] = found
        return evidence
