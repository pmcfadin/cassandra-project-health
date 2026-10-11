"""Golden tests for project_health.metrics.contributor_pipeline (issue #159):
`first_to_second_conversion_rate`, `funnel_stage_conversion`,
`contributor_tenure_survival`, `contributor_churn_rate`, and
`sustained_contributor_count`.

Same hand-built-fixture pattern test_metrics_engine.py already uses (one
`test_<metric_id>_golden` per scenario, exact `value`/`n`/`flag`/
`details_json` assertions) -- `AS_OF` is fixed at 2024-03-15 throughout, so
January and February 2024 are always the "completed months" and March 2024
is always the current, excluded month.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pyarrow as pa
import pytest

from project_health.metrics.contributor_pipeline import (
    CHURN_INACTIVITY_DAYS,
    FLOOR_COHORT_SURVIVAL,
    _FUNNEL_NOT_AVAILABLE_REASON,
)
from project_health.metrics.engine import compute_all
from tests.fixtures.metrics.builders import contribution_events, identity_link_for
from tests.test_metrics_engine import CONFIG, NOW

UTC = timezone.utc
RUN_ID = "run-test-contributor-pipeline-1"
COMPUTED_AT = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
AS_OF = date(2024, 3, 15)


def _ts(y, m, d, hh=12):
    return datetime(y, m, d, hh, 0, tzinfo=UTC)


def _rows_for(table: pa.Table, metric_id: str) -> list[dict]:
    rows = table.to_pylist()
    return sorted(
        (r for r in rows if r["metric_id"] == metric_id),
        key=lambda r: r["window_start"],
    )


def _details(row: dict) -> dict:
    return json.loads(row["details_json"]) if row["details_json"] else {}


def _commits(author: str, dates: list[date]) -> list[dict]:
    return [
        {
            "author_raw_type": "git_email",
            "author_raw_value": author,
            "occurred_at": _ts(d.year, d.month, d.day),
        }
        for d in dates
    ]


def _compute(rows: list[dict]) -> pa.Table:
    contribution_event = contribution_events(rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)
    return compute_all(
        {"contribution_event": contribution_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )


# --- first_to_second_conversion_rate -----------------------------------------


def test_first_to_second_conversion_rate_golden():
    rows = []
    # 5-person Jan 2016 cohort (n == 5, meets the rate/ratio floor). 3 land
    # a second commit within 180 days; 2 don't (one has no second commit at
    # all, one's second commit lands past the 180-day window).
    rows += _commits("alice@example.org", [date(2016, 1, 5), date(2016, 2, 4)])  # +30d
    rows += _commits("bob@example.org", [date(2016, 1, 6), date(2016, 6, 1)])  # +147d
    rows += _commits("carol@example.org", [date(2016, 1, 7), date(2016, 7, 1)])  # +176d
    rows += _commits("dave@example.org", [date(2016, 1, 8)])  # no second commit ever
    rows += _commits(
        "eve@example.org", [date(2016, 1, 9), date(2016, 8, 1)]
    )  # +205d, past the window

    result = _compute(rows)
    by_window = {r["window_start"]: r for r in _rows_for(result, "first_to_second_conversion_rate")}
    row = by_window[date(2016, 1, 1)]

    assert row["n"] == 5
    assert row["flag"] == "ok"
    assert row["value"] == pytest.approx(0.6)
    details = _details(row)
    assert details["cohort_size"] == 5
    assert details["converted"] == 3
    assert details["follow_up_days"] == 180
    assert details["low_confidence"] is True  # 5 < 30


def test_first_to_second_conversion_rate_skips_cohorts_whose_window_has_not_elapsed():
    # A cohort whose 180-day follow-up window has not fully elapsed before
    # AS_OF (2024-03-15) must not appear at all -- METRICS.md: "evaluated
    # only once the 180-day follow-up window has fully elapsed".
    rows = _commits("zoe@example.org", [date(2024, 2, 1)])
    result = _compute(rows)
    by_window = {r["window_start"] for r in _rows_for(result, "first_to_second_conversion_rate")}
    assert date(2024, 2, 1) not in by_window


# --- funnel_stage_conversion --------------------------------------------------


def test_funnel_stage_conversion_golden():
    rows = []
    # Jan 2016 cohort, n == 5 (meets the floor). Horizon = 730 days from
    # each contributor's own first commit (~early Jan 2018).
    rows += _commits("alice@example.org", [date(2016, 1, 10)])  # reaches nothing
    rows += _commits(
        "bob@example.org", [date(2016, 1, 10), date(2016, 2, 10)]
    )  # 2nd only
    rows += _commits(
        "carol@example.org",
        [
            date(2016, 1, 10),
            date(2016, 1, 20),
            date(2016, 1, 30),
            date(2016, 2, 10),
            date(2016, 2, 20),
        ],
    )  # 2nd, 5th, and sustained (5 commits within 41 days)
    rows += _commits(
        "dave@example.org",
        [
            date(2016, 1, 10),
            date(2016, 7, 10),
            date(2017, 1, 10),
            date(2017, 7, 10),
            date(2018, 1, 5),
        ],
    )  # 2nd and 5th (within the 730d horizon), but not sustained (spread > 365d)
    rows += _commits("eve@example.org", [date(2016, 1, 10)])  # reaches nothing

    result = _compute(rows)
    by_window = {r["window_start"]: r for r in _rows_for(result, "funnel_stage_conversion")}
    row = by_window[date(2016, 1, 1)]

    assert row["n"] == 5
    assert row["flag"] == "ok"
    # Headline value: the deepest computable stage (sustained_contributor)'s
    # reached fraction -- only carol reaches it.
    assert row["value"] == pytest.approx(0.2)

    details = _details(row)
    assert details["cohort_size"] == 5
    assert details["horizon_days"] == 730
    stages = details["stages"]

    second = stages["second_contribution"]
    assert (second["reached"], second["not_reached"], second["in_flight"]) == (3, 2, 0)
    assert second["reached_fraction"] == pytest.approx(0.6)

    fifth = stages["fifth_contribution"]
    assert (fifth["reached"], fifth["not_reached"], fifth["in_flight"]) == (2, 3, 0)
    assert fifth["reached_fraction"] == pytest.approx(0.4)

    sustained = stages["sustained_contributor"]
    assert (sustained["reached"], sustained["not_reached"], sustained["in_flight"]) == (1, 4, 0)
    assert sustained["reached_fraction"] == pytest.approx(0.2)

    for stage_id in ("committer", "pmc_member"):
        assert stages[stage_id]["status"] == "not_available"
        assert stages[stage_id]["reason"] == _FUNNEL_NOT_AVAILABLE_REASON


# --- sustained_contributor_count ---------------------------------------------


def test_sustained_contributor_count_golden():
    rows = []
    # Trailing-12m window ending Feb 2024 == [2023-03-01, 2024-02-29].
    # alice: 5 commits in-window -> sustained. bob: 4 commits -> not.
    rows += _commits(
        "alice@example.org",
        [
            date(2023, 4, 1),
            date(2023, 6, 1),
            date(2023, 8, 1),
            date(2023, 10, 1),
            date(2023, 12, 1),
        ],
    )
    rows += _commits(
        "bob@example.org",
        [date(2023, 5, 1), date(2023, 6, 1), date(2023, 7, 1), date(2023, 8, 1)],
    )

    result = _compute(rows)
    by_window_end = {r["window_end"]: r for r in _rows_for(result, "sustained_contributor_count")}
    row = by_window_end[date(2024, 2, 29)]

    assert row["n"] == 1
    assert row["flag"] == "ok"  # headcount metric, exempt from the §0.6 floor (issue #27-style)
    assert row["value"] == 1.0
    assert row["window_start"] == date(2023, 3, 1)
    assert _details(row)["threshold_commits"] == 5


def test_sustained_contributor_count_reports_zero_not_insufficient_data():
    rows = _commits("alice@example.org", [date(2024, 1, 5)])  # 1 commit, below threshold
    result = _compute(rows)
    by_window_end = {r["window_end"]: r for r in _rows_for(result, "sustained_contributor_count")}
    row = by_window_end[date(2024, 2, 29)]
    assert row["n"] == 0
    assert row["value"] == 0.0
    assert row["flag"] == "ok"


# --- contributor_churn_rate ---------------------------------------------------


def test_contributor_churn_rate_golden():
    rows = []
    # Lookback window for reporting month Feb 2024 is [2022-02-01, 2023-01-31]
    # (m - 24 through m - 13). 3 contributors are active in-window and never
    # return (churned); 2 are active in-window and return later (not churned).
    rows += _commits("alice@example.org", [date(2022, 3, 1)])
    rows += _commits("bob@example.org", [date(2022, 5, 1)])
    rows += _commits("carol@example.org", [date(2022, 9, 1)])
    rows += _commits("dave@example.org", [date(2022, 4, 1), date(2023, 5, 1)])
    rows += _commits("eve@example.org", [date(2022, 10, 1), date(2023, 8, 1)])

    result = _compute(rows)
    by_window = {
        (r["window_start"], r["window_end"]): r for r in _rows_for(result, "contributor_churn_rate")
    }
    row = by_window[(date(2022, 2, 1), date(2023, 1, 31))]

    assert row["n"] == 5
    assert row["flag"] == "ok"
    assert row["value"] == pytest.approx(0.6)
    details = _details(row)
    assert details["reporting_month"] == date(2024, 2, 1).isoformat()
    assert details["active_in_window"] == 5
    assert details["churned"] == 3
    assert details["inactivity_window_days"] == CHURN_INACTIVITY_DAYS


def test_contributor_churn_rate_below_floor_is_insufficient_data():
    rows = _commits("alice@example.org", [date(2022, 3, 1)])
    result = _compute(rows)
    by_window = {
        (r["window_start"], r["window_end"]): r for r in _rows_for(result, "contributor_churn_rate")
    }
    row = by_window[(date(2022, 2, 1), date(2023, 1, 31))]
    assert row["n"] == 1
    assert row["flag"] == "insufficient_data"
    assert row["value"] is None


# --- contributor_tenure_survival (Kaplan-Meier) ------------------------------


def test_contributor_tenure_survival_below_floor_shows_still_active_table_not_a_curve():
    # 5 contributors (< FLOOR_COHORT_SURVIVAL == 30), each with a single
    # commit inside the trailing-24m cohort-intake window ending Feb 2024
    # ([2022-03-01, 2024-02-29]).
    rows = []
    for i in range(5):
        rows += _commits(f"dev{i}@example.org", [date(2022, 6, 1)])

    result = _compute(rows)
    rows_out = _rows_for(result, "contributor_tenure_survival")
    assert len(rows_out) == 1
    row = rows_out[0]

    assert row["n"] == 5
    assert row["flag"] == "insufficient_data"
    assert row["value"] is None
    assert row["window_start"] == date(2022, 3, 1)
    assert row["window_end"] == date(2024, 2, 29)

    details = _details(row)
    assert str(FLOOR_COHORT_SURVIVAL) in details["note"]
    table = {entry["months"]: entry for entry in details["still_active_table"]}
    assert set(table) == {3, 6, 12, 18, 24}
    # Tenure (2022-06-01 -> as_of 2024-03-15) comfortably clears the 3- and
    # 12-month marks, and each contributor's single commit means their
    # 12-month inactivity cutoff (2023-06-01) is still after both marks.
    assert table[3]["eligible"] == 5
    assert table[3]["still_active"] == 5
    assert table[12]["eligible"] == 5
    assert table[12]["still_active"] == 5


def test_contributor_tenure_survival_golden_curve_at_floor():
    # 30 contributors (== FLOOR_COHORT_SURVIVAL), each with exactly one
    # commit on the same day inside the trailing-24m cohort window -- a
    # one-time contributor's tenure is 0 (METRICS.md: the churn event is
    # "dated to their last observed activity", i.e. last - first, not
    # last + the 12-month confirmation window -- orchestrator review of
    # issue #159 fixup). Fully hand-computable KM curve: everyone churns at
    # the same time (t=0), so the curve has one step, straight to 0.
    rows = []
    for i in range(30):
        rows += _commits(f"dev{i}@example.org", [date(2022, 6, 1)])

    result = _compute(rows)
    rows_out = _rows_for(result, "contributor_tenure_survival")
    assert len(rows_out) == 1
    row = rows_out[0]

    assert row["n"] == 30
    assert row["flag"] == "ok"
    assert row["value"] == pytest.approx(0.0)

    details = _details(row)
    assert details["cohort_size"] == 30
    assert details["event_count"] == 30
    assert details["censored_count"] == 0
    assert details["median_tenure_days"] == 0
    assert details["median_not_reached"] is False
    assert details["survival_at_12m"] == pytest.approx(0.0)
    curve = details["curve"]
    assert len(curve) == 1
    point = curve[0]
    assert point["t_days"] == 0
    assert point["survival"] == pytest.approx(0.0)
    assert point["n_at_risk"] == 30
    assert point["n_events"] == 30
    assert point["ci_low"] == pytest.approx(0.0)
    assert point["ci_high"] == pytest.approx(0.0)
    # Everyone's duration is 0 (no censored contributors, no later
    # follow-up) -- no extension point past the one event, no censor marks.
    assert details["max_follow_up_days"] == 0
    assert details["censor_marks"] == []


def test_contributor_tenure_survival_censors_a_recently_active_contributor():
    # 30 contributors, all first-committing 2022-06-01: 29 are one-timers
    # (event at t=0); 1 ("recent") commits again close to AS_OF, so their
    # inactivity_cutoff (last + 365d) is still in the future -- right-
    # censored at as_of - first, not treated as churned.
    recent_first = date(2022, 6, 1)
    rows = []
    for i in range(29):
        rows += _commits(f"dev{i}@example.org", [date(2022, 6, 1)])
    rows += _commits("recent@example.org", [recent_first, date(2024, 1, 1)])

    result = _compute(rows)
    row = _rows_for(result, "contributor_tenure_survival")[0]
    assert row["n"] == 30
    details = _details(row)
    assert details["event_count"] == 29
    assert details["censored_count"] == 1

    censor_duration = (AS_OF - recent_first).days

    # The one censored contributor doesn't produce a KM step of its own
    # (only event times do), but the curve now (orchestrator review of
    # issue #159 fixup) extends flat out to the maximum observed follow-up
    # time -- here, the censored contributor's own duration -- rather than
    # stopping at the last event, so it has 2 points: the t=0 event step,
    # and the trailing extension at the censored contributor's duration.
    assert len(details["curve"]) == 2
    event_point, extension_point = details["curve"]
    assert event_point["t_days"] == 0
    assert event_point["n_events"] == 29
    assert event_point["n_at_risk"] == 30  # the censored one is still at risk at t=0
    assert extension_point["t_days"] == censor_duration
    assert extension_point["n_events"] == 0
    assert extension_point["n_at_risk"] == 1  # only the censored contributor is left
    assert extension_point["survival"] == pytest.approx(event_point["survival"])

    assert details["max_follow_up_days"] == censor_duration
    assert len(details["censor_marks"]) == 1
    assert details["censor_marks"][0]["t_days"] == censor_duration
    assert details["censor_marks"][0]["survival"] == pytest.approx(event_point["survival"])


def test_contributor_tenure_survival_event_time_is_dated_to_last_activity_not_window_end():
    """Orchestrator review of issue #159 fixup: the churn EVENT must be
    dated to the contributor's own last observed activity (tenure = last -
    first), never to last + the 12-month confirmation window -- that
    window only decides whether a churn is confirmed as of `as_of`, not
    when it's dated. Three explicit scenarios, 10 contributors each (30
    total, meets the KM floor):

    - group A ("one-timer"): a single commit, long since confirmed churned
      -> event at t=0.
    - group B ("active 0-7 months then silent"): two commits ~7 months
      apart, long since confirmed churned -> event at t=(last-first).days
      (~214 days, ~7 months) -- NOT at +365d/+12 months.
    - group C ("active within the last 12 months"): last commit recent
      enough that churn isn't confirmed yet -> right-censored at
      as_of - first, not an event at all.
    """
    group_a_first = date(2022, 6, 1)
    group_b_first = date(2022, 6, 1)
    group_b_last = date(2023, 1, 1)  # 214 days after group_b_first (~7 months)
    group_c_first = date(2023, 6, 1)
    group_c_last = date(2024, 1, 1)  # within 12 months of AS_OF (2024-03-15)

    rows = []
    for i in range(10):
        rows += _commits(f"onetimer{i}@example.org", [group_a_first])
    for i in range(10):
        rows += _commits(f"sevenmonth{i}@example.org", [group_b_first, group_b_last])
    for i in range(10):
        rows += _commits(f"recent{i}@example.org", [group_c_first, group_c_last])

    result = _compute(rows)
    row = _rows_for(result, "contributor_tenure_survival")[0]
    assert row["n"] == 30
    assert row["flag"] == "ok"

    details = _details(row)
    assert details["event_count"] == 20  # groups A + B
    assert details["censored_count"] == 10  # group C

    event_b_days = (group_b_last - group_b_first).days
    assert event_b_days == 214
    censor_c_days = (AS_OF - group_c_first).days
    assert censor_c_days > event_b_days  # group C's censoring duration is the max follow-up

    # Orchestrator review of issue #159 fixup: the curve now extends flat
    # out to the maximum observed follow-up time -- group C's censoring
    # duration -- not just to the last EVENT time, so it has 3 points: the
    # two event steps (t=0, t=event_b_days) plus a trailing extension at
    # censor_c_days.
    curve_by_t = {point["t_days"]: point for point in details["curve"]}
    assert set(curve_by_t) == {0, event_b_days, censor_c_days}

    # t=0: all 30 still at risk (group C's censoring duration is far larger
    # than 0), 10 events (group A). Ŝ(0) = 1 - 10/30 = 2/3.
    point_a = curve_by_t[0]
    assert point_a["n_at_risk"] == 30
    assert point_a["n_events"] == 10
    assert point_a["survival"] == pytest.approx(20 / 30, abs=1e-4)

    # t=event_b_days: group A (duration 0) has already left the risk set;
    # 20 left at risk (group B + group C), 10 events (group B).
    # Ŝ(event_b_days) = 2/3 * (1 - 10/20) = 1/3.
    point_b = curve_by_t[event_b_days]
    assert point_b["n_at_risk"] == 20
    assert point_b["n_events"] == 10
    assert point_b["survival"] == pytest.approx((20 / 30) * (1 - 10 / 20), abs=1e-4)

    # t=censor_c_days: the trailing extension point -- only group C (10
    # people, all sharing the same censoring duration) is still at risk;
    # no event there, survival unchanged from the event_b_days step.
    point_c = curve_by_t[censor_c_days]
    assert point_c["n_at_risk"] == 10
    assert point_c["n_events"] == 0
    assert point_c["survival"] == pytest.approx(point_b["survival"], abs=1e-4)

    assert details["median_tenure_days"] == event_b_days
    assert details["median_not_reached"] is False
    # Ŝ(365 days) reads off the last point at or before t=365 -- that's the
    # censor_c_days extension point here (288 < 365 < the next thing that
    # would exist), but since the extension only carries the event_b_days
    # step's value forward unchanged, the number is the same either way.
    assert censor_c_days < 365
    assert details["survival_at_12m"] == pytest.approx(point_b["survival"], abs=1e-4)

    assert details["max_follow_up_days"] == censor_c_days
    # All 10 group-C members share the identical first/last commit dates,
    # so they all produce the same censor mark.
    assert len(details["censor_marks"]) == 10
    for mark in details["censor_marks"]:
        assert mark["t_days"] == censor_c_days
        assert mark["survival"] == pytest.approx(point_b["survival"], abs=1e-4)


def test_contributor_tenure_survival_curve_tracks_at_risk_through_staggered_censoring():
    """Orchestrator review of issue #159 fixup: the curve must carry a
    point at every distinct observed time (event or censoring), not just
    event times, so the low-n tail fade has a real point to split on where
    the risk set actually crosses the threshold -- not just at the final
    point. 20 contributors churn together at t=0; 15 more are censored at
    15 distinct, staggered durations (50, 60, ..., 190 days), so n_at_risk
    declines one-by-one across those 15 points (15, 14, ..., 1) instead of
    jumping straight from 20 to 1.
    """
    rows = []
    for i in range(20):
        rows += _commits(f"onetimer{i}@example.org", [date(2022, 6, 1)])

    staggered_durations = list(range(50, 200, 10))  # 50, 60, ..., 190 -- 15 values
    assert len(staggered_durations) == 15
    for i, duration_days in enumerate(staggered_durations):
        first = AS_OF - timedelta(days=duration_days)
        rows += _commits(f"staggered{i}@example.org", [first])

    result = _compute(rows)
    row = _rows_for(result, "contributor_tenure_survival")[0]
    assert row["n"] == 35
    assert row["flag"] == "ok"

    details = _details(row)
    assert details["event_count"] == 20
    assert details["censored_count"] == 15
    assert details["max_follow_up_days"] == 190

    curve_by_t = {point["t_days"]: point for point in details["curve"]}
    # One point for the t=0 event, plus one per distinct staggered
    # censoring duration -- the engine must not collapse the 15 distinct
    # censoring times down to just a single trailing point.
    assert set(curve_by_t) == {0, *staggered_durations}

    assert curve_by_t[0]["n_at_risk"] == 35
    assert curve_by_t[0]["n_events"] == 20

    # n_at_risk declines by exactly 1 at each successive staggered
    # censoring point (15 down to 1), with n_events == 0 throughout (none
    # of these are confirmed churns).
    for rank, duration_days in enumerate(staggered_durations):
        point = curve_by_t[duration_days]
        assert point["n_at_risk"] == 15 - rank
        assert point["n_events"] == 0

    # The low-n fade threshold (10) is first crossed at the 7th staggered
    # point (n_at_risk drops from 10 to 9 there) -- a real, distinct point
    # mid-curve, not just the final one.
    first_faded = staggered_durations[6]
    assert curve_by_t[first_faded]["n_at_risk"] == 9
    assert curve_by_t[staggered_durations[5]]["n_at_risk"] == 10

    assert len(details["censor_marks"]) == 15
    assert {mark["t_days"] for mark in details["censor_marks"]} == set(staggered_durations)
