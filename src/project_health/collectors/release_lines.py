"""Release-lines collector for the Releases tab (issue #166).

Three independent HTTP sources, fetched by one collector:

- **JIRA versions + per-version open issues** (`issue_tracker.base_url`/
  `.project_key`, reused from `JiraCollector`'s own config, D25/D22: no new
  JIRA endpoint, just `/rest/api/2/project/{key}/versions` and the existing
  `/rest/api/2/search`). Every version's `description` is quoted verbatim
  (never paraphrased) and is also the **only** signal used to classify a
  version as a blocker-ladder placeholder or a series backlog placeholder
  (see `classify_versions` below) — this project's own design rule is
  "derive the ladder from descriptions, not hard-coded names," so nothing
  here hard-codes e.g. `"6.0-beta1"`; the chain is read back out of each
  placeholder's own `"Placeholder for blockers to <X>"` text.
- **Butler trends** (`release_lines.butler_trends_url`) — per-workflow CI
  failure/test/duration history, re-joined per build from Butler's own
  three parallel point lists (`test_failures`/`test_runs`/`duration`,
  matched by Butler's own `extra` build-label field).
- **Jenkins job status** (`release_lines.jenkins_api_url`) — one job per
  branch line, `color` + last completed build, verbatim.

## Independent degrade-to-unavailable, per source (issue #166)

Each of the three sources above is fetched behind its own `try`/`except`
inside `collect()`. A failure in one (after its own retries) leaves that
source's table(s) **empty for this run only** and sets the matching
`*_ok` flag on `ReleaseLinesCollectionResult` to `False` — it never raises
out of `collect()`, so the other two sources' data for this same run is
never lost. Because `storage.write_partition`/`read_table` is an
append-only-raw, union-all-partitions store (same pattern as `release`/
`scorecard_check`), a prior run's partition for a now-unavailable source is
still there and still read; the site shows that source's block as
"unavailable" only when NO partition has ever been written for it (a
first-run outage), otherwise it keeps showing the last successfully
collected state (same `read_last_good_snapshot` spirit `_collect_release`/
`_collect_security` already use at the whole-source level, just applied per
sub-table here since one collector owns three independent sources).
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.schema import get_schema, validate

DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_RETRIES = 3
DEFAULT_MIN_REQUEST_INTERVAL = 0.5  # same ≤2 req/s politeness cap as JiraCollector
DEFAULT_TIMEOUT = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0

_ISSUE_FIELDS = "key,summary,status,priority,assignee,updated,created"

# --- Description-driven version classification -----------------------------

_BLOCKER_PLACEHOLDER_RE = re.compile(
    r"^Placeholder for blockers to\s+(?P<target>\S+)", re.IGNORECASE
)
_LINE_PREFIX_RE = re.compile(r"^(\d+\.\d+)")
_TARGET_STAGE_RE = re.compile(r"^(?P<line>\d+\.\d+)(?:-(?P<stage>[a-zA-Z]+)\d*)?")


class CollectionError(Exception):
    """Raised when a request fails after exhausting all retry attempts."""


@dataclass(frozen=True)
class JiraVersionInfo:
    version_id: str
    name: str
    description: str | None
    released: bool
    release_date: str | None  # ISO date string, JIRA's own, or None


@dataclass(frozen=True)
class LadderStep:
    """One ordered step in a release line's blocker ladder, derived purely
    from its placeholder version's own JIRA description (module docstring)."""

    version: JiraVersionInfo
    stage: str | None  # the stage word this placeholder's OWN name carries (e.g. "alpha"), if any
    points_to_stage: str | None  # the stage word its description's target names, or None (-> GA)
    # issue #166 orchestrator review of PR #167: a short, human label for
    # what this step's own open issues block -- e.g. "beta1", "rc1", "GA"
    # -- read straight out of the placeholder's own description (its
    # `_BLOCKER_PLACEHOLDER_RE` `target` group), never hard-coded. A target
    # with a `<line>-<stage><digits>` shape (e.g. "6.0-beta1") keeps just
    # the `<stage><digits>` suffix; any other shape (e.g. "6.0.0", "6.0.0
    # (GA)", "4.1.0") -- i.e. a bare/GA-style version -- becomes "GA".
    target_label: str


