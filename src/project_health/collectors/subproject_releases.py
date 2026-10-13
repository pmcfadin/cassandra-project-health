"""Releases-tab collector for a non-Cassandra subproject (issue #175).

Cassandra's own `/releases/` page (`collectors/release_lines.py`, issue
#166) assumes two things that are specific to Cassandra itself: a JIRA
blocker-ladder placeholder convention (`"Placeholder for blockers to
6.0-beta1"`) and Jenkins/Butler as the project's CI systems. Neither holds
for a subproject -- verified live 2026-10-12:

- Every unreleased JIRA version under CASSJAVA/CASSGO/CASSPYTHON/CASSCPP/
  CASSNODEJS/CASSSIDECAR/CASSANALYTICS has `description: null` -- none
  follow Cassandra's own ladder-placeholder naming/description scheme, so
  there is no ladder to derive here. A subproject's unreleased versions are
  tracked flat: name, open/total ticket count, a link to JIRA's own search.
- A subproject's CI, where it has any, is GitHub Actions (not Jenkins/
  Butler) -- and several have NONE at all (java-driver, python-driver,
  cpp-driver, accord all have zero runs, on their default branch, under a
  repo-committed `.github/workflows/*.yml` path; what they do have is only
  GitHub's own auto-generated `dynamic/...`-path pseudo-workflows --
  Copilot PR review, Dependabot, pages-build-deployment -- which this
  collector deliberately excludes, see `_REAL_WORKFLOW_PATH_PREFIX` below).

Two independent sources, same "degrade to unavailable per source, never
raise out of `collect()`" discipline `ReleaseLinesCollector` already uses
(module docstring there) -- a JIRA outage must never take GitHub Actions
down for the same run, and vice versa.

GA release *history* itself is NOT collected here -- it's already the
`release` table (git tags, issue #135, `collectors/release.py`), read
as-is by `site/subproject_releases_page.py`. This collector only adds the
two things that table doesn't have: unreleased JIRA versions, and CI state.
"""

from __future__ import annotations

import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pyarrow as pa

from project_health.collectors.github import resolve_github_token
from project_health.collectors.github_checks import GITHUB_API_BASE
from project_health.collectors.release_lines import ReleaseLinesCollector
from project_health.collectors.retry import exponential_backoff, is_transient_body_error
from project_health.config import ProjectConfig
from project_health.schema import get_schema, validate

DEFAULT_MAX_RETRIES = 3
DEFAULT_MIN_REQUEST_INTERVAL = 0.3
DEFAULT_TIMEOUT = 30.0
# How many of a repo's most recent runs on its default branch to scan for
# the latest-per-workflow grouping (issue #175's own "latest ... run ...
# per workflow name" -- a page this size comfortably covers every
# distinct workflow for every subproject verified live, including the
# noisiest, nodejs-driver's Dependabot-interleaved history).
DEFAULT_RUNS_PAGE_SIZE = 100

# A run's own `path` field distinguishes a repo-committed workflow file
# from one of GitHub's auto-generated pseudo-workflows (`dynamic/...` --
# Copilot PR review, Dependabot Updates, pages-build-deployment,
# Dependency Graph) -- verified live 2026-10-12 against every subproject
# repo (module docstring). Only the former is "this project's own CI."
_REAL_WORKFLOW_PATH_PREFIX = ".github/workflows/"


class CollectionError(Exception):
    """Raised when a request fails after exhausting all retry attempts."""


@dataclass(frozen=True)
class UnreleasedVersionInfo:
    """One unreleased JIRA fixVersion with at least one ticket (open or
    resolved) filed against it -- a subproject's flat (no ladder)
    equivalent of `release_lines.JiraVersionInfo` plus its own open/total
    counts and a ready-to-link JIRA search URL."""

    version_id: str
    name: str
    description: str | None
    release_date: str | None
    major_minor: str | None
    open_count: int
    total_count: int
    issues_url: str


@dataclass(frozen=True)
class WorkflowRunInfo:
    workflow_name: str
    workflow_path: str
    status: str | None
    conclusion: str | None
    run_number: int | None
    html_url: str
    run_started_at: datetime | None


@dataclass(frozen=True)
class SubprojectReleaseCollectionResult:
    jira_version: pa.Table
    ci_workflow_run: pa.Table
    jira_ok: bool
    actions_ok: bool
    jira_version_count: int = 0
    ci_workflow_run_count: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def _major_minor_line(name: str) -> str | None:
    import re

    m = re.match(r"^(\d+\.\d+)", name)
    return m.group(1) if m else None


