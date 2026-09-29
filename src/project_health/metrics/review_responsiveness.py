"""Review responsiveness: blended JIRA+GitHub time-to-first-review by
contributor tier (issue #102).

Answers, from data, the question the Community page's "Review
responsiveness" section poses: do new contributors have a hard time getting
their patches reviewed, and is it getting worse? Neutral facts only (D25) --
no thresholds, no verdicts.

## Deliberately outside `metrics/registry.py`'s `METRIC_IDS` and composite scoring

This module does **not** wire into `metrics/engine.py::compute_all`,
`metrics/registry.py::METRIC_IDS`, or `scoring/registry.py`'s scored-metric
list -- the same architectural choice METRICS.md §9 documents for
`governance/fact_metrics.py`'s five descriptive trend metrics, for the same
reason: composite/dimension scoring should not silently absorb a metric this
uncertain. Concretely, three caveats make these metrics **not composite-
score-ready today**, by design (owner decision, issue #102, default "no,
explain" per the issue's own instructions):

1. **Coverage is incomplete and actively changing.** Author tiers only mean
   what they claim ("this author's Nth-ever Patch-Available submission")
   once the `jira_changelog` Patch-Available backfill
   (`pipeline._collect_jira_patch_available_backfill`) has walked the full
   ~11.7k-issue history; until then, a submission's tier is computed only
   from whatever slice of history the backfill has reached so far, and early
   runs will materially undercount an author's true prior submission count
   (biasing everyone toward `first`/`tier_2_5`). A metric a composite score
   depends on should not change meaning run-over-run purely because a
   backfill made more progress -- `metric_coverage_share`/`n_issues_covered`
   in every row's `details_json` and the site's own coverage note (see
   `site/review_responsiveness_page.py`) make this visible instead of
   hiding it behind a score.
2. **`patch_author` is a proxy, not a verified identity.** METRICS.md's
   general population rules (§0.5) never silently merge identities below a
   confidence threshold; this module goes further and doesn't even attempt
   identity resolution for the JIRA-username-shaped `patch_author` it
   reconstructs from changelog history -- it's the best available signal for
   "whose patch is this," not a confirmed identity, and a composite score
   should not rest on an unconfirmed identity signal.
3. **Small per-year, per-tier `n`.** The issue's own acceptance data shows
   `first`-tier submissions running ~30-50/year -- comfortably above
   METRICS.md §0.6's floor (5) for a single reading, but thin enough that a
   composite score built on top would swing on a handful of submissions in
   either direction, which is exactly the kind of over-precision D2 rule 5
   warns against.

If/when the backfill completes and stabilizes (item 1) across a couple of
full runs, promoting a subset of these (most plausibly
`review_response_within_30d_share` for the `first` tier, the dimension's
most direct "are newcomers being left waiting" read) to `scoring/registry.py`
is a reasonable follow-up -- tracked as a possible future issue, not done
here (no scope creep).

## Submission unit and author-tier reconstruction

A "patch submission" is one JIRA issue that ever entered `Patch Available`
(`jira_changelog`, `field='status'`, `to_value='Patch Available'`), with any
GitHub PRs whose title names the ticket attached -- `pr.linked_issue_keys`
(issue #102) unioned, per PR, with `pr_issue_link` (issue #105: the
historical backfill for a PR collected before `linked_issue_keys` existed;
see `collectors/github.py`'s "`pr_issue_link` historical backfill" section
and `schema/tables.py`'s `PR_ISSUE_LINK` docstring).

- `submitted_at` = `min(first Patch-Available transition, earliest linked PR
  created_at)`.
- `patch_author` = the JIRA assignee at the moment of that first transition,
  reconstructed from `jira_changelog`'s `field='assignee'` history: the
  `from_value` of the first assignee change *after* the transition (i.e. who
  the assignee was immediately before that change), falling back to the
  issue's *current* `assignee_raw` (`issue` table) when no such later
  assignee change exists, falling back to whoever performed the
  Patch-Available transition itself (`actor_raw_value`) when the issue has
  never had an assignee at all.
- **Author tier** at submission: the number of *earlier* submissions by the
  same `patch_author`, counted over the entire reconstructed history sorted
  by `submitted_at` -- `first` (0 earlier), `tier_2_5` (1-4 earlier),
  `tier_6plus` (5+ earlier). Tier `proxy` (METRICS.md §0.1): a defensible
  stand-in for "how experienced is this contributor," not a verified
  identity-linked contribution count.

## Blended first-visible-response

The earliest qualifying event at or after `submitted_at`, from someone other
than the patch author (and, for a GitHub event, other than that PR's own
author or any other linked PR's author on the same issue) and not a bot:

- a JIRA comment (`issue_comment`, `jira_username` bot-pattern excluded via
  `bot_identifier`, same join `dev_metrics.py::_time_to_first_response_jira`
  uses);
- a JIRA status transition to `Review In Progress` or `Ready to Commit`
  (`jira_changelog`);
- a GitHub PR review (`pr_review`) or PR comment (`pr_comment`) on a linked
  PR -- already bot-filtered at collection time
  (`collectors/github.py::_is_bot_login`), per `dev_metrics.py`'s own
  documented reasoning for why GitHub-side rows need no `bot_identifier`
  join.

There is no fixed priority across sources -- whichever qualifying event has
the earliest timestamp wins, and `details_json.first_response_source`
records which one it was (`'jira_comment'` | `'jira_status'` | `'gh_review'`
| `'gh_comment'`).

## Verified against a one-off reference analysis (`blend.py`, issue #102)

A one-off script over a full raw pull (11,754 Patch-Available issues' JIRA
changelog + comment metadata, all `apache/cassandra` GitHub PRs) implements
the same definitions in ~40 lines of plain Python and was used to sanity
check this module's output before this issue's PR was opened; see the PR
description for the side-by-side per-year table. Two disclosed, intentional
differences from that reference script:

- **Bot list.** This module reuses this project's own maintained
  `bot_patterns` (`projects/<id>.yaml`, applied via `bot_identifier` for
  JIRA and at GitHub-collection time), not `blend.py`'s ad hoc
  hardcoded name list -- expected to exclude a slightly different (more
  complete, project-reviewed) set of bot accounts.
- **Comment cap.** `issue_comment` is capped at each issue's earliest
  `collectors.jira.MAX_COMMENTS_PER_ISSUE_STORED` (20) comments; `blend.py`
  read every comment. Since "first response" only ever needs an issue's
  *earliest* qualifying comment, this cap changes nothing for
  `review_first_response_median_days`/the share metrics below unless an
  issue's first 20 comments are *all* from the author/bots and a 21st+
  comment from someone else would have been the true first response -- a
  rare case for a Patch-Available-tagged issue (review discussion is
  usually not the reporter monologuing 20+ times before anyone replies).
- **Right-censoring denominators.** Unlike `blend.py` (which age-gates the
  7d/30d/no-visible-response shares all off the *same* >=30-day-old cohort),
  this module uses the age gate the issue's own spec asks for: the 7d share's
  denominator is submissions >=7 days old at `as_of`, the 30d share's and
  no-visible-response share's denominator is submissions >=30 days old --
  narrower per-stat cohorts, so this module's 7d share numbers are not
  directly comparable to a hand recomputation using `blend.py`'s single
  30-day gate for every stat.

## Windows: calendar-year and trailing-12-month

Every stat is reported twice: once as dense calendar-year rows (Jan 1 -
Dec 31, completed years only -- a year is "completed" once `as_of` has moved
into a later calendar year, mirroring D5's "completed periods only" rule),
and once as dense trailing-12-calendar-month rows (one row per completed
month, `metrics.windows.trailing_12m_window`) -- the issue's own rationale:
first-patch volume is too small (~30-50/year) for a monthly window to be
readable, but a rolling trailing-12m view still shows trend movement between
full calendar years. Metric ids encode both the tier and the window
(`_yearly`/`_trailing12m` suffix) since `metric_value` has no dedicated
tier/window-kind column (a calendar year and a trailing-12m window ending
that same December can otherwise collide on `(window_start, window_end)`).
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

import duckdb
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.metrics.engine import _bot_identifiers, _connect
from project_health.metrics.windows import add_months, month_end, month_start, trailing_12m_window
from project_health.schema import get_schema, validate

DEFINITION_VERSION = "1.0"

TIER_FIRST = "first"
TIER_2_5 = "tier_2_5"
TIER_6PLUS = "tier_6plus"
TIERS: tuple[str, ...] = (TIER_FIRST, TIER_2_5, TIER_6PLUS)

WINDOW_YEARLY = "yearly"
WINDOW_TRAILING12M = "trailing12m"
WINDOWS: tuple[str, ...] = (WINDOW_YEARLY, WINDOW_TRAILING12M)

RESPONSE_WITHIN_7D_DAYS = 7
RESPONSE_WITHIN_30D_DAYS = 30
COMMITTED_WITHIN_DAYS = 365

# METRICS.md §0.6 default floors, reused here for consistency even though
# this module deliberately doesn't call `metrics/engine.py::_make_row`
# (see module docstring -- kept fully independent of `engine.py`'s
# `DEFINITION_VERSIONS`/`METRIC_IDS` on purpose).
FLOOR_RATE_RATIO = 5
FLOOR_LATENCY = 5

_STAT_MEDIAN_DAYS = "review_first_response_median_days"
_STAT_WITHIN_7D = "review_response_within_7d_share"
_STAT_WITHIN_30D = "review_response_within_30d_share"
_STAT_NO_VISIBLE_RESPONSE = "review_no_visible_response_share"
_STAT_COMMITTED_365D = "patch_committed_within_365d_share"
_STAT_FIRST_PATCH_SUBMISSIONS = "first_patch_submissions"

_TIERED_STATS: tuple[str, ...] = (
    _STAT_MEDIAN_DAYS,
    _STAT_WITHIN_7D,
    _STAT_WITHIN_30D,
    _STAT_NO_VISIBLE_RESPONSE,
    _STAT_COMMITTED_365D,
)


def metric_id(stat: str, window: str, tier: str | None) -> str:
    """The `metric_value.metric_id` for one (stat, window, tier) combination.

    `tier=None` is used only for `first_patch_submissions`, which is
    tier-`first`-only by definition and so carries no tier suffix.
    """
    suffix = f"_{tier}" if tier else ""
    return f"{stat}_{window}{suffix}"


ALL_METRIC_IDS: tuple[str, ...] = tuple(
    metric_id(stat, window, tier) for stat in _TIERED_STATS for window in WINDOWS for tier in TIERS
) + tuple(metric_id(_STAT_FIRST_PATCH_SUBMISSIONS, window, None) for window in WINDOWS)


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
    details: dict,
) -> dict:
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


@dataclass(frozen=True)
class _Submission:
    issue_key: str
    author: str | None
    submitted_at: datetime
    tier: str
    first_response_at: datetime | None
    first_response_source: str | None
    resolved_fixed_at: datetime | None


def _first_patch_available_actor(con: duckdb.DuckDBPyConnection) -> dict[str, str | None]:
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT
                issue_key, actor_raw_value,
                ROW_NUMBER() OVER (PARTITION BY issue_key ORDER BY changed_at ASC) AS rn
            FROM jira_changelog
            WHERE field = 'status' AND to_value = 'Patch Available'
        )
        SELECT issue_key, actor_raw_value FROM ranked WHERE rn = 1
        """
    ).fetchall()
    return dict(rows)


