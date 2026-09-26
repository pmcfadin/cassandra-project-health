"""GitHub check-runs collector for `code-style-checkstyle` (issue #36).

`governance-policy.yaml`'s `code-style-checkstyle` rule's `check_method` is
`GET /repos/{owner}/{repo}/commits/{sha}/check-runs`, filtered to the two
GitHub Actions runs `.github/workflows/code-check.yaml` defines
(`ant-check-jdk11`, `ant-check-jdk17`) — the checkstyle-enforcing surface for
`cassandra-4.1`+ / `trunk`. The same endpoint is also the *documented, but
explicitly not used for scoring* evidence source the policy lists under
`pre-commit-ci-evidence` ("this only surfaces the GitHub Actions
licence/checkstyle workflow ... NOT the pre-commit Jenkins pipeline this
rule actually requires") — `checks.py`'s `score_pre_commit_ci_evidence`
deliberately never calls this collector, only `score_code_style_checkstyle`
does.

Uses a GitHub token from the environment (`GITHUB_TOKEN`, else `GH_TOKEN`)
if set, per issue #36's instruction — unauthenticated access works too
(GitHub's Checks API is public for a public repo) but is rate-limited far
more tightly (60 req/hr vs. 5,000 authenticated).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx

from project_health.collectors.retry import exponential_backoff, is_transient_body_error
from project_health.governance.checks import CheckstyleEvidence

GITHUB_API_BASE = "https://api.github.com"

# `.github/workflows/code-check.yaml` (docs/spec/GOVERNANCE.md §2 R5) — the
# two checkstyle-enforcing GitHub Actions check-run names.
CHECKSTYLE_RUN_NAMES = frozenset({"ant-check-jdk11", "ant-check-jdk17"})

DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_RETRIES = 5
DEFAULT_MIN_REQUEST_INTERVAL = 0.25
DEFAULT_TIMEOUT = 30.0


class CollectionError(Exception):
    """Raised when a GitHub check-runs request fails after exhausting retries."""


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None
    html_url: str | None


def _github_token(explicit: str | None) -> str | None:
    return explicit or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


class GitHubChecksCollector:
    """Fetches GitHub Checks API results for specific commit shas."""

    source_id = "github_checks"

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._page_size = page_size
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._last_request_at: float | None = None

        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        resolved_token = _github_token(token)
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"
        self._authenticated = resolved_token is not None
        self._client = httpx.Client(
            base_url=GITHUB_API_BASE, headers=headers, transport=transport, timeout=timeout
        )
        # Every physical HTTP attempt (including retries) — a per-run API
        # budget (issue #36 fixup cycle 1, `pipeline.py`'s `_collect_governance`)
        # reads this to stop cleanly before exhausting the shared GitHub
        # Actions token's ~1,000 req/hr, rather than relying on a 403 to find
        # out after the fact.
        self.call_count = 0

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubChecksCollector":
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
                        f"GitHub check-runs request to {path} failed after {attempt} "
                        f"attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue

            # A sha unreachable from any ref, or otherwise unknown to the
            # Checks API, is "no evidence" (unknown), not a hard failure —
            # very old / patch-file-based commits routinely 404/422 here.
            if response.status_code in (404, 422):
                return response
            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                reset_at = response.headers.get("X-RateLimit-Reset")
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub API rate limit exhausted fetching {path} "
                        f"(resets at epoch {reset_at})"
                    )
                self._backoff(attempt, retry_after=None)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub check-runs request to {path} failed after {attempt} "
                        f"attempt(s): HTTP {response.status_code}"
                    )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            try:
                response.json()
            except Exception as exc:
                if not is_transient_body_error(exc):
                    raise
                # issue #86: a truncated/undecodable check-runs body is
                # retried exactly like a 5xx.
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub check-runs request to {path} returned an undecodable "
                        f"response body after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue
            return response

    def fetch_check_runs(self, sha: str) -> tuple[CheckRun, ...]:
        """Every check-run recorded against `sha`, any name — pagination
        followed until a short page. Returns `()` for a sha the Checks API
        has no record of at all (404/422), never raises for that case."""
        page = 1
        runs: list[CheckRun] = []
        while True:
            response = self._get_with_retry(
                f"/repos/{self._owner}/{self._repo}/commits/{sha}/check-runs",
                {"per_page": self._page_size, "page": page},
            )
            if response.status_code in (404, 422):
                return tuple(runs)

            payload = response.json()
            page_runs = payload.get("check_runs", [])
            for run in page_runs:
                runs.append(
                    CheckRun(
                        name=run.get("name", ""),
                        status=run.get("status", ""),
                        conclusion=run.get("conclusion"),
                        html_url=run.get("html_url"),
                    )
                )
            if len(page_runs) < self._page_size:
                return tuple(runs)
            page += 1

    def fetch_checkstyle_evidence(self, sha: str) -> tuple[CheckstyleEvidence, ...]:
        """Just the checkstyle-relevant check-runs for `sha`
        (`CHECKSTYLE_RUN_NAMES`), shaped for `governance/checks.py`'s
        `score_code_style_checkstyle`."""
        runs = self.fetch_check_runs(sha)
        return tuple(
            CheckstyleEvidence(
                sha=sha, check_run_name=r.name, conclusion=r.conclusion, html_url=r.html_url
            )
            for r in runs
            if r.name in CHECKSTYLE_RUN_NAMES
        )

    def fetch_checkstyle_evidence_for_shas(
        self, shas: Iterable[str]
    ) -> dict[str, tuple[CheckstyleEvidence, ...]]:
        """`sha -> checkstyle check-runs`, for every sha in `shas` that has
        at least one (a sha with none is simply absent from the returned
        dict — callers treat a missing key the same as an empty tuple)."""
        result: dict[str, tuple[CheckstyleEvidence, ...]] = {}
        for sha in dict.fromkeys(shas):
            runs = self.fetch_checkstyle_evidence(sha)
            if runs:
                result[sha] = runs
        return result