def classify_versions(
    versions: list[JiraVersionInfo], line: str
) -> dict[str, list[JiraVersionInfo] | JiraVersionInfo | None]:
    """Classify every JIRA version under `line` (e.g. "6.0") by its own
    `description` text and name shape -- never by a hard-coded version name.

    Returns a dict with keys:
      - "ladder_placeholders": unordered list of blocker-ladder placeholder
        versions (description starts with "Placeholder for blockers to ...")
      - "series_placeholder": the `<line>.x`-style series backlog version.
        Detected structurally (name == `line + ".x"` exactly) rather than
        by description text: live data (verified 2026-10-12) shows this
        project's own series-placeholder versions consistently follow that
        `<line>.x` naming across every line (2.1.x, 2.2.x, 3.0.x, 3.11.x,
        4.0.x, 4.1.x, 5.0.x, 6.x, 7.x) even though not every one currently
        carries a `"Placeholder for a ... series change"` description (e.g.
        "6.0.x" has none right now) -- the name shape is the more reliable
        signal here, and the description (when present) is still quoted
        verbatim on the site, never fabricated when absent.
      - "ga_target": the bare `<line>` version itself (no description, name
        exactly equals `line`), or None.
      - "next_versions": UNRELEASED, undescribed versions under `line`
        whose name carries a digit after the line prefix (e.g.
        "6.0-alpha3", "5.0.10") -- i.e. a concrete numbered pre-release or
        patch target, distinct from the bare GA target and from either
        placeholder shape.
      - "released_versions": the same name shape as `next_versions`, but
        RELEASED (`released=True`) -- e.g. "6.0-alpha1", "6.0-alpha2".
        Orchestrator review of PR #167: a line that hasn't reached GA yet
        has no row at all in the git-tag-derived `release` table
        (`collectors/release.py` deliberately excludes alpha/beta/rc tags,
        issue #135), so its own pre-GA release history would otherwise be
        invisible to the /releases/ page's timeline -- this key is JIRA's
        own record of exactly that history, filling the gap without
        touching `release`/`collectors/release.py` at all (`release_lines_
        page.py` unions this with `release`'s rows, disjoint by
        construction since one is pre-release-only and the other GA-only).
        Sorted oldest-first by `release_date`.
    """
    ladder_placeholders: list[JiraVersionInfo] = []
    series_placeholder: JiraVersionInfo | None = None
    ga_target: JiraVersionInfo | None = None
    next_versions: list[JiraVersionInfo] = []
    released_versions: list[JiraVersionInfo] = []

    for v in versions:
        if not v.name.startswith(line):
            continue
        remainder = v.name[len(line) :]
        desc = (v.description or "").strip()

        if remainder == ".x":
            series_placeholder = v
            continue
        if _BLOCKER_PLACEHOLDER_RE.match(desc):
            ladder_placeholders.append(v)
            continue
        if desc:
            # A described version that's neither ladder nor series shape --
            # not one of this function's recognized categories; skip rather
            # than guess.
            continue
        if remainder == "":
            ga_target = v
            continue
        if not any(c.isdigit() for c in remainder):
            continue
        if not v.released:
            next_versions.append(v)
        elif remainder.startswith("-"):
            # Pre-release-shaped (e.g. "6.0-alpha1", "4.1-beta1") -- the
            # line's own GA history (e.g. "4.0.21") is already tracked,
            # with its authoritative git-tag date, by `collectors/
            # release.py`'s `release` table; only the pre-release shape is
            # tracked here, so `released_versions` never duplicates that
            # table's rows (structurally disjoint: "-" vs "." after the
            # line prefix) while still filling the pre-GA gap git tags
            # can't see (module docstring).
            released_versions.append(v)

    return {
        "ladder_placeholders": ladder_placeholders,
        "series_placeholder": series_placeholder,
        "ga_target": ga_target,
        "next_versions": sorted(next_versions, key=lambda v: v.name),
        "released_versions": sorted(
            released_versions, key=lambda v: (v.release_date or "", v.name)
        ),
    }