def _build_submissions(con: duckdb.DuckDBPyConnection) -> list[_Submission]:
    """Reconstruct every patch submission from the accumulated raw tables --
    see the module docstring for the full definition. Pure Python over a
    handful of DuckDB fetches (not one giant SQL query): the tiering step
    needs a single, globally-sorted pass no SQL window function can express
    as directly as a plain running counter, and the blended first-response
    detection mixes four differently-shaped sources per issue.
    """
    pa_rows = con.execute(
        """
        SELECT issue_key, MIN(changed_at) AS tpa
        FROM jira_changelog
        WHERE field = 'status' AND to_value = 'Patch Available'
        GROUP BY 1
        """
    ).fetchall()
    if not pa_rows:
        return []
    tpa_by_issue: dict[str, datetime] = dict(pa_rows)

    assignee_by_issue: dict[str, list[tuple[datetime, str | None, str | None]]] = {}
    for issue_key, changed_at, from_value, to_value in con.execute(
        """
        SELECT issue_key, changed_at, from_value, to_value
        FROM jira_changelog
        WHERE field = 'assignee'
        ORDER BY issue_key, changed_at
        """
    ).fetchall():
        assignee_by_issue.setdefault(issue_key, []).append((changed_at, from_value, to_value))

    pa_actor_by_issue = _first_patch_available_actor(con)

    issue_info: dict[str, tuple[str | None, str | None, datetime | None]] = {
        issue_key: (assignee_raw, resolution, resolved_at)
        for issue_key, assignee_raw, resolution, resolved_at in con.execute(
            # `issue` holds several rows per key (nightly + backfill, D3 append-only).
            # Keep the latest `updated_at`; on a tie prefer the row that carries
            # `resolution` -- rows written before #102 have it null, and the
            # backfill re-emits the same `updated_at` with it filled in.
            """
            SELECT issue_key, assignee_raw, resolution, resolved_at
            FROM issue
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY issue_key
                ORDER BY updated_at DESC, (resolution IS NOT NULL) DESC
            ) = 1
            """
        ).fetchall()
    }

    # issue #105: `pr_issue_link`'s (repo, number, issue_key) rows backfill
    # the ticket key(s) a PR collected *before* `pr.linked_issue_keys`
    # (issue #102) existed named in its title -- see schema/tables.py's
    # `PR_ISSUE_LINK` docstring. Keyed by (repo, number) so it can be unioned
    # per-PR against `pr.linked_issue_keys` below, since either source (or
    # both, redundantly) can name the same key for the same PR.
    backfilled_keys_by_pr: dict[tuple[str, int], set[str]] = {}
    for repo, number, issue_key in con.execute(
        "SELECT repo, number, issue_key FROM pr_issue_link"
    ).fetchall():
        backfilled_keys_by_pr.setdefault((repo, number), set()).add(issue_key)

    # Flattened in Python rather than a SQL `unnest()` -- simpler and avoids
    # any DuckDB list-column edge case, and `pr` is a small enough table
    # (thousands of rows, not millions) that this costs nothing measurable.
    prs_by_issue: dict[str, list[dict]] = {}
    for repo, number, created_at, author, linked_issue_keys in con.execute(
        "SELECT repo, number, created_at, author_raw_value, linked_issue_keys FROM pr"
    ).fetchall():
        issue_keys = set(linked_issue_keys or []) | backfilled_keys_by_pr.get((repo, number), set())
        for issue_key in issue_keys:
            prs_by_issue.setdefault(issue_key, []).append(
                {"repo": repo, "number": number, "created_at": created_at, "author": author}
            )

    comments_by_issue: dict[str, list[tuple[str, datetime]]] = {}
    for issue_key, author, created_at in con.execute(
        """
        SELECT jc.issue_key, jc.author_raw_value, jc.created_at
        FROM issue_comment jc
        LEFT JOIN bot_identifier bi
            ON bi.raw_type = 'jira_username' AND bi.raw_value = jc.author_raw_value
        WHERE bi.raw_value IS NULL AND jc.author_raw_value IS NOT NULL
        """
    ).fetchall():
        comments_by_issue.setdefault(issue_key, []).append((author, created_at))

    status_events_by_issue: dict[str, list[tuple[datetime, str | None]]] = {}
    for issue_key, changed_at, actor in con.execute(
        """
        SELECT jc.issue_key, jc.changed_at, jc.actor_raw_value
        FROM jira_changelog jc
        LEFT JOIN bot_identifier bi
            ON bi.raw_type = 'jira_username' AND bi.raw_value = jc.actor_raw_value
        WHERE jc.field = 'status'
          AND jc.to_value IN ('Review In Progress', 'Ready to Commit')
          AND bi.raw_value IS NULL
        """
    ).fetchall():
        status_events_by_issue.setdefault(issue_key, []).append((changed_at, actor))

    reviews_by_pr: dict[tuple[str, int], list[tuple[datetime, str]]] = {}
    for repo, number, submitted_at, reviewer in con.execute(
        """
        SELECT repo, pr_number, submitted_at, reviewer_raw_value
        FROM pr_review
        WHERE submitted_at IS NOT NULL AND reviewer_raw_value IS NOT NULL
        """
    ).fetchall():
        reviews_by_pr.setdefault((repo, number), []).append((submitted_at, reviewer))

    pr_comments_by_pr: dict[tuple[str, int], list[tuple[datetime, str]]] = {}
    for repo, number, created_at, author in con.execute(
        """
        SELECT repo, pr_number, created_at, author_raw_value
        FROM pr_comment
        WHERE author_raw_value IS NOT NULL
        """
    ).fetchall():
        pr_comments_by_pr.setdefault((repo, number), []).append((created_at, author))

    prelim: list[_Submission] = []
    for issue_key, tpa in tpa_by_issue.items():
        linked_prs = prs_by_issue.get(issue_key, [])
        submitted_at = min([tpa] + [pr["created_at"] for pr in linked_prs])

        assignee_after = [
            change for change in assignee_by_issue.get(issue_key, []) if change[0] > tpa
        ]
        current_assignee, resolution, resolved_at = issue_info.get(issue_key, (None, None, None))
        # `assignee_after[0][1]` (the `from_value` of the first assignee
        # change after `tpa`) can itself be null -- the issue had no
        # assignee yet at the moment of the Patch-Available transition, and
        # only got its *first* assignee afterward. That's a genuinely
        # missing value, not "no assignee change happened at all", so it
        # falls through the *same* fallback chain a missing `assignee_after`
        # would: current assignee, then the transition's own actor.
        history_author = assignee_after[0][1] if assignee_after else None
        if history_author is not None:
            author = history_author
        elif current_assignee:
            author = current_assignee
        else:
            author = pa_actor_by_issue.get(issue_key)

        pr_author_set = {pr["author"] for pr in linked_prs if pr["author"]}

        events: list[tuple[datetime, str]] = []
        for c_author, c_time in comments_by_issue.get(issue_key, []):
            if c_time >= submitted_at and c_author != author:
                events.append((c_time, "jira_comment"))
        for s_time, s_actor in status_events_by_issue.get(issue_key, []):
            if s_time >= submitted_at and s_actor != author:
                events.append((s_time, "jira_status"))
        for pr in linked_prs:
            pr_key = (pr["repo"], pr["number"])
            pr_author = pr["author"]
            for r_time, r_author in reviews_by_pr.get(pr_key, []):
                if (
                    r_time >= submitted_at
                    and r_author != pr_author
                    and r_author not in pr_author_set
                ):
                    events.append((r_time, "gh_review"))
            for c_time, c_author2 in pr_comments_by_pr.get(pr_key, []):
                if (
                    c_time >= submitted_at
                    and c_author2 != pr_author
                    and c_author2 not in pr_author_set
                ):
                    events.append((c_time, "gh_comment"))

        first_response = min(events, key=lambda e: e[0]) if events else None
        resolved_fixed_at = resolved_at if resolution == "Fixed" else None

        prelim.append(
            _Submission(
                issue_key=issue_key,
                author=author,
                submitted_at=submitted_at,
                tier="",  # filled in below, once sorted
                first_response_at=first_response[0] if first_response else None,
                first_response_source=first_response[1] if first_response else None,
                resolved_fixed_at=resolved_fixed_at,
            )
        )

    prelim.sort(key=lambda s: s.submitted_at)
    seen: Counter[str] = Counter()
    submissions: list[_Submission] = []
    for index, submission in enumerate(prelim):
        # An author-less submission (no assignee ever, no PA-transition actor
        # either -- should be rare) gets a unique per-submission key so it
        # never falsely inflates another real author's tier count, and is
        # itself always tier `first` (the degenerate, but documented, case).
        author_key = submission.author or f"__unknown__:{index}"
        prior = seen[author_key]
        tier = TIER_FIRST if prior == 0 else (TIER_2_5 if prior < 5 else TIER_6PLUS)
        seen[author_key] += 1
        submissions.append(replace(submission, tier=tier))
    return submissions


