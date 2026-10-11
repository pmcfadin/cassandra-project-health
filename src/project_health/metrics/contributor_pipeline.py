"""Contributor-pipeline funnel metrics (issue #159): METRICS.md's
"contributor sustainability" dimension gets five metrics the spec always
named but never implemented --

- `first_to_second_conversion_rate` -- cohort-month share of first-time
  committers who land a second commit within 180 days.
- `funnel_stage_conversion` -- multi-stage funnel (2nd -> 5th -> sustained
  -> committer -> PMC) for the same first-time-committer cohorts.
- `contributor_tenure_survival` -- Kaplan-Meier survival curve over
  contributor tenure, with a Greenwood's-formula confidence band.
- `contributor_churn_rate` -- the KM curve's "quick monthly read" companion.
- `sustained_contributor_count` -- headcount of contributors with >= 5
  commits in a trailing 12-month window.

Kept in its own module rather than growing `metrics/engine.py` further, the
same "own module, two lines to wire in" pattern `metrics/dev_metrics.py`
(issue #54) and `metrics/release_cadence.py` (issue #135) already
established -- this module reuses `engine.py`'s private helpers
(`_dense_months`, `_make_row`, `_last_completed_month`,
`_contributor_commit_counts`, the floor constants) rather than duplicating
them.

## Qualifying-activity scope (ambiguity resolution, issue #159)

METRICS.md §0.3 names several activity types (`code_commit`, `code_review`,
`jira_issue_authored`, `jira_comment`, `github_pr`, `github_pr_review`,
`mailing_list_post`) as candidates for "qualifying activity", and
`contributor_tenure_survival`/`contributor_churn_rate`'s own definitions say
"any of §0.3's activity types". This module scopes "qualifying activity" to
`code_commit` only (the `contribution_event` table), for two disclosed
reasons, not an oversight:

1. **Precedent.** Every contributor-sustainability metric already shipped
   (`active_contributors_monthly`, `new_contributors_monthly`,
   `truck_factor`, `contributor_hhi`, `contributor_absence_factor`) is
   git-commit-only despite METRICS.md's own broader activity-type catalog --
   this module stays consistent with its siblings rather than being the one
   metric family that suddenly spans every source.
2. **Identity resolution doesn't actually support a cross-source union
   yet.** `normalize.identity.extract_raw_identifiers` (the function that
   feeds `resolve_identities`) only ever reads `contribution_event`,
   `review_event`, and `issue` (reporter/assignee) -- `message` (mailing
   list), `pr`/`pr_review` (GitHub), and `roster_entry.asf_id` (ASF roster)
   are never fed into identity resolution at all. Joining those raw,
   unlinked identifiers into a "last active" signal alongside fully
   resolved git/JIRA identities would silently conflate "two different
   raw identifiers, never confirmed to be the same person" with "one
   resolved person", which is exactly what D2.5's identity-confidence rule
   forbids. `identity_overrides.yaml` is also empty today (no manual
   cross-source merges), so there is no path to a trustworthy union.

This is a disclosed simplification per the issue's own instruction ("where
the spec is ambiguous choose the simplest reading and document it in
METRICS.md") -- see METRICS.md's own per-metric sections for the mirrored
note.

## `funnel_stage_conversion`'s committer/PMC stages: not available

The committer and PMC stages need an ASF roster (`roster_entry`) row joined
to the SAME cohort identity used for the git-commit stages. That join does
not exist today, for the identical identity-resolution reason above:
`roster_entry.asf_id` is never extracted/resolved by
`normalize.identity.extract_raw_identifiers`/`resolve_identities`, so there
is no confirmed link from an ASF `availid` to a git/JIRA identity to join
against (`roster_entry.identity_id` is written `NULL` by every collector
and never filled in). METRICS.md D2.5 forbids auto-matching an availid to a
git email without that confirmed link, so fabricating one here would
violate this project's "never fabricate" rule (the issue's own instruction).
Both stages are reported `status: "not_available"` with this reason in
`details_json`, never a fabricated number -- `_FUNNEL_NOT_AVAILABLE_REASON`
below.
"""

from __future__ import annotations

import bisect
import math
from datetime import date, datetime, timedelta

import duckdb

