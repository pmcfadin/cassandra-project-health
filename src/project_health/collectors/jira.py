"""ASF JIRA collector (ARCHITECTURE.md §2.2 `IssueTrackerAdapter`, M0 subset).

M0 scope is issues only — no comments/changelog yet (those extend this module
without changing its shape, per ARCHITECTURE.md §2.2's Protocol split).

Emits two normalized tables (ARCHITECTURE.md §3, `schema/tables.py`):

- ``issue`` — one row per fetched issue, with ``reporter_raw``/``assignee_raw``
  carrying the raw JIRA username (``fields.reporter.name`` /
  ``fields.assignee.name``) and ``*_identity_id`` left ``null`` for identity
  resolution (#6) to fill in later.
- ``review_event`` — one row per reviewer named in either of the two
  configured JIRA custom fields (``reviewer_extraction.jira_fields`` in
  ``projects/<id>.yaml`` — field ids are never hard-coded here), with
  ``source='jira_field'``, ``reviewer_raw_type='jira_username'``,
  ``reviewer_raw_value`` = the user's JIRA ``name``, and
  ``reviewer_identity_id`` left ``null``.

Both tables are returned already validated against their declared schema
(``project_health.schema.validate``).

## Watermark strategy — why it isn't a naive `max(updated)`

Two verified JIRA REST quirks (see issue #5 / `docs/spec/data-probe.md`)
shape how the watermark is built and consumed:

1. ``/rest/api/2/search`` truncates ``fields.updated`` to whole seconds
   (``"...45.000+0000"`` — the millisecond digits are always ``.000``),
   while ``/rest/api/2/issue/{key}`` returns true milliseconds. So a
   watermark derived from search results can't carry sub-second precision
   even if we wanted it to.
2. JQL's ``updated >= "<literal>"`` comparison only has **minute**
   precision — the literal format JIRA accepts is ``yyyy-MM-dd HH:mm``, with
   no seconds field at all.

Combined, this means a watermark built from `max(fields.updated)` and fed
straight back into `updated >= "<watermark>"` can silently miss an issue
that was updated in the same minute as the watermark, a moment after this
run's snapshot was taken — JQL literally cannot express "after 21:38:41",
only "at or after 21:38". The fix used here: `next_watermark` stores the
full-precision `max(updated)` seen (§`JiraCollectionResult.next_watermark`),
but `build_jql` subtracts `WATERMARK_SAFETY_MARGIN` *and* truncates to the
minute before embedding it in JQL — so the next run's query window
deliberately overlaps the previous one rather than trying to land exactly on
a boundary a minute-granularity filter can't express. This means a run can
re-fetch a handful of issues it already saw; the fix for that is not a
smarter watermark, it's a downstream dedupe on `issue_key` (natural key,
idempotent upsert — ARCHITECTURE.md §11 "Idempotency"), which is cheap and
exact where a tighter watermark heuristic would only be approximate.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.schema import get_schema, validate

# --- Tuning constants (issue #5) --------------------------------------------

DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_RETRIES = 5
# ≤2 req/s politeness cap (issue #5 / DATA-SOURCES.md §2: "page politely... no
# parallel fan-out"); expressed as a minimum interval between request starts.
DEFAULT_MIN_REQUEST_INTERVAL = 0.5
DEFAULT_TIMEOUT = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 20.0

# See module docstring "Watermark strategy" above.
WATERMARK_SAFETY_MARGIN = timedelta(minutes=2)
_JQL_DATETIME_FORMAT = "%Y-%m-%d %H:%M"  # JQL's minute-precision literal format

_STANDARD_FIELDS = (
    "summary",
    "status",
    "created",
    "updated",
    "resolutiondate",
    "reporter",
    "assignee",
    "priority",
    "issuetype",
)


class CollectionError(Exception):
    """Raised when a JIRA request fails after exhausting all retry attempts."""


@dataclass(frozen=True)
class JiraCollectionResult:
    """Output of one `JiraCollector.collect()` run."""

    issues: pa.Table
    review_events: pa.Table
    # Full-precision ISO 8601 string of max(updated) seen this run, or the
    # input watermark unchanged if no issues were fetched. `None` only when
    # there was no prior watermark and nothing was fetched. Callers persist
    # this via `project_health.storage.write_watermark` and pass it back in
    # as `watermark` on the next run; `build_jql` (not this value) is where
    # the safety margin gets applied.
    next_watermark: str | None
    issue_count: int
    review_event_count: int


# --- Timestamp / JQL helpers -------------------------------------------------


def _parse_jira_timestamp(value: str) -> datetime:
    """Parse a JIRA REST timestamp (`2026-09-25T21:38:41.000+0000`) to aware UTC.

    Works for both the always-`.000` seconds-truncated form `/search`
    returns and the true-millisecond form `/issue/{key}` returns — both use
    the same `%Y-%m-%dT%H:%M:%S.%f%z` layout, just with different precision
    in the fractional-second digits.
    """
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc)


def build_jql(project_key: str, watermark: str | None) -> str:
    """Build the issue-search JQL for `project_key`.

    `watermark` is a stored ISO 8601 timestamp string (a prior run's
    `JiraCollectionResult.next_watermark`); `None` on a first run omits the
    `updated >=` clause entirely, per issue #5. When present, the watermark
    is reduced by `WATERMARK_SAFETY_MARGIN` and truncated to minute
    precision before being embedded — see the module docstring's "Watermark
    strategy" section for why.
    """
    jql = f"project={project_key}"
    if watermark:
        dt = datetime.fromisoformat(watermark)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        safe = (dt - WATERMARK_SAFETY_MARGIN).astimezone(timezone.utc)
        jql += f' AND updated >= "{safe.strftime(_JQL_DATETIME_FORMAT)}"'
    jql += " ORDER BY updated ASC"
    return jql


def _exponential_backoff(attempt: int) -> float:
    """Exponential backoff with jitter, capped at `_BACKOFF_CAP` seconds."""
    exp = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


# --- Row normalization --------------------------------------------------


def _status_category(status_field: dict | None) -> str | None:
    if not status_field:
        return None
    category = status_field.get("statusCategory") or {}
    return category.get("name")


def _normalize_issue(raw: dict, source_snapshot_id: str) -> dict:
    """Build one `issue` table row (schema/tables.py `ISSUE`) from a raw
    `/rest/api/2/search` issue object."""
    fields = raw["fields"]
    reporter = fields.get("reporter") or {}
    assignee = fields.get("assignee") or {}
    status = fields.get("status") or {}
    priority = fields.get("priority") or {}
    issuetype = fields.get("issuetype") or {}

    resolutiondate = fields.get("resolutiondate")

    return {
        "issue_key": raw["key"],
        "summary": fields.get("summary"),
        "status": status.get("name"),
        "status_category": _status_category(status),
        "priority": priority.get("name"),
        "issue_type": issuetype.get("name"),
        "created_at": _parse_jira_timestamp(fields["created"]),
        "updated_at": _parse_jira_timestamp(fields["updated"]),
        "resolved_at": _parse_jira_timestamp(resolutiondate) if resolutiondate else None,
        "reporter_identity_id": None,
        "reporter_raw": reporter.get("name"),
        "assignee_identity_id": None,
        "assignee_raw": assignee.get("name"),
        "source_snapshot_id": source_snapshot_id,
    }


def _field_users(fields: dict, field_id: str | None) -> list[dict]:
    """Return the list of JIRA user objects named in `field_id` (which may be
    a single-user or multi-user custom field, or unset/null/absent)."""
    if not field_id:
        return []
    value = fields.get(field_id)
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _normalize_review_events(
    raw: dict,
    reviewers_field: str | None,
    reviewer_field: str | None,
    source_snapshot_id: str,
) -> list[dict]:
    """Build `review_event` rows (schema/tables.py `REVIEW_EVENT`) for one
    issue, one row per distinct reviewer named across the two configured
    JIRA fields.

    A reviewer named in both fields for the same issue is emitted once, with
    `evidence` recording that both fields named them (issue #5 requirement).
    Timestamp = `resolutiondate` if set, else `updated` (issue #5).
    """
    fields = raw["fields"]
    issue_key = raw["key"]

    resolutiondate = fields.get("resolutiondate")
    occurred_at = (
        _parse_jira_timestamp(resolutiondate)
        if resolutiondate
        else _parse_jira_timestamp(fields["updated"])
    )

    # username -> ordered list of field ids that named them, preserving the
    # order fields/users were encountered so evidence text is deterministic.
    named_in: dict[str, list[str]] = {}
    order: list[str] = []
    for field_id in (reviewers_field, reviewer_field):
        for user in _field_users(fields, field_id):
            username = user.get("name")
            if not username:
                continue
            if username not in named_in:
                named_in[username] = []
                order.append(username)
            if field_id not in named_in[username]:
                named_in[username].append(field_id)

    rows = []
    for username in order:
        field_ids = named_in[username]
        if len(field_ids) > 1:
            evidence = (
                f"jira_username {username!r} named in fields {', '.join(field_ids)} "
                f"for {issue_key}; deduped to one review_event row"
            )
        else:
            evidence = f"jira_username {username!r} named in field {field_ids[0]} for {issue_key}"
        rows.append(
            {
                "event_id": str(uuid.uuid4()),
                "source": "jira_field",
                "reviewer_identity_id": None,
                "reviewer_raw_type": "jira_username",
                "reviewer_raw_value": username,
                "author_identity_id": None,
                "author_raw_type": None,
                "author_raw_value": None,
                "issue_key": issue_key,
                "repo": None,
                "occurred_at": occurred_at,
                "evidence": evidence,
                "source_snapshot_id": source_snapshot_id,
            }
        )
    return rows


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


# --- Collector ----------------------------------------------------------


class JiraCollector:
    """ASF JIRA collector — M0 subset of `IssueTrackerAdapter` (issues only).

    Reads `base_url`/`project_key` from `config.issue_tracker` and the
    reviewer custom-field ids from `config.reviewer_extraction.jira_fields`
    (never hard-coded, per issue #5). Anonymous, unauthenticated access
    (DATA-SOURCES.md §2).
    """

    source_id = "jira"

    def __init__(
        self,
        config: ProjectConfig,
        transport: httpx.BaseTransport | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        issue_tracker = config.issue_tracker
        base_url = getattr(issue_tracker, "base_url", None) if issue_tracker else None
        project_key = getattr(issue_tracker, "project_key", None) if issue_tracker else None
        if not base_url or not project_key:
            raise ValueError(
                "config.issue_tracker.base_url and .project_key are required for "
                "JiraCollector"
            )

        self._project_key = project_key
        jira_fields = config.reviewer_extraction.jira_fields
        self._reviewers_field = jira_fields.reviewers_field
        self._reviewer_field = jira_fields.reviewer_field

        self._page_size = page_size
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._last_request_at: float | None = None

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=timeout
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JiraCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _fields_param(self) -> str:
        fields = list(_STANDARD_FIELDS)
        for extra in (self._reviewers_field, self._reviewer_field):
            if extra and extra not in fields:
                fields.append(extra)
        return ",".join(fields)

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

    def _get_with_retry(self, path: str, params: dict) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            self._respect_rate_limit()
            try:
                response = self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"JIRA request to {path} timed out after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"JIRA request to {path} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"JIRA request to {path} failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            return response

    def fetch_issues(
        self, watermark: str | None = None, max_issues: int | None = None
    ) -> Iterator[dict]:
        """Yield raw `/rest/api/2/search` issue objects, paginating
        `startAt`/`maxResults` until a short (or empty) page signals the end.

        `max_issues`, when set, stops iteration early (used by the live
        smoke check, issue #5, to stay at or under 200 issues) without
        requesting a page beyond what's needed.
        """
        jql = build_jql(self._project_key, watermark)
        fields_param = self._fields_param()
        start_at = 0
        fetched = 0

        while True:
            params = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": self._page_size,
                "fields": fields_param,
            }
            response = self._get_with_retry("/rest/api/2/search", params)
            payload = response.json()
            issues = payload.get("issues", [])

            for issue in issues:
                yield issue
                fetched += 1
                if max_issues is not None and fetched >= max_issues:
                    return

            if len(issues) < self._page_size:
                return
            start_at += len(issues)

    def collect(
        self,
        watermark: str | None = None,
        snapshot_id: str | None = None,
        max_issues: int | None = None,
    ) -> JiraCollectionResult:
        """Fetch issues (and their reviewer fields) and return validated
        `issue` / `review_event` tables plus the next watermark.

        `snapshot_id` is stamped into every row's `source_snapshot_id`
        (ARCHITECTURE.md §3, §5); callers that already have a `run_id`
        typically pass a snapshot id tied to it. A random one is generated
        if omitted, so this method is usable standalone (e.g. the live smoke
        check).
        """
        snapshot_id = snapshot_id or str(uuid.uuid4())
        issue_rows: list[dict] = []
        review_rows: list[dict] = []
        max_updated: datetime | None = None

        for raw_issue in self.fetch_issues(watermark=watermark, max_issues=max_issues):
            issue_row = _normalize_issue(raw_issue, snapshot_id)
            issue_rows.append(issue_row)
            updated_at = issue_row["updated_at"]
            if max_updated is None or updated_at > max_updated:
                max_updated = updated_at

            review_rows.extend(
                _normalize_review_events(
                    raw_issue, self._reviewers_field, self._reviewer_field, snapshot_id
                )
            )

        issues_table = validate("issue", _rows_to_table(issue_rows, get_schema("issue")))
        review_table = validate(
            "review_event", _rows_to_table(review_rows, get_schema("review_event"))
        )

        next_watermark = max_updated.isoformat() if max_updated is not None else watermark

        return JiraCollectionResult(
            issues=issues_table,
            review_events=review_table,
            next_watermark=next_watermark,
            issue_count=len(issue_rows),
            review_event_count=len(review_rows),
        )