def _calendar_year_windows(submissions: list[_Submission], as_of: date) -> list[tuple[date, date]]:
    if not submissions:
        return []
    first_year = min(s.submitted_at.year for s in submissions)
    last_completed_year = as_of.year - 1
    if last_completed_year < first_year:
        return []
    return [
        (date(year, 1, 1), date(year, 12, 31))
        for year in range(first_year, last_completed_year + 1)
    ]


def _trailing12m_windows(submissions: list[_Submission], as_of: date) -> list[tuple[date, date]]:
    if not submissions:
        return []
    first_month = month_start(min(s.submitted_at.date() for s in submissions))
    last_month = add_months(month_start(as_of), -1)
    if first_month > last_month:
        return []
    windows: list[tuple[date, date]] = []
    cursor = first_month
    while cursor <= last_month:
        window_end = month_end(cursor)
        window_start, _ = trailing_12m_window(window_end)
        windows.append((window_start, window_end))
        cursor = add_months(cursor, 1)
    return windows


def _days_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 86400.0


def _window_rows(
    cohort: list[_Submission],
    windows: list[tuple[date, date]],
    window_kind: str,
    tier: str | None,
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> list[dict]:
    as_of_dt = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)
    rows: list[dict] = []
    for window_start, window_end in windows:
        in_window = [s for s in cohort if window_start <= s.submitted_at.date() <= window_end]
        n_submitted = len(in_window)

        with_response = [s for s in in_window if s.first_response_at is not None]
        response_days = [_days_between(s.submitted_at, s.first_response_at) for s in with_response]
        median_days = statistics.median(response_days) if response_days else None

        old_7d = [
            s for s in in_window if (as_of_dt - s.submitted_at).days >= RESPONSE_WITHIN_7D_DAYS
        ]
        n_within_7d = sum(
            1
            for s in old_7d
            if s.first_response_at is not None
            and _days_between(s.submitted_at, s.first_response_at) <= RESPONSE_WITHIN_7D_DAYS
        )

        old_30d = [
            s for s in in_window if (as_of_dt - s.submitted_at).days >= RESPONSE_WITHIN_30D_DAYS
        ]
        n_within_30d = sum(
            1
            for s in old_30d
            if s.first_response_at is not None
            and _days_between(s.submitted_at, s.first_response_at) <= RESPONSE_WITHIN_30D_DAYS
        )
        n_no_visible_response = sum(1 for s in old_30d if s.first_response_at is None)

        old_365d = [
            s for s in in_window if (as_of_dt - s.submitted_at).days >= COMMITTED_WITHIN_DAYS
        ]
        n_committed = sum(
            1
            for s in old_365d
            if s.resolved_fixed_at is not None
            and _days_between(s.submitted_at, s.resolved_fixed_at) <= COMMITTED_WITHIN_DAYS
        )

        source_counts = Counter(
            s.first_response_source for s in in_window if s.first_response_source
        )

        rows.append(
            _row(
                metric_id_=metric_id(_STAT_MEDIAN_DAYS, window_kind, tier),
                window_start=window_start,
                window_end=window_end,
                raw_value=median_days,
                n=len(with_response),
                floor=FLOOR_LATENCY,
                run_id=run_id,
                computed_at=computed_at,
                details={"n_submitted": n_submitted, "source_breakdown": dict(source_counts)},
            )
        )
        rows.append(
            _row(
                metric_id_=metric_id(_STAT_WITHIN_7D, window_kind, tier),
                window_start=window_start,
                window_end=window_end,
                raw_value=(n_within_7d / len(old_7d)) if old_7d else None,
                n=len(old_7d),
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={"n_hit": n_within_7d, "n_denominator": len(old_7d)},
            )
        )
        rows.append(
            _row(
                metric_id_=metric_id(_STAT_WITHIN_30D, window_kind, tier),
                window_start=window_start,
                window_end=window_end,
                raw_value=(n_within_30d / len(old_30d)) if old_30d else None,
                n=len(old_30d),
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={"n_hit": n_within_30d, "n_denominator": len(old_30d)},
            )
        )
        rows.append(
            _row(
                metric_id_=metric_id(_STAT_NO_VISIBLE_RESPONSE, window_kind, tier),
                window_start=window_start,
                window_end=window_end,
                raw_value=(n_no_visible_response / len(old_30d)) if old_30d else None,
                n=len(old_30d),
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={"n_hit": n_no_visible_response, "n_denominator": len(old_30d)},
            )
        )
        rows.append(
            _row(
                metric_id_=metric_id(_STAT_COMMITTED_365D, window_kind, tier),
                window_start=window_start,
                window_end=window_end,
                raw_value=(n_committed / len(old_365d)) if old_365d else None,
                n=len(old_365d),
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={"n_hit": n_committed, "n_denominator": len(old_365d)},
            )
        )
    return rows