from project_health.metrics.engine import (
    FLOOR_RATE_RATIO,
    _contributor_commit_counts,
    _dense_months,
    _last_completed_month,
    _make_row,
)
from project_health.metrics.windows import (
    add_months,
    month_end,
    month_start,
    trailing_12m_window,
    trailing_24m_window,
)
from project_health.schema import CODE_COMMIT

# contributor_tenure_survival's "Event (churn)" / contributor_churn_rate's
# "same churn/inactivity definition" (METRICS.md): 12 months of no
# qualifying activity, operationalized as 365 days.
CHURN_INACTIVITY_DAYS = 365

# first_to_second_conversion_rate's fixed follow-up window (METRICS.md,
# "default 180 days").
FIRST_TO_SECOND_FOLLOWUP_DAYS = 180

# funnel_stage_conversion's fixed observation horizon (METRICS.md, "default
# 24 months"), operationalized in days (24 * ~30.44) for the same reason
# this module uses day-granularity everywhere else -- simpler than
# reconciling month-anchored and day-anchored arithmetic for a per-
# contributor (not per-cohort-month) horizon.
FUNNEL_HORIZON_DAYS = 730

# contributor_tenure_survival's minimum cohort for a rendered KM curve
# (METRICS.md §0.6: "Cohort/survival metrics: floor = 30 individuals in the
# cohort for a reported Kaplan-Meier curve; smaller cohorts may be shown as
# raw counts only, never as a smoothed curve.").
FLOOR_COHORT_SURVIVAL = 30

# first_to_second_conversion_rate / funnel_stage_conversion's own
# "cohorts below 30 are shown but flagged low-confidence" note (METRICS.md),
# distinct from FLOOR_RATE_RATIO (5), which still gates insufficient_data.
LOW_CONFIDENCE_COHORT_FLOOR = 30

_FUNNEL_NOT_AVAILABLE_REASON = (
    "No confirmed identity link exists between an ASF roster asf_id and "
    "this project's git/JIRA identities: identity_overrides.yaml is empty, "
    "and normalize.identity.extract_raw_identifiers/resolve_identities does "
    "not process roster_entry.asf_id at all (roster_entry.identity_id is "
    "always NULL as written). METRICS.md D2.5 forbids auto-matching an ASF "
    "availid to a git email without a confirmed link, so this stage cannot "
    "be computed without fabricating a match. Committer effective_from "
    "dates are additionally often NULL in roster_entry (schema note: "
    "'join date if available, null otherwise')."
)

_FUNNEL_STAGE_ORDER: tuple[str, ...] = (
    "second_contribution",
    "fifth_contribution",
    "sustained_contributor",
    "committer",
    "pmc_member",
)

_FUNNEL_COMPUTABLE_STAGES: tuple[str, ...] = (
    "second_contribution",
    "fifth_contribution",
    "sustained_contributor",
)

_FUNNEL_NOT_AVAILABLE_STAGES: tuple[str, ...] = ("committer", "pmc_member")


# --- Shared commit history ---------------------------------------------------


def _commit_history(con: duckdb.DuckDBPyConnection) -> dict[str, list[date]]:
    """`{identity_id: [commit_date, ...]}`, ascending, for non-bot, resolved
    identities -- `code_commit` only (see module docstring's scope
    decision). Shared by every metric in this module so the underlying
    per-contributor commit history is only ever fetched/grouped once per
    `compute_contributor_pipeline` call."""
    rows = con.execute(
        """
        SELECT ri.identity_id AS identity_id, ce.occurred_at::DATE AS d
        FROM contribution_event ce
        JOIN resolved_identity ri
            ON ri.source_type = ce.author_raw_type AND ri.source_value = ce.author_raw_value
        LEFT JOIN bot_identifier bi
            ON bi.raw_type = ce.author_raw_type AND bi.raw_value = ce.author_raw_value
        WHERE ce.event_type = ? AND bi.raw_value IS NULL
        ORDER BY ri.identity_id, d
        """,
        [CODE_COMMIT],
    ).fetchall()
    history: dict[str, list[date]] = {}
    for identity_id, d in rows:
        history.setdefault(identity_id, []).append(d)
    return history


def _cohort_by_month(history: dict[str, list[date]]) -> dict[date, list[str]]:
    """`{cohort_month: [identity_id, ...]}` -- a contributor's cohort month
    is the calendar month of their first-ever qualifying commit
    (`new_contributors_monthly`'s own trigger event, METRICS.md)."""
    cohorts: dict[date, list[str]] = {}
    for identity_id, dates in history.items():
        if not dates:
            continue
        cohorts.setdefault(month_start(dates[0]), []).append(identity_id)
    return cohorts


