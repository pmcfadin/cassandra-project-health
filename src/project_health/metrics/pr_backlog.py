"""Open PR backlog: monthly reconstructed composition of open GitHub PRs by
age, linked-JIRA-ticket state, and GitHub response status (issue #142).

Answers, from data, "what does the open-PR backlog actually look like, and
how is it composed" -- age distribution, whether a linked ticket is itself
still open/resolved, and whether a PR has had any visible GitHub response at
all. Neutral facts only (D25): no thresholds, no "healthy"/"should"/
"cleanup" framing, no verdict.

## Deliberately outside `metrics/registry.py`'s `METRIC_IDS` and composite scoring

Same architectural choice METRICS.md §9 (`governance/fact_metrics.py`) and
§10 (`metrics/review_responsiveness.py`) make for their own descriptive
metrics, for the same reason: this module computes plain backlog-composition
counts/shares, not a rate this project judges "higher/lower is better" with
a `role`/`key`/`supporting` classification. It has its own
`DEFINITION_VERSION`/registry (`build_pr_backlog_registry` below), its own
snapshot file (`pipeline.py`'s `_write_pr_backlog_snapshot`), and its own
`metrics_meta.PR_BACKLOG_METRICS` entries for the site -- never wired into
`metrics/engine.py::compute_all` or `scoring/registry.py`.

## Monthly reconstruction

Every row is a reconstructed **snapshot as of a given completed month's
end** `T`, not a rate computed *over* that month's new activity: a PR is
"open as of `T`" when `pr.created_at <= T` and (`pr.closed_at` is null or
`pr.closed_at > T`) -- the only reconstruction GitHub's own data supports,
since M0 has no PR state-history table, only each PR's current
`closed_at`/`merged_at`. Dense across every completed month from the first
month any PR was created through the last completed month before the run's
`as_of` date (`metrics/engine.py::_dense_months`, the same helper every
other dense M0 metric uses).

- **Age buckets** (`<30d` / `30-90d` / `90d-1y` / `1-3y` / `>3y`), by
  `T - pr.created_at`.
- **Linked-ticket state** (`ticket_open` / `ticket_fixed` /
  `ticket_closed_other` / `no_ticket_key`): the ticket key(s) named by a PR
  are `pr.linked_issue_keys` (issue #102) unioned with `pr_issue_link`
  (issue #105's historical backfill for a PR collected before that column
  existed) -- same union `review_responsiveness.py::_build_submissions`
  uses. A PR naming no key is `no_ticket_key`. For a PR naming one or more
  keys, each key's state is read from the `issue` table (already deduped to
  one row per key by `pipeline._dedupe_issue_rows`'s "latest `updated_at`"
  rule before it ever reaches this module) and gated on `resolved_at <= T`:
  a key resolved after `T`, or never resolved, contributes `ticket_open`;
  one resolved at or before `T` with `resolution == 'Fixed'` contributes
  `ticket_fixed`; any other resolution at or before `T` contributes
  `ticket_closed_other`. When a PR names more than one key (rare) and they
  disagree, `ticket_fixed` wins over `ticket_closed_other` wins over
  `ticket_open` -- a disclosed, deterministic priority, not a claim about
  which key is "the" ticket.
  - **KNOWN LIMITATION (disclosed, not hidden):** `issue.resolution`/
    `resolved_at` are each issue's *current* snapshot, not a historical
    changelog entry -- a ticket that was `Fixed` and later reopened (rare,
    but not impossible) would misclassify an earlier month's bucket the
    same way `review_responsiveness.py`'s own "no historical reopen
    tracking" simplification does. `jira_changelog` (issue #102) carries
    exactly this history for `status`/`assignee`, but reconstructing
    historical resolution state from it is out of this issue's scope (no
    new collection, per the issue's own "Build" note) -- a reasonable
    follow-up, not done here.
- **No-GitHub-response share**: among open-as-of-`T` PRs, the share with no
  `pr_review`/`pr_comment` from someone other than the PR's own author,
  timestamped at or before `T`. Labelled "GitHub only" everywhere it's
  rendered (`details_json.label`) -- a PR's actual review can happen on its
  linked JIRA ticket instead, which this project's GitHub-only signal
  cannot see (same caveat `stale_pr_rate`/`pr_merge_lead_time` already
  carry for every GitHub-PR-only M0 metric, DECISIONS.md "Cassandra-specific
  facts"). `pr_review`/`pr_comment` rows are already bot-filtered at
  collection time (`collectors/github.py::_is_bot_login`), same convention
  `dev_metrics.py`/`review_responsiveness.py` document for GitHub-side
  data -- no `bot_identifier` join needed here.

## Base-branch split: not available (disclosed gap, no new collection)

The issue's own owner snapshot includes a base-branch split (trunk vs.
release branches vs. other), but `schema/tables.py`'s `PR` table has no
base-ref column, and `collectors/github.py`'s GraphQL query never requests
`baseRefName` -- there is no raw data, cached or not, this module could
reconstruct that split from without a new collector field (out of scope per
the issue's own "Build (from existing raw data; no new collection)" note).
This module emits no base-branch metric at all rather than fabricate one;
`site/pr_backlog_page.py` and `docs/spec/METRICS.md` §12 both say so
explicitly, as a fact about collector coverage, not a verdict.

## Headcounts: no sample-size floor

`open_pr_backlog_total`, `_drafts`, the five age-bucket counts, and the
four ticket-state counts are plain headcounts over the current backlog --
like `active_contributors_monthly`/`new_contributors_monthly`/
`unique_reviewers_monthly` (METRICS.md §0.6's own headcount exemption),
a raw count is already the complete, meaningful statistic at any n,
including 0, so they always report `flag='ok'`. Only
`open_pr_backlog_no_github_response_share`, a share, applies METRICS.md
§0.6's rate/ratio floor (5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import duckdb
import pyarrow as pa

from project_health.metrics.engine import _connect, _dense_months
from project_health.metrics.windows import month_end, month_start
from project_health.schema import get_schema, validate

DEFINITION_VERSION = "1.0"

TOTAL = "open_pr_backlog_total"
DRAFTS = "open_pr_backlog_drafts"

AGE_LT_30D = "open_pr_backlog_age_lt_30d"
AGE_30_90D = "open_pr_backlog_age_30_90d"
AGE_90D_1Y = "open_pr_backlog_age_90d_1y"
AGE_1_3Y = "open_pr_backlog_age_1_3y"
AGE_GT_3Y = "open_pr_backlog_age_gt_3y"
AGE_METRIC_IDS: tuple[str, ...] = (AGE_LT_30D, AGE_30_90D, AGE_90D_1Y, AGE_1_3Y, AGE_GT_3Y)

TICKET_OPEN = "open_pr_backlog_ticket_open"
TICKET_FIXED = "open_pr_backlog_ticket_fixed"
TICKET_CLOSED_OTHER = "open_pr_backlog_ticket_closed_other"
NO_TICKET_KEY = "open_pr_backlog_no_ticket_key"
TICKET_METRIC_IDS: tuple[str, ...] = (TICKET_OPEN, TICKET_FIXED, TICKET_CLOSED_OTHER, NO_TICKET_KEY)

NO_GITHUB_RESPONSE_SHARE = "open_pr_backlog_no_github_response_share"
NO_GITHUB_RESPONSE_LABEL = "github_only_review_may_occur_in_jira"

ALL_METRIC_IDS: tuple[str, ...] = (
    (TOTAL, DRAFTS) + AGE_METRIC_IDS + TICKET_METRIC_IDS + (NO_GITHUB_RESPONSE_SHARE,)
)

# METRICS.md §0.6 default rate/ratio floor -- the only ratio metric this
# module emits (every other metric here is a headcount, exempted per the
# module docstring).
FLOOR_RATE_RATIO = 5

_AGE_LT30D_DAYS = 30
_AGE_30_90D_DAYS = 90
_AGE_90D_1Y_DAYS = 365
_AGE_1_3Y_DAYS = 365 * 3


def _age_bucket(age_days: float) -> str:
    if age_days < _AGE_LT30D_DAYS:
        return AGE_LT_30D
    if age_days < _AGE_30_90D_DAYS:
        return AGE_30_90D
    if age_days < _AGE_90D_1Y_DAYS:
        return AGE_90D_1Y
    if age_days < _AGE_1_3Y_DAYS:
        return AGE_1_3Y
    return AGE_GT_3Y


def _end_of_day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _PR:
    repo: str
    number: int
    author: str | None
    is_draft: bool
    created_at: datetime
    closed_at: datetime | None
    ticket_keys: frozenset[str]
    first_non_author_response_at: datetime | None


def _build_prs(con: duckdb.DuckDBPyConnection) -> list[_PR]:
    pr_rows = con.execute(
        """
        SELECT repo, number, author_raw_value, is_draft, created_at, closed_at, linked_issue_keys
        FROM pr
        """
    ).fetchall()
    if not pr_rows:
        return []

    # issue #105: `pr_issue_link`'s (repo, number, issue_key) rows backfill
    # the ticket key(s) a PR collected *before* `pr.linked_issue_keys`
    # (issue #102) existed -- unioned per-PR below, same union
    # `review_responsiveness.py::_build_submissions` performs.
    backfilled_keys_by_pr: dict[tuple[str, int], set[str]] = {}
    for repo, number, issue_key in con.execute(
        "SELECT repo, number, issue_key FROM pr_issue_link"
    ).fetchall():
        backfilled_keys_by_pr.setdefault((repo, number), set()).add(issue_key)

    reviews_by_pr: dict[tuple[str, int], list[tuple[datetime, str]]] = {}
    for repo, number, submitted_at, reviewer in con.execute(
        """
        SELECT repo, pr_number, submitted_at, reviewer_raw_value
        FROM pr_review
        WHERE submitted_at IS NOT NULL AND reviewer_raw_value IS NOT NULL
        """
    ).fetchall():
        reviews_by_pr.setdefault((repo, number), []).append((submitted_at, reviewer))

    comments_by_pr: dict[tuple[str, int], list[tuple[datetime, str]]] = {}
    for repo, number, created_at, author in con.execute(
        """
        SELECT repo, pr_number, created_at, author_raw_value
        FROM pr_comment
        WHERE author_raw_value IS NOT NULL
        """
    ).fetchall():
        comments_by_pr.setdefault((repo, number), []).append((created_at, author))

    prs: list[_PR] = []
    for repo, number, author, is_draft, created_at, closed_at, linked_issue_keys in pr_rows:
        pr_key = (repo, number)
        ticket_keys = frozenset(linked_issue_keys or []) | frozenset(
            backfilled_keys_by_pr.get(pr_key, set())
        )
        events: list[datetime] = []
        for r_time, r_author in reviews_by_pr.get(pr_key, []):
            if r_author != author:
                events.append(r_time)
        for c_time, c_author in comments_by_pr.get(pr_key, []):
            if c_author != author:
                events.append(c_time)
        prs.append(
            _PR(
                repo=repo,
                number=number,
                author=author,
                is_draft=bool(is_draft),
                created_at=created_at,
                closed_at=closed_at,
                ticket_keys=ticket_keys,
                first_non_author_response_at=min(events) if events else None,
            )
        )
    return prs


def _issue_state_by_key(
    con: duckdb.DuckDBPyConnection,
) -> dict[str, tuple[str | None, datetime | None]]:
    return {
        issue_key: (resolution, resolved_at)
        for issue_key, resolution, resolved_at in con.execute(
            "SELECT issue_key, resolution, resolved_at FROM issue"
        ).fetchall()
    }


def _ticket_state(
    ticket_keys: frozenset[str],
    issue_state: dict[str, tuple[str | None, datetime | None]],
    as_of_dt: datetime,
) -> str:
    if not ticket_keys:
        return NO_TICKET_KEY
    saw_fixed = False
    saw_closed_other = False
    for key in ticket_keys:
        info = issue_state.get(key)
        if info is None:
            continue
        resolution, resolved_at = info
        if resolved_at is not None and resolved_at <= as_of_dt:
            if resolution == "Fixed":
                saw_fixed = True
            else:
                saw_closed_other = True
    if saw_fixed:
        return TICKET_FIXED
    if saw_closed_other:
        return TICKET_CLOSED_OTHER
    return TICKET_OPEN


def _row(
    *,
    metric_id_: str,
    window_start: date,
    window_end: date,
    raw_value: float | None,
    n: int,
    floor: int,
    run_id: str,
    computed_at: datetime,
    details: dict[str, Any],
) -> dict[str, Any]:
    if raw_value is None or n < floor:
        value = None
        flag = "insufficient_data"
    else:
        value = float(raw_value)
        flag = "ok"
    return {
        "metric_id": metric_id_,
        "definition_version": DEFINITION_VERSION,
        "window_start": window_start,
        "window_end": window_end,
        "value": value,
        "n": n,
        "flag": flag,
        "run_id": run_id,
        "computed_at": computed_at,
        "details_json": json.dumps(details, sort_keys=True, default=str) if details else None,
    }


def compute_pr_backlog(
    tables: dict[str, pa.Table],
    *,
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> pa.Table:
    """Every `open_pr_backlog_*` `metric_value` row for this run (issue #142).

    Kept as its own entry point (not wired into `metrics/engine.py::
    compute_all`) -- see module docstring. Callers (`pipeline.py`) write
    this table to its own snapshot file, mirroring
    `review_responsiveness_metric_value.parquet`'s treatment, not the main
    `metrics.parquet`/`METRIC_IDS`-checked path.
    """
    con = _connect(tables)
    try:
        prs = _build_prs(con)
        issue_state = _issue_state_by_key(con)
    finally:
        con.close()

    if not prs:
        return get_schema("metric_value").empty_table()

    first_month = month_start(min(pr.created_at.date() for pr in prs))
    months = _dense_months(first_month, as_of)

    rows: list[dict[str, Any]] = []
    for month in months:
        window_end = month_end(month)
        as_of_dt = _end_of_day(window_end)
        open_prs = [
            pr
            for pr in prs
            if pr.created_at <= as_of_dt and (pr.closed_at is None or pr.closed_at > as_of_dt)
        ]
        n_open = len(open_prs)
        n_drafts = sum(1 for pr in open_prs if pr.is_draft)

        age_counts: dict[str, int] = {mid: 0 for mid in AGE_METRIC_IDS}
        ticket_counts: dict[str, int] = {mid: 0 for mid in TICKET_METRIC_IDS}
        n_no_response = 0
        for pr in open_prs:
            age_days = (as_of_dt - pr.created_at).total_seconds() / 86400.0
            age_counts[_age_bucket(age_days)] += 1
            ticket_counts[_ticket_state(pr.ticket_keys, issue_state, as_of_dt)] += 1
            response_at = pr.first_non_author_response_at
            if response_at is None or response_at > as_of_dt:
                n_no_response += 1

        rows.append(
            _row(
                metric_id_=TOTAL,
                window_start=month,
                window_end=window_end,
                raw_value=float(n_open),
                n=n_open,
                floor=0,
                run_id=run_id,
                computed_at=computed_at,
                details={},
            )
        )
        rows.append(
            _row(
                metric_id_=DRAFTS,
                window_start=month,
                window_end=window_end,
                raw_value=float(n_drafts),
                n=n_open,
                floor=0,
                run_id=run_id,
                computed_at=computed_at,
                details={"n_open": n_open},
            )
        )
        for mid in AGE_METRIC_IDS:
            rows.append(
                _row(
                    metric_id_=mid,
                    window_start=month,
                    window_end=window_end,
                    raw_value=float(age_counts[mid]),
                    n=n_open,
                    floor=0,
                    run_id=run_id,
                    computed_at=computed_at,
                    details={"n_open": n_open},
                )
            )
        for mid in TICKET_METRIC_IDS:
            rows.append(
                _row(
                    metric_id_=mid,
                    window_start=month,
                    window_end=window_end,
                    raw_value=float(ticket_counts[mid]),
                    n=n_open,
                    floor=0,
                    run_id=run_id,
                    computed_at=computed_at,
                    details={"n_open": n_open},
                )
            )
        rows.append(
            _row(
                metric_id_=NO_GITHUB_RESPONSE_SHARE,
                window_start=month,
                window_end=window_end,
                raw_value=(n_no_response / n_open) if n_open else None,
                n=n_open,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "n_hit": n_no_response,
                    "n_denominator": n_open,
                    "label": NO_GITHUB_RESPONSE_LABEL,
                },
            )
        )

    schema = get_schema("metric_value")
    return validate("metric_value", pa.Table.from_pylist(rows, schema=schema))


# --- Registry (`metric_definition_version` rows, mirrors
# `governance/registry.py::build_governance_fact_metrics_registry`) --------

_CHANGELOG_NOTE = (
    "Initial implementation (issue #142): a monthly reconstructed snapshot of the open PR "
    "backlog's composition -- never a rate computed over new activity -- by age bucket, "
    "linked-JIRA-ticket state, and GitHub-visible response status. Descriptive only "
    "(direction_of_good is null in metrics_meta.py): this project does not judge backlog "
    "composition good or bad."
)

_DESCRIPTIONS: dict[str, str] = {
    TOTAL: (
        "Count of GitHub PRs open as of each completed month's end T (pr.created_at <= T and "
        "(pr.closed_at is null or pr.closed_at > T)). Headcount, no sample-size floor."
    ),
    DRAFTS: (
        "Of the open-as-of-T backlog, the subset with pr.is_draft = true. Headcount, no "
        "sample-size floor. details_json.n_open carries the same-month total for context."
    ),
    AGE_LT_30D: "Open-as-of-T backlog: PRs less than 30 days old at T. Headcount, no floor.",
    AGE_30_90D: "Open-as-of-T backlog: PRs 30-90 days old at T. Headcount, no floor.",
    AGE_90D_1Y: "Open-as-of-T backlog: PRs 90 days to 1 year old at T. Headcount, no floor.",
    AGE_1_3Y: "Open-as-of-T backlog: PRs 1-3 years old at T. Headcount, no floor.",
    AGE_GT_3Y: "Open-as-of-T backlog: PRs more than 3 years old at T. Headcount, no floor.",
    TICKET_OPEN: (
        "Open-as-of-T backlog: PRs naming a JIRA ticket (pr.linked_issue_keys union "
        "pr_issue_link) whose state at T is unresolved. Headcount, no floor."
    ),
    TICKET_FIXED: (
        "Open-as-of-T backlog: PRs naming a JIRA ticket resolved Fixed at or before T. "
        "Headcount, no floor."
    ),
    TICKET_CLOSED_OTHER: (
        "Open-as-of-T backlog: PRs naming a JIRA ticket resolved (not Fixed) at or before T. "
        "Headcount, no floor."
    ),
    NO_TICKET_KEY: (
        "Open-as-of-T backlog: PRs naming no JIRA ticket key at all. Headcount, no floor."
    ),
    NO_GITHUB_RESPONSE_SHARE: (
        "Share of the open-as-of-T backlog with no pr_review/pr_comment from someone other "
        "than the PR's own author, at or before T. GitHub-visible only -- review may have "
        "happened on the linked JIRA ticket instead. METRICS.md §0.6 rate/ratio floor (5) "
        "applies."
    ),
}


def build_pr_backlog_registry(changed_at: datetime) -> pa.Table:
    """One `metric_definition_version` row per `ALL_METRIC_IDS` entry, all at
    `DEFINITION_VERSION` ("1.0")."""
    rows = [
        {
            "metric_id": metric_id,
            "version": DEFINITION_VERSION,
            "description": _DESCRIPTIONS[metric_id],
            "changed_at": changed_at,
            "changelog_note": _CHANGELOG_NOTE,
        }
        for metric_id in ALL_METRIC_IDS
    ]
    schema = get_schema("metric_definition_version")
    return validate("metric_definition_version", pa.Table.from_pylist(rows, schema=schema))