def order_ladder(placeholders: list[JiraVersionInfo]) -> list[LadderStep]:
    """Order `placeholders` earliest-to-GA by following each one's own
    description chain (module docstring: "derive the ladder from
    descriptions, not hard-coded names").

    Each placeholder's description names a *target* version (e.g.
    `"...to 6.0-beta1..."` or `"...to 6.0.0 (GA)..."`); `_TARGET_STAGE_RE`
    extracts that target's own stage word (`"beta"`), which is matched
    against another placeholder's *own* name-stage (`"6.0-beta"` ->
    `"beta"`) to link them. The placeholder with no other placeholder
    pointing at its stage is the chain's start; a target with no matching
    placeholder (e.g. "6.0.0 (GA)") is the terminal, implicit GA step.

    Falls back to a plain alphabetical-by-name order (never raises) if the
    chain can't be fully resolved -- e.g. a future line with a differently
    worded description -- so the page still renders every open blocker,
    just without a guaranteed stage order in that edge case.
    """
    steps: dict[str, LadderStep] = {}
    stage_of: dict[str, str] = {}
    for v in placeholders:
        m = _TARGET_STAGE_RE.match(v.name)
        stage = m.group("stage") if m else None
        target_match = _BLOCKER_PLACEHOLDER_RE.match((v.description or "").strip())
        points_to_stage = None
        target_label = "GA"
        if target_match:
            target = target_match.group("target")
            tm = _TARGET_STAGE_RE.match(target)
            if tm:
                points_to_stage = tm.group("stage")  # None means the target is bare -> GA
                if tm.group("stage"):
                    # Keep just the "<stage><digits>" suffix, e.g. "6.0-beta1" -> "beta1".
                    target_label = target[len(tm.group("line")) + 1 :]
        steps[v.name] = LadderStep(
            version=v, stage=stage, points_to_stage=points_to_stage, target_label=target_label
        )
        if stage:
            stage_of[stage] = v.name

    # A stage that is some placeholder's own stage AND also named as a
    # target by another placeholder is not a chain start.
    pointed_to_stages = {s.points_to_stage for s in steps.values() if s.points_to_stage}
    starts = [name for name, s in steps.items() if s.stage not in pointed_to_stages]

    ordered: list[LadderStep] = []
    seen: set[str] = set()
    for start in sorted(starts):
        name = start
        while name and name not in seen:
            step = steps[name]
            ordered.append(step)
            seen.add(name)
            name = stage_of.get(step.points_to_stage) if step.points_to_stage else None

    # Anything not reached by chain-walking (an unresolved/disconnected
    # placeholder) still gets listed, alphabetically, rather than dropped.
    for name in sorted(steps):
        if name not in seen:
            ordered.append(steps[name])

    return ordered


def _major_minor_line(name: str) -> str | None:
    m = _LINE_PREFIX_RE.match(name)
    return m.group(1) if m else None


def _branch_line_from_job(name: str, job_prefix: str) -> str | None:
    if not name.startswith(job_prefix):
        return None
    return name[len(job_prefix) :]


def _exponential_backoff(attempt: int) -> float:
    import random

    exp = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


def _parse_jira_date(value: str | None):
    from datetime import date

    if not value:
        return None
    return date.fromisoformat(value[:10])


def _parse_jira_timestamp(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc)


def _ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


@dataclass(frozen=True)
class ReleaseLinesCollectionResult:
    """Output of one `ReleaseLinesCollector.collect()` run. Each `*_ok` flag
    is independent -- see module docstring's "Independent degrade-to-
    unavailable" section."""

    jira_version: pa.Table
    release_blocker_issue: pa.Table
    ci_job_status: pa.Table
    ci_trend_point: pa.Table
    jira_ok: bool
    butler_ok: bool
    jenkins_ok: bool
    jira_version_count: int = 0
    blocker_issue_count: int = 0
    ci_job_count: int = 0
    ci_trend_point_count: int = 0
    errors: dict[str, str] = field(default_factory=dict)