# --- first_to_second_conversion_rate ----------------------------------------


def _first_to_second_conversion_rate(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    history: dict[str, list[date]],
) -> list[dict]:
    cohorts = _cohort_by_month(history)
    if not cohorts:
        return []

    out = []
    for month in _dense_months(min(cohorts), as_of):
        cohort = cohorts.get(month, [])
        n = len(cohort)
        if n == 0:
            continue
        # METRICS.md: "evaluated only once the 180-day follow-up window has
        # fully elapsed" -- worst case, a contributor whose first commit
        # landed on the LAST day of the cohort month. Unlike the generic
        # "dense, never sparse" convention (engine.py module docstring),
        # this metric's own window text says not to emit a reading before
        # that's true, so an in-flight cohort month is skipped entirely
        # here rather than emitted as `insufficient_data`.
        horizon_complete = (
            month_end(month) + timedelta(days=FIRST_TO_SECOND_FOLLOWUP_DAYS) < as_of
        )
        if not horizon_complete:
            continue

        converted = 0
        for identity_id in cohort:
            dates = history[identity_id]
            if len(dates) >= 2 and (dates[1] - dates[0]).days <= FIRST_TO_SECOND_FOLLOWUP_DAYS:
                converted += 1
        rate = converted / n

        out.append(
            _make_row(
                metric_id="first_to_second_conversion_rate",
                window_start=month,
                window_end=month_end(month),
                raw_value=rate,
                n=n,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "cohort_size": n,
                    "converted": converted,
                    "follow_up_days": FIRST_TO_SECOND_FOLLOWUP_DAYS,
                    "low_confidence": n < LOW_CONFIDENCE_COHORT_FLOOR,
                },
            )
        )
    return out


# --- funnel_stage_conversion -------------------------------------------------


def _funnel_stages_reached(dates: list[date], horizon_end: date) -> dict[str, bool]:
    """Per-contributor `{stage: reached_within_horizon}` for the three
    computable stages (METRICS.md `funnel_stage_conversion`)."""
    second = len(dates) >= 2 and dates[1] <= horizon_end
    fifth = len(dates) >= 5 and dates[4] <= horizon_end
    sustained = False
    if len(dates) >= 5:
        for i in range(4, len(dates)):
            if dates[i] > horizon_end:
                break
            # "5 contributions" (sustained_contributor_count's own
            # threshold) within any trailing-365-day span, by horizon_end --
            # a window-free proxy for "reached >= 5 commits in some
            # trailing-12m window" without re-gridding to calendar months.
            if (dates[i] - dates[i - 4]).days <= CHURN_INACTIVITY_DAYS:
                sustained = True
                break
    return {
        "second_contribution": second,
        "fifth_contribution": fifth,
        "sustained_contributor": sustained,
    }


def _funnel_stage_conversion(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    history: dict[str, list[date]],
) -> list[dict]:
    cohorts = _cohort_by_month(history)
    if not cohorts:
        return []

    out = []
    for month in _dense_months(min(cohorts), as_of):
        cohort = cohorts.get(month, [])
        n = len(cohort)
        if n == 0:
            continue

        counts = {
            stage: {"reached": 0, "in_flight": 0, "not_reached": 0}
            for stage in _FUNNEL_COMPUTABLE_STAGES
        }
        for identity_id in cohort:
            dates = history[identity_id]
            horizon_end = dates[0] + timedelta(days=FUNNEL_HORIZON_DAYS)
            horizon_elapsed = as_of > horizon_end
            reached = _funnel_stages_reached(dates, horizon_end)
            for stage, is_reached in reached.items():
                if is_reached:
                    counts[stage]["reached"] += 1
                elif horizon_elapsed:
                    counts[stage]["not_reached"] += 1
                else:
                    counts[stage]["in_flight"] += 1

        stages_detail: dict[str, dict] = {}
        for stage, c in counts.items():
            stages_detail[stage] = {
                "status": "computed",
                "reached": c["reached"],
                "in_flight": c["in_flight"],
                "not_reached": c["not_reached"],
                "reached_fraction": round(c["reached"] / n, 4),
                "in_flight_fraction": round(c["in_flight"] / n, 4),
                "not_reached_fraction": round(c["not_reached"] / n, 4),
            }
        for stage in _FUNNEL_NOT_AVAILABLE_STAGES:
            stages_detail[stage] = {
                "status": "not_available",
                "reason": _FUNNEL_NOT_AVAILABLE_REASON,
            }

        # Headline value: the deepest reliably-computable stage
        # ("sustained contributor"), reached-fraction. Role: supporting
        # (METRICS.md) -- this never drives a dimension status on its own.
        headline = counts["sustained_contributor"]["reached"] / n

        out.append(
            _make_row(
                metric_id="funnel_stage_conversion",
                window_start=month,
                window_end=month_end(month),
                raw_value=headline,
                n=n,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "cohort_size": n,
                    "horizon_days": FUNNEL_HORIZON_DAYS,
                    "stage_order": list(_FUNNEL_STAGE_ORDER),
                    "stages": stages_detail,
                    "low_confidence": n < LOW_CONFIDENCE_COHORT_FLOOR,
                },
            )
        )
    return out