def _first_patch_submissions_rows(
    first_tier: list[_Submission],
    windows: list[tuple[date, date]],
    window_kind: str,
    run_id: str,
    computed_at: datetime,
) -> list[dict]:
    rows = []
    for window_start, window_end in windows:
        n = sum(1 for s in first_tier if window_start <= s.submitted_at.date() <= window_end)
        rows.append(
            _row(
                metric_id_=metric_id(_STAT_FIRST_PATCH_SUBMISSIONS, window_kind, None),
                window_start=window_start,
                window_end=window_end,
                raw_value=float(n),
                n=n,
                floor=0,  # headcount: reports for any n, including 0 (METRICS.md §0.6 exemption).
                run_id=run_id,
                computed_at=computed_at,
                details={},
            )
        )
    return rows


def compute_review_responsiveness(
    tables: dict[str, pa.Table],
    *,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    config: ProjectConfig,
) -> pa.Table:
    """Every review-responsiveness `metric_value` row for this run.

    Kept as its own entry point (not wired into `metrics/engine.py::
    compute_all`) -- see module docstring "Deliberately outside...". Callers
    (`pipeline.py`) write this table to its own snapshot file, mirroring
    `governance/fact_metrics.py`'s `governance_metric_value.parquet`
    treatment, not the main `metrics.parquet`/`METRIC_IDS`-checked path.
    """
    con = _connect(tables)
    try:
        con.register("bot_identifier", _bot_identifiers(con, config))
        submissions = _build_submissions(con)
    finally:
        con.close()

    if not submissions:
        return get_schema("metric_value").empty_table()

    yearly_windows = _calendar_year_windows(submissions, as_of)
    trailing_windows = _trailing12m_windows(submissions, as_of)

    rows: list[dict] = []
    for tier in TIERS:
        cohort = [s for s in submissions if s.tier == tier]
        rows.extend(
            _window_rows(cohort, yearly_windows, WINDOW_YEARLY, tier, as_of, run_id, computed_at)
        )
        rows.extend(
            _window_rows(
                cohort, trailing_windows, WINDOW_TRAILING12M, tier, as_of, run_id, computed_at
            )
        )

    first_tier = [s for s in submissions if s.tier == TIER_FIRST]
    rows.extend(
        _first_patch_submissions_rows(
            first_tier, yearly_windows, WINDOW_YEARLY, run_id, computed_at
        )
    )
    rows.extend(
        _first_patch_submissions_rows(
            first_tier, trailing_windows, WINDOW_TRAILING12M, run_id, computed_at
        )
    )

    schema = get_schema("metric_value")
    table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    return validate("metric_value", table)