class ReleaseLinesCollector:
    """Releases-tab collector: JIRA fixVersions/blockers + Butler + Jenkins
    (issue #166). See module docstring."""

    source_id = "release_lines"

    def __init__(
        self,
        config: ProjectConfig,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        issue_tracker = config.issue_tracker
        base_url = getattr(issue_tracker, "base_url", None) if issue_tracker else None
        project_key = getattr(issue_tracker, "project_key", None) if issue_tracker else None
        if not base_url or not project_key:
            raise ValueError(
                "config.issue_tracker.base_url and .project_key are required for "
                "ReleaseLinesCollector"
            )
        self._base_url = base_url.rstrip("/")
        self._project_key = project_key
        # issue #181: same `config.issue_tracker.jql_filter` AND-clause
        # `collectors/jira.py`'s `JiraCollector` applies -- a subproject
        # sharing another's `project_key` (accord, ccm) must never have
        # `fetch_unresolved_issues`'s fixVersion search span the WHOLE
        # shared project. `None` (every project verified so far except
        # accord/ccm) omits the clause entirely, unchanged from before.
        self._jql_filter = getattr(issue_tracker, "jql_filter", None)

        release_lines_cfg = getattr(config, "release_lines", None)
        self._active_lines: list[str] = list(
            getattr(release_lines_cfg, "active_lines", None)
            or ["6.0", "5.0", "4.1", "4.0", "3.11", "3.0"]
        )
        self._butler_url = getattr(release_lines_cfg, "butler_trends_url", None) or (
            "https://butler.cassandra.apache.org/api/upstream/trends"
        )
        self._jenkins_url = getattr(release_lines_cfg, "jenkins_api_url", None) or (
            "https://ci-cassandra.apache.org/api/json"
        )
        self._job_prefix = "Cassandra-"

        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._sleep_fn = sleep_fn
        self._last_request_at: float | None = None
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ReleaseLinesCollector":
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

    def _get_with_retry(self, url: str, params: dict | None = None) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            self._respect_rate_limit()
            try:
                response = self._client.get(url, params=params)
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

    # --- JIRA -----------------------------------------------------------

    def fetch_jira_versions(self) -> list[JiraVersionInfo]:
        url = f"{self._base_url}/rest/api/2/project/{self._project_key}/versions"
        response = self._get_with_retry(url)
        out = []
        for raw in response.json():
            out.append(
                JiraVersionInfo(
                    version_id=str(raw["id"]),
                    name=raw["name"],
                    description=raw.get("description"),
                    released=bool(raw.get("released", False)),
                    release_date=raw.get("releaseDate"),
                )
            )
        return out

    def fetch_unresolved_issues(self, version_name: str) -> list[dict]:
        url = f"{self._base_url}/rest/api/2/search"
        jql = f'project={self._project_key} AND fixVersion="{version_name}"'
        if self._jql_filter:
            jql += f" AND {self._jql_filter}"
        jql += " AND resolution=Unresolved ORDER BY key ASC"
        start_at = 0
        issues: list[dict] = []
        while True:
            params = {
                "jql": jql,
                "fields": _ISSUE_FIELDS,
                "startAt": start_at,
                "maxResults": DEFAULT_PAGE_SIZE,
            }
            response = self._get_with_retry(url, params)
            payload = response.json()
            page = payload.get("issues", [])
            issues.extend(page)
            if len(page) < DEFAULT_PAGE_SIZE:
                return issues
            start_at += len(page)

    def tracked_versions_for_line(
        self, versions: list[JiraVersionInfo], line: str
    ) -> list[tuple[JiraVersionInfo, str]]:
        """`[(version, label)]` for every version under `line` this
        collector tracks: every ladder placeholder (labelled by its chain
        position, `order_ladder`), the series backlog placeholder
        (labelled "series"), the bare GA target (labelled "ga_target"),
        every unreleased next-version candidate (labelled "next"), and
        every released pre-release version (labelled "released", issue
        #166 orchestrator review of PR #167 -- see `classify_versions`'
        `released_versions` docstring). `collect()` fetches open issues
        for every label except "released" (historical, no open-issue
        count needed -- see its own comment)."""
        classified = classify_versions(versions, line)
        tracked: list[tuple[JiraVersionInfo, str]] = []
        ladder = order_ladder(classified["ladder_placeholders"])  # type: ignore[arg-type]
        for i, step in enumerate(ladder):
            tracked.append((step.version, f"ladder:{i}:{step.stage or 'unknown'}"))
        if classified["series_placeholder"] is not None:
            tracked.append((classified["series_placeholder"], "series"))  # type: ignore[arg-type]
        if classified["ga_target"] is not None:
            tracked.append((classified["ga_target"], "ga_target"))  # type: ignore[arg-type]
        for v in classified["next_versions"]:  # type: ignore[union-attr]
            tracked.append((v, "next"))
        for v in classified["released_versions"]:  # type: ignore[union-attr]
            tracked.append((v, "released"))
        return tracked

    # --- Butler -----------------------------------------------------------

    def fetch_butler_trends(self) -> list[dict]:
        response = self._get_with_retry(self._butler_url)
        payload = response.json()
        return payload.get("data", [])

    # --- Jenkins ------------------------------------------------------------

    def fetch_jenkins_jobs(self) -> list[dict]:
        params = {"tree": "jobs[name,color,lastCompletedBuild[number,result,timestamp]]"}
        response = self._get_with_retry(self._jenkins_url, params)
        payload = response.json()
        return payload.get("jobs", [])

    # --- Orchestration ----------------------------------------------------

    def collect(self, snapshot_id: str | None = None) -> ReleaseLinesCollectionResult:
        snapshot_id = snapshot_id or str(uuid.uuid4())
        collected_at = datetime.now(timezone.utc)
        errors: dict[str, str] = {}

        # --- JIRA (versions + per-version unresolved issues) --------------
        jira_version_rows: list[dict[str, Any]] = []
        blocker_issue_rows: list[dict[str, Any]] = []
        jira_ok = True
        try:
            all_versions = self.fetch_jira_versions()
            tracked_names: set[str] = set()
            lines_for_version: dict[str, str] = {}
            # issue #166 orchestrator review of PR #167: a "released"-
            # labelled version (historical pre-release, `classify_versions`'
            # `released_versions`) is tracked for its `jira_version` row
            # (the /releases/ timeline) only -- it has already shipped, so
            # there is no reason to spend an API call checking it for open
            # issues the way a still-open ladder/series/next version needs.
            needs_issues: dict[str, bool] = {}
            for line in self._active_lines:
                for version, label in self.tracked_versions_for_line(all_versions, line):
                    tracked_names.add(version.name)
                    lines_for_version[version.name] = line
                    needs_issues[version.name] = needs_issues.get(
                        version.name, False
                    ) or label != "released"

            for v in all_versions:
                if v.name not in tracked_names:
                    continue
                jira_version_rows.append(
                    {
                        "version_id": v.version_id,
                        "name": v.name,
                        "description": v.description,
                        "released": v.released,
                        "release_date": _parse_jira_date(v.release_date),
                        "major_minor": lines_for_version.get(v.name) or _major_minor_line(v.name),
                        "source_snapshot_id": snapshot_id,
                        "collected_at": collected_at,
                    }
                )
                if not needs_issues.get(v.name, True):
                    continue
                for raw_issue in self.fetch_unresolved_issues(v.name):
                    fields = raw_issue["fields"]
                    status = fields.get("status") or {}
                    priority = fields.get("priority") or {}
                    assignee = fields.get("assignee") or {}
                    blocker_issue_rows.append(
                        {
                            "version_name": v.name,
                            "issue_key": raw_issue["key"],
                            "summary": fields.get("summary"),
                            "status": status.get("name"),
                            "priority": priority.get("name"),
                            "assignee_raw": assignee.get("name"),
                            "created_at": _parse_jira_timestamp(fields.get("created")),
                            "updated_at": _parse_jira_timestamp(fields.get("updated")),
                            "source_snapshot_id": snapshot_id,
                            "collected_at": collected_at,
                        }
                    )
        except Exception as exc:  # noqa: BLE001 - one source's outage must never take the others down
            jira_ok = False
            errors["jira"] = str(exc)
            jira_version_rows = []
            blocker_issue_rows = []

        # --- Butler -------------------------------------------------------
        ci_trend_rows: list[dict[str, Any]] = []
        butler_ok = True
        try:
            for workflow_entry in self.fetch_butler_trends():
                workflow = workflow_entry.get("workflow") or workflow_entry.get("version")
                if not workflow:
                    continue
                branch_line = _branch_line_from_job(workflow, self._job_prefix) or workflow
                data = workflow_entry.get("data") or {}
                failures_by_label = {
                    p["extra"]: p for p in (data.get("test_failures") or {}).get("points", [])
                }
                runs_by_label = {
                    p["extra"]: p for p in (data.get("test_runs") or {}).get("points", [])
                }
                duration_by_label = {
                    p["extra"]: p for p in (data.get("duration") or {}).get("points", [])
                }
                labels = set(failures_by_label) | set(runs_by_label) | set(duration_by_label)
                for label in labels:
                    point = (
                        failures_by_label.get(label)
                        or runs_by_label.get(label)
                        or duration_by_label.get(label)
                    )
                    ts_ms = point["x"]
                    ci_trend_rows.append(
                        {
                            "workflow": workflow,
                            "branch_line": branch_line,
                            "build_label": label,
                            "build_timestamp": _ms_to_utc(ts_ms),
                            "test_failures": (failures_by_label.get(label) or {}).get("y"),
                            "test_runs": (runs_by_label.get(label) or {}).get("y"),
                            "duration_minutes": (duration_by_label.get(label) or {}).get("y"),
                            "source_snapshot_id": snapshot_id,
                            "collected_at": collected_at,
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            butler_ok = False
            errors["butler"] = str(exc)
            ci_trend_rows = []

        # --- Jenkins --------------------------------------------------------
        ci_job_rows: list[dict[str, Any]] = []
        jenkins_ok = True
        try:
            tracked_job_names = {f"{self._job_prefix}{line}" for line in self._active_lines}
            tracked_job_names.add(f"{self._job_prefix}trunk")
            for job in self.fetch_jenkins_jobs():
                name = job.get("name")
                if name not in tracked_job_names:
                    continue
                branch_line = _branch_line_from_job(name, self._job_prefix) or name
                last_build = job.get("lastCompletedBuild") or {}
                timestamp = last_build.get("timestamp")
                ci_job_rows.append(
                    {
                        "job_name": name,
                        "branch_line": branch_line,
                        "color": job.get("color"),
                        "last_build_number": last_build.get("number"),
                        "last_build_result": last_build.get("result"),
                        "last_build_timestamp": _ms_to_utc(timestamp) if timestamp else None,
                        "source_snapshot_id": snapshot_id,
                        "collected_at": collected_at,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            jenkins_ok = False
            errors["jenkins"] = str(exc)
            ci_job_rows = []

        jira_version_table = validate(
            "jira_version", _rows_to_table(jira_version_rows, get_schema("jira_version"))
        )
        blocker_issue_table = validate(
            "release_blocker_issue",
            _rows_to_table(blocker_issue_rows, get_schema("release_blocker_issue")),
        )
        ci_job_table = validate(
            "ci_job_status", _rows_to_table(ci_job_rows, get_schema("ci_job_status"))
        )
        ci_trend_table = validate(
            "ci_trend_point", _rows_to_table(ci_trend_rows, get_schema("ci_trend_point"))
        )

        return ReleaseLinesCollectionResult(
            jira_version=jira_version_table,
            release_blocker_issue=blocker_issue_table,
            ci_job_status=ci_job_table,
            ci_trend_point=ci_trend_table,
            jira_ok=jira_ok,
            butler_ok=butler_ok,
            jenkins_ok=jenkins_ok,
            jira_version_count=len(jira_version_rows),
            blocker_issue_count=len(blocker_issue_rows),
            ci_job_count=len(ci_job_rows),
            ci_trend_point_count=len(ci_trend_rows),
            errors=errors,
        )