# --- sustained_contributor_count ---------------------------------------------


def _sustained_contributor_count(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Headcount of contributors with >= 5 `code_commit`s in a trailing-12m
    window (METRICS.md `sustained_contributor_count`). Exempt from the
    METRICS.md §0.6 sample-size floor -- that metric's own "Population &
    exclusions" line cites only §0.5, not §0.6, unlike every ratio metric
    in this module, matching `active_contributors_monthly`/
    `new_contributors_monthly`'s existing exemption (issue #27): a raw
    headcount of "how many contributors cleared this threshold" is already
    the complete, meaningful statistic at any n, including 0. See
    `engine.HEADCOUNT_METRICS`, which this metric_id is added to."""
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m "
        "FROM contribution_event WHERE event_type = ?",
        [CODE_COMMIT],
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        window_start, _ = trailing_12m_window(window_end)
        credits = _contributor_commit_counts(con, window_start, window_end)
        sustained = sum(1 for _, commits in credits if commits >= 5)
        out.append(
            _make_row(
                metric_id="sustained_contributor_count",
                window_start=window_start,
                window_end=window_end,
                raw_value=float(sustained),
                n=sustained,
                floor=FLOOR_RATE_RATIO,  # ignored for headcount metrics
                run_id=run_id,
                computed_at=computed_at,
                details={"threshold_commits": 5},
            )
        )
    return out


# --- contributor_churn_rate ---------------------------------------------------


def _contributor_churn_rate(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    history: dict[str, list[date]],
) -> list[dict]:
    """Churn(m) = |{c: last_active(c) in [m-24, m-12)}| / |{c: active in
    [m-24, m-12)}| (METRICS.md `contributor_churn_rate`). `last_active(c)`
    is each contributor's single, global last commit date (over the full
    accumulated history, D3) -- fixed per identity, independent of `m` --
    so a contributor who returns after a long gap is correctly judged "not
    churned" for every historical `m`, the same hindsight-aware reading
    `contributor_tenure_survival`'s censoring logic uses."""
    if not history:
        return []
    sorted_history = {
        identity_id: sorted(dates) for identity_id, dates in history.items() if dates
    }
    last_active = {identity_id: dates[-1] for identity_id, dates in sorted_history.items()}
    first_commit_month = min(month_start(dates[0]) for dates in sorted_history.values())

    out = []
    # The lookback window [m-24, m-12) is 12 calendar months ending the
    # month before m-12 (m-13); the first reporting month `m` worth
    # evaluating is the first one whose lookback window could contain any
    # data at all.
    start_m = add_months(first_commit_month, 13)
    for month in _dense_months(start_m, as_of):
        lookback_end_month = add_months(month, -13)
        window_end = month_end(lookback_end_month)
        window_start, _ = trailing_12m_window(window_end)

        denom = 0
        churned = 0
        for identity_id, dates in sorted_history.items():
            i = bisect.bisect_left(dates, window_start)
            if i >= len(dates) or dates[i] > window_end:
                continue  # no commit in [window_start, window_end]
            denom += 1
            if window_start <= last_active[identity_id] <= window_end:
                churned += 1
        rate = (churned / denom) if denom else None

        out.append(
            _make_row(
                metric_id="contributor_churn_rate",
                window_start=window_start,
                window_end=window_end,
                raw_value=rate,
                n=denom,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "reporting_month": month.isoformat(),
                    "active_in_window": denom,
                    "churned": churned,
                    "inactivity_window_days": CHURN_INACTIVITY_DAYS,
                },
            )
        )
    return out