def _jira_issues_url(
    base_url: str, project_key: str, version_name: str, jql_filter: str | None
) -> str:
    jql = f'project={project_key} AND fixVersion="{version_name}"'
    if jql_filter:
        jql += f" AND {jql_filter}"
    return f"{base_url}/issues/?jql={urllib.parse.quote(jql)}"


def _exponential_backoff(attempt: int) -> float:
    return exponential_backoff(attempt)


class SubprojectReleasesCollector:
    """Releases-tab collector for a non-Cassandra subproject (issue #175).
    See module docstring."""

    source_id = "subproject_releases"

    def __init__(
        self,
        config: ProjectConfig,
        transport: httpx.BaseTransport | None = None,
        github_token: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
        runs_page_size: int = DEFAULT_RUNS_PAGE_SIZE,
    ) -> None:
        self._config = config
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._sleep_fn = sleep_fn
        self._runs_page_size = runs_page_size
        self._last_request_at: float | None = None

        # JIRA half -- reuses `ReleaseLinesCollector`'s own fetch methods
        # (module docstring: "reuse release_lines collector's JIRA versions
        # part") for a project that HAS a JIRA `issue_tracker:` section
        # (easy-stress has none -- `self._jira` stays `None`, same "no
        # section -> source doesn't apply" discipline every other
        # `_SOURCE_CONFIG_ATTR`-gated source already uses). Its constructor
        # makes no network call of its own (only sets up URLs/a client), so
        # constructing it here never touches Jenkins/Butler.
        self._jira: ReleaseLinesCollector | None = None
        if config.issue_tracker is not None and getattr(config.issue_tracker, "base_url", None):
            self._jira = ReleaseLinesCollector(
                config,
                transport=transport,
                max_retries=max_retries,
                min_request_interval=min_request_interval,
                sleep_fn=sleep_fn,
                timeout=timeout,
            )

        # GitHub Actions half -- same auth/retry shape as
        # `collectors/github_checks.py`'s `GitHubChecksCollector`.
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        resolved_token = github_token or resolve_github_token()
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"
        self._github_client = httpx.Client(
            base_url=GITHUB_API_BASE, headers=headers, transport=transport, timeout=timeout
        )

    def close(self) -> None:
        if self._jira is not None:
            self._jira.close()
        self._github_client.close()

    def __enter__(self) -> "SubprojectReleasesCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _respect_rate_limit(self) -> None:
        if self._min_request_interval <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            wait = self._min_request_interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
        self._last_request_at = time.monotonic()

    def _github_get_with_retry(self, path: str, params: dict) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            self._respect_rate_limit()
            try:
                response = self._github_client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub Actions request to {path} failed after {attempt} "
                        f"attempt(s): {exc}"
                    ) from exc
                self._sleep_fn(_exponential_backoff(attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub Actions request to {path} failed after {attempt} "
                        f"attempt(s): HTTP {response.status_code}"
                    )
                self._sleep_fn(_exponential_backoff(attempt))
                continue
            if (
                response.status_code == 403
                and response.headers.get("X-RateLimit-Remaining") == "0"
            ):
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub API rate limit exhausted fetching {path}"
                    )
                self._sleep_fn(_exponential_backoff(attempt))
                continue

            response.raise_for_status()
            try:
                response.json()
            except Exception as exc:
                if not is_transient_body_error(exc):
                    raise
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"GitHub Actions request to {path} returned an undecodable "
                        f"response body after {attempt} attempt(s): {exc}"
                    ) from exc
                self._sleep_fn(_exponential_backoff(attempt))
                continue
            return response

    # --- JIRA -------------------------------------------------------------

    def fetch_unreleased_versions(self) -> list[UnreleasedVersionInfo]:
        """Every unreleased, non-archived JIRA fixVersion that has at least
        one ticket (open or resolved) filed against it (module docstring:
        "only show unreleased versions that have tickets"). `[]` if this
        project has no `issue_tracker:` section at all (easy-stress)."""
        if self._jira is None:
            return []
        issue_tracker = self._config.issue_tracker
        base_url = issue_tracker.base_url.rstrip("/")  # type: ignore[union-attr]
        project_key = issue_tracker.project_key  # type: ignore[union-attr]
        jql_filter = getattr(issue_tracker, "jql_filter", None)

        out: list[UnreleasedVersionInfo] = []
        for v in self._jira.fetch_jira_versions():
            if v.released or v.archived:
                continue
            open_count = self._jira.fetch_version_issue_count(v.name, unresolved_only=True)
            total_count = self._jira.fetch_version_issue_count(v.name, unresolved_only=False)
            if total_count == 0:
                continue
            out.append(
                UnreleasedVersionInfo(
                    version_id=v.version_id,
                    name=v.name,
                    description=v.description,
                    release_date=v.release_date,
                    major_minor=_major_minor_line(v.name),
                    open_count=open_count,
                    total_count=total_count,
                    issues_url=_jira_issues_url(base_url, project_key, v.name, jql_filter),
                )
            )
        return out

    # --- GitHub Actions -----------------------------------------------------

    def fetch_latest_workflow_runs(
        self, owner: str, repo: str, branch: str
    ) -> list[WorkflowRunInfo]:
        """The most recent run, per distinct `workflow_name`, among this
        repo's `DEFAULT_RUNS_PAGE_SIZE` most recent runs on `branch` --
        excluding any run whose own `path` isn't a repo-committed
        `.github/workflows/*` file (module docstring: GitHub's own
        auto-generated pseudo-workflows are never "this project's CI").
        `[]` when there are none at all (`actions_ok` stays `True` -- a
        real, verified fact, not a fetch failure; see module docstring's
        java-driver/python-driver/cpp-driver/accord examples)."""
        response = self._github_get_with_retry(
            f"/repos/{owner}/{repo}/actions/runs",
            {"branch": branch, "per_page": self._runs_page_size},
        )
        payload = response.json()
        seen: dict[str, WorkflowRunInfo] = {}
        for run in payload.get("workflow_runs", []):
            path = run.get("path") or ""
            if not path.startswith(_REAL_WORKFLOW_PATH_PREFIX):
                continue
            name = run.get("name") or path
            if name in seen:
                # GitHub returns runs newest-first; the first one seen per
                # name is already the latest.
                continue
            started_at = run.get("run_started_at") or run.get("created_at")
            seen[name] = WorkflowRunInfo(
                workflow_name=name,
                workflow_path=path,
                status=run.get("status"),
                conclusion=run.get("conclusion"),
                run_number=run.get("run_number"),
                html_url=run.get("html_url") or "",
                run_started_at=(
                    datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    if started_at
                    else None
                ),
            )
        return list(seen.values())

    # --- Orchestration ----------------------------------------------------

    def collect(self, snapshot_id: str | None = None) -> SubprojectReleaseCollectionResult:
        snapshot_id = snapshot_id or str(uuid.uuid4())
        collected_at = datetime.now(timezone.utc)
        errors: dict[str, str] = {}

        jira_rows: list[dict[str, Any]] = []
        jira_ok = True
        try:
            for v in self.fetch_unreleased_versions():
                jira_rows.append(
                    {
                        "version_id": v.version_id,
                        "name": v.name,
                        "description": v.description,
                        "release_date": (
                            _parse_jira_date(v.release_date) if v.release_date else None
                        ),
                        "major_minor": v.major_minor,
                        "open_count": v.open_count,
                        "total_count": v.total_count,
                        "issues_url": v.issues_url,
                        "source_snapshot_id": snapshot_id,
                        "collected_at": collected_at,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - one source's outage must never take the other down
            jira_ok = False
            errors["jira"] = str(exc)
            jira_rows = []

        ci_rows: list[dict[str, Any]] = []
        actions_ok = True
        try:
            for repo_cfg in self._config.repos:
                for run in self.fetch_latest_workflow_runs(
                    repo_cfg.owner, repo_cfg.name, repo_cfg.default_branch
                ):
                    ci_rows.append(
                        {
                            "repo": f"{repo_cfg.owner}/{repo_cfg.name}",
                            "workflow_name": run.workflow_name,
                            "workflow_path": run.workflow_path,
                            "status": run.status,
                            "conclusion": run.conclusion,
                            "run_number": run.run_number,
                            "html_url": run.html_url,
                            "run_started_at": run.run_started_at,
                            "source_snapshot_id": snapshot_id,
                            "collected_at": collected_at,
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            actions_ok = False
            errors["actions"] = str(exc)
            ci_rows = []

        jira_table = validate(
            "subproject_release_version",
            _rows_to_table(jira_rows, get_schema("subproject_release_version")),
        )
        ci_table = validate(
            "ci_workflow_run", _rows_to_table(ci_rows, get_schema("ci_workflow_run"))
        )

        return SubprojectReleaseCollectionResult(
            jira_version=jira_table,
            ci_workflow_run=ci_table,
            jira_ok=jira_ok,
            actions_ok=actions_ok,
            jira_version_count=len(jira_rows),
            ci_workflow_run_count=len(ci_rows),
            errors=errors,
        )


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


def _parse_jira_date(value: str):
    from datetime import date

    return date.fromisoformat(value[:10])