# --- contributor_tenure_survival (Kaplan-Meier) ------------------------------


def _kaplan_meier(durations: list[tuple[int, str]]) -> dict:
    """Standard Kaplan-Meier estimator plus a Greenwood's-formula 95% CI
    band (METRICS.md `contributor_tenure_survival`'s own formula:
    `Ŝ(t) = Π_{t_i<=t} (1 - d_i/n_i)`). `durations` is
    `[(duration_days, "event"|"censored"), ...]`. Returns `{"curve": [...],
    "median_days": int | None}` -- `median_days` is the first event time at
    which Ŝ(t) <= 0.5, or `None` if the curve never reaches it (a
    legitimate, healthy outcome -- "median not reached" -- not a computation
    failure, see `_contributor_tenure_survival`'s own handling of this)."""
    if not durations:
        return {"curve": [], "median_days": None}

    event_times = sorted({d for d, status in durations if status == "event"})
    survival = 1.0
    greenwood_sum = 0.0
    curve: list[dict] = []
    median_days: int | None = None

    for t in event_times:
        at_risk = sum(1 for d, _status in durations if d >= t)
        events_at_t = sum(1 for d, status in durations if d == t and status == "event")
        if at_risk == 0:
            continue
        survival *= 1 - events_at_t / at_risk
        if at_risk > events_at_t:
            greenwood_sum += events_at_t / (at_risk * (at_risk - events_at_t))
        se = survival * math.sqrt(greenwood_sum) if survival > 0 else 0.0
        ci_low = max(0.0, survival - 1.96 * se)
        ci_high = min(1.0, survival + 1.96 * se)
        curve.append(
            {
                "t_days": t,
                "t_months": round(t / 30.4368, 2),
                "survival": round(survival, 4),
                "ci_low": round(ci_low, 4),
                "ci_high": round(ci_high, 4),
                "n_at_risk": at_risk,
                "n_events": events_at_t,
            }
        )
        if median_days is None and survival <= 0.5:
            median_days = t

    return {"curve": curve, "median_days": median_days}


def _survival_at(curve: list[dict], t_days: float) -> float:
    """Ŝ(t_days), read off the KM step function -- 1.0 before the first
    event time, otherwise the most recent step at or before `t_days`."""
    survival = 1.0
    for point in curve:
        if point["t_days"] <= t_days:
            survival = point["survival"]
        else:
            break
    return survival


def _still_active_table(cohort: dict[str, list[date]], as_of: date) -> list[dict]:
    """METRICS.md's small-cohort fallback: "smaller populations ... are
    shown as a simple 'still active at N months' count table instead" of a
    smoothed curve. A contributor is "still active at N months" if they
    hadn't yet accumulated `CHURN_INACTIVITY_DAYS` of silence by the time
    their tenure reached N months -- only contributors old enough to have
    reached that mark are counted as `eligible` (no look-ahead)."""
    marks_months = (3, 6, 12, 18, 24)
    table = []
    for months in marks_months:
        mark_days = months * 30
        eligible = 0
        still_active = 0
        for dates in cohort.values():
            tenure_days = (as_of - dates[0]).days
            if tenure_days < mark_days:
                continue
            eligible += 1
            inactivity_cutoff = dates[-1] + timedelta(days=CHURN_INACTIVITY_DAYS)
            mark_date = dates[0] + timedelta(days=mark_days)
            if inactivity_cutoff >= mark_date:
                still_active += 1
        table.append(
            {
                "months": months,
                "eligible": eligible,
                "still_active": still_active,
                "still_active_fraction": (
                    round(still_active / eligible, 4) if eligible else None
                ),
            }
        )
    return table


def _contributor_tenure_survival(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    history: dict[str, list[date]],
) -> list[dict]:
    """One snapshot row per run (window = the trailing-24m cohort-intake
    window, METRICS.md's own window text), not a dense monthly series --
    same "single current snapshot" convention `stale_jira_rate`/
    `stale_pr_rate` already use for a metric whose own definition is a
    point-in-time read, not a calendar-month-by-month history."""
    if not history:
        return []

    last_completed = _last_completed_month(as_of)
    window_start, window_end = trailing_24m_window(month_end(last_completed))

    cohort = {
        identity_id: dates
        for identity_id, dates in history.items()
        if dates and window_start <= dates[0] <= window_end
    }
    n = len(cohort)

    if n < FLOOR_COHORT_SURVIVAL:
        return [
            _make_row(
                metric_id="contributor_tenure_survival",
                window_start=window_start,
                window_end=window_end,
                raw_value=None,
                n=n,
                floor=FLOOR_COHORT_SURVIVAL,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "cohort_size": n,
                    "note": (
                        f"Cohort below the METRICS.md §0.6 Kaplan-Meier floor "
                        f"({FLOOR_COHORT_SURVIVAL}); showing a simple still-active-count "
                        "table instead of a curve."
                    ),
                    "still_active_table": _still_active_table(cohort, as_of),
                },
            )
        ]

    # METRICS.md: the churn event is "dated to their last observed
    # activity" -- tenure (time-to-event) is last_activity - first_activity,
    # NOT last_activity + the 12-month inactivity window. The 12-month
    # window only decides whether a churn is *observed* (confirmed) as of
    # `as_of`: once 12 months have passed with no further activity, we date
    # the departure back to the last time the contributor was actually
    # seen, not to the end of the confirmation window. A contributor with
    # only one commit ever therefore has tenure 0 once confirmed (orchestrator
    # review of issue #159 fixup: the original implementation dated the
    # event at last_activity + 365 days instead, which flattened every
    # churned contributor's tenure to a multiple of ~12 months and produced
    # a nonsensical "everyone churns at exactly 12 months" curve).
    # Still-within-the-window contributors (not yet confirmed either way)
    # are right-censored at `as_of - first_activity` -- they might still
    # return, so their true tenure is unknown, not necessarily shorter.
    durations: list[tuple[int, str]] = []
    for dates in cohort.values():
        first = dates[0]
        last = dates[-1]
        inactivity_cutoff = last + timedelta(days=CHURN_INACTIVITY_DAYS)
        if inactivity_cutoff <= as_of:
            durations.append(((last - first).days, "event"))
        else:
            durations.append(((as_of - first).days, "censored"))

    km = _kaplan_meier(durations)
    event_count = sum(1 for _, status in durations if status == "event")
    censored_count = n - event_count
    # Headline value: Ŝ(365 days) -- the 12-month survival probability --
    # not the median, which can legitimately be "not reached" (None).
    # METRICS.md: "Direction of good: higher (a higher survival probability
    # at a given tenure is the healthy reading)".
    survival_12m = _survival_at(km["curve"], 365)

    return [
        _make_row(
            metric_id="contributor_tenure_survival",
            window_start=window_start,
            window_end=window_end,
            raw_value=round(survival_12m, 4),
            n=n,
            floor=FLOOR_COHORT_SURVIVAL,
            run_id=run_id,
            computed_at=computed_at,
            details={
                "cohort_size": n,
                "event_count": event_count,
                "censored_count": censored_count,
                "inactivity_window_days": CHURN_INACTIVITY_DAYS,
                "survival_at_12m": round(survival_12m, 4),
                "median_tenure_days": km["median_days"],
                "median_tenure_months": (
                    round(km["median_days"] / 30.4368, 2)
                    if km["median_days"] is not None
                    else None
                ),
                "median_not_reached": km["median_days"] is None,
                "curve": km["curve"],
            },
        )
    ]


# --- Entry point --------------------------------------------------------------


def compute_contributor_pipeline(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Compute all five contributor-pipeline metrics in one pass, sharing
    one `_commit_history` fetch (issue #159). Called from
    `engine.compute_all` the same way `dev_metrics.compute_dev_metrics`/
    `release_cadence.compute_release_cadence` are."""
    history = _commit_history(con)
    rows: list[dict] = []
    rows.extend(_first_to_second_conversion_rate(con, as_of, run_id, computed_at, history))
    rows.extend(_funnel_stage_conversion(con, as_of, run_id, computed_at, history))
    rows.extend(_contributor_tenure_survival(con, as_of, run_id, computed_at, history))
    rows.extend(_contributor_churn_rate(con, as_of, run_id, computed_at, history))
    rows.extend(_sustained_contributor_count(con, as_of, run_id, computed_at))
    return rows
