"""Golden and reproducibility tests for project_health.metrics.engine (issue #7).

Each `test_<metric_id>_golden` case hand-builds just enough normalized-table
data (via tests/fixtures/metrics/builders.py) to exercise one metric's
formula, then asserts the exact `metric_value` row(s) it expects -- value,
`n`, `flag`, and the metric-specific `details_json` fields. `as_of` is fixed
at 2024-03-15 throughout, so January and February 2024 are always the
"completed months" and March 2024 is always the current, excluded month.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pytest

from project_health.config import load_project
from project_health.metrics.engine import compute_all
from tests.fixtures.metrics.builders import (
    contribution_events,
    identity_link_for,
    issues,
    review_events,
    roster_entries,
)

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_project(REPO_ROOT / "projects" / "cassandra.yaml")

NOW = datetime(2026, 9, 25, tzinfo=UTC)
RUN_ID = "run-test-1"
COMPUTED_AT = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
AS_OF = date(2024, 3, 15)  # Jan/Feb 2024 completed; March 2024 is the current, excluded month.


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


# --- active_contributors_monthly (headcount: reports any n, issue #27) -----


def test_active_contributors_monthly_golden_reports_any_n():
    ce_rows = []
    # January 2024: 5 distinct authors.
    for i in range(5):
        ce_rows.append(
            {
                "author_raw_type": "git_email",
                "author_raw_value": f"alice{i}@example.org",
                "occurred_at": _ts(2024, 1, 5),
            }
        )
    # A bot commit in January must not inflate the count.
    ce_rows.append(
        {
            "author_raw_type": "git_email",
            "author_raw_value": "dependabot[bot]@users.noreply.github.com",
            "occurred_at": _ts(2024, 1, 6),
        }
    )
    # February 2024: only 2 distinct authors -- below the old floor (5), but
    # headcounts are exempt from §0.6 (issue #27): still flag='ok'.
    for i in range(2):
        ce_rows.append(
            {
                "author_raw_type": "git_email",
                "author_raw_value": f"bob{i}@example.org",
                "occurred_at": _ts(2024, 2, 10),
            }
        )
    # March 2024 (the as_of month) must never appear in the output.
    ce_rows.append(
        {
            "author_raw_type": "git_email",
            "author_raw_value": "carol@example.org",
            "occurred_at": _ts(2024, 3, 1),
        }
    )

    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)

    result = compute_all(
        {"contribution_event": contribution_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "active_contributors_monthly")
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]

    jan, feb = rows
    assert jan["window_end"] == date(2024, 1, 31)
    assert jan["n"] == 5
    assert jan["value"] == 5.0
    assert jan["flag"] == "ok"
    assert jan["definition_version"] == "1.1"
    assert jan["run_id"] == RUN_ID
    assert jan["computed_at"] == COMPUTED_AT

    # Below the old rate/ratio floor (5), but headcounts are exempt (#27).
    assert feb["n"] == 2
    assert feb["value"] == 2.0
    assert feb["flag"] == "ok"
    assert feb["definition_version"] == "1.1"


def test_active_contributors_monthly_excludes_current_month_and_bots():
    ce_rows = [
        {
            "author_raw_type": "git_email",
            "author_raw_value": "carol@example.org",
            "occurred_at": _ts(2024, 3, 1),
        }
    ]
    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)

    result = compute_all(
        {"contribution_event": contribution_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    assert _rows_for(result, "active_contributors_monthly") == []


# --- new_contributors_monthly -----------------------------------------------


def test_new_contributors_monthly_golden():
    ce_rows = []
    for i in range(5):
        ce_rows.append(
            {
                "author_raw_type": "git_email",
                "author_raw_value": f"alice{i}@example.org",
                "occurred_at": _ts(2024, 1, 5),
            }
        )
    # alice0's second commit, in February -- must NOT count as "new" again.
    ce_rows.append(
        {
            "author_raw_type": "git_email",
            "author_raw_value": "alice0@example.org",
            "occurred_at": _ts(2024, 2, 1),
        }
    )
    # bob0's first-ever commit, in February -- exactly one new contributor
    # that month; below the old rate/ratio floor, but headcounts are exempt
    # (issue #27), so this still reports flag='ok'.
    ce_rows.append(
        {
            "author_raw_type": "git_email",
            "author_raw_value": "bob0@example.org",
            "occurred_at": _ts(2024, 2, 2),
        }
    )
    # carol's first-ever commit lands in the as_of month -- excluded.
    ce_rows.append(
        {
            "author_raw_type": "git_email",
            "author_raw_value": "carol@example.org",
            "occurred_at": _ts(2024, 3, 1),
        }
    )

    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)

    result = compute_all(
        {"contribution_event": contribution_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "new_contributors_monthly")
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]

    jan, feb = rows
    assert jan["n"] == 5
    assert jan["value"] == 5.0
    assert jan["flag"] == "ok"
    assert jan["definition_version"] == "1.1"

    assert feb["n"] == 1
    assert feb["value"] == 1.0
    assert feb["flag"] == "ok"
    assert feb["definition_version"] == "1.1"


def test_new_contributors_monthly_reports_low_and_zero_n_as_ok():
    """Issue #27: (a) n=3 in a month, and (b) a fully empty gap month
    (n=0) must both report `flag='ok'` with `value=n`, never
    `insufficient_data` due to the old §0.6 sample floor."""
    as_of = date(2024, 5, 1)
    ce_rows = []
    # January 2024: 3 first-ever commits -- below the old floor (5).
    for i in range(3):
        ce_rows.append(
            {
                "author_raw_type": "git_email",
                "author_raw_value": f"alice{i}@example.org",
                "occurred_at": _ts(2024, 1, 5),
            }
        )
    # March 2024: one first-ever commit, so the dense-months range covers
    # February (a fully empty gap month, n=0) between January and March.
    ce_rows.append(
        {
            "author_raw_type": "git_email",
            "author_raw_value": "carol@example.org",
            "occurred_at": _ts(2024, 3, 1),
        }
    )

    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)

    result = compute_all(
        {"contribution_event": contribution_event, "identity_link": identity_link},
        as_of=as_of,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = {r["window_start"]: r for r in _rows_for(result, "new_contributors_monthly")}
    assert sorted(rows) == [date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1), date(2024, 4, 1)]

    jan = rows[date(2024, 1, 1)]
    assert jan["n"] == 3
    assert jan["value"] == 3.0
    assert jan["flag"] == "ok"

    feb = rows[date(2024, 2, 1)]  # fully empty gap month
    assert feb["n"] == 0
    assert feb["value"] == 0.0
    assert feb["flag"] == "ok"


# --- unique_reviewers_monthly ------------------------------------------------


def test_unique_reviewers_monthly_golden_union_with_per_source_breakdown():
    re_rows = []
    trailer_names = ["Nina Reviewer", "Oscar Reviewer", "Piper Reviewer"]
    jira_users = ["qreviewer", "rreviewer", "sreviewer"]
    for name in trailer_names:
        re_rows.append(
            {
                "source": "commit_trailer",
                "reviewer_raw_type": "git_name",
                "reviewer_raw_value": name,
                "occurred_at": _ts(2024, 1, 10),
            }
        )
    for user in jira_users:
        re_rows.append(
            {
                "source": "jira_field",
                "reviewer_raw_type": "jira_username",
                "reviewer_raw_value": user,
                "occurred_at": _ts(2024, 1, 12),
            }
        )
    # February: only 2 distinct reviewers -- below the old floor, but
    # headcounts are exempt from §0.6 (issue #27): still flag='ok'.
    re_rows.append(
        {
            "source": "commit_trailer",
            "reviewer_raw_type": "git_name",
            "reviewer_raw_value": "Tara Reviewer",
            "occurred_at": _ts(2024, 2, 5),
        }
    )
    re_rows.append(
        {
            "source": "jira_field",
            "reviewer_raw_type": "jira_username",
            "reviewer_raw_value": "ureviewer",
            "occurred_at": _ts(2024, 2, 6),
        }
    )

    review_event = review_events(re_rows)
    identity_link = identity_link_for(review_events=review_event, now=NOW)

    result = compute_all(
        {"review_event": review_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "unique_reviewers_monthly")
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]

    jan, feb = rows
    assert jan["n"] == 6
    assert jan["value"] == 6.0
    assert jan["flag"] == "ok"
    assert jan["definition_version"] == "1.1"
    jan_details = _details(jan)
    assert jan_details == {
        "commit_trailer_count": 3,
        "jira_field_count": 3,
        "union_count": 6,
    }

    assert feb["n"] == 2
    assert feb["value"] == 2.0
    assert feb["flag"] == "ok"
    assert feb["definition_version"] == "1.1"


def test_monthly_metrics_are_dense_across_a_gap_month_and_a_trailing_gap():
    # Fixup-cycle-1 review: windows must be dense, not sparse. Activity in
    # January and April 2024 only; February and March are a gap in the
    # middle, and May is a trailing gap after the last event but still a
    # completed month before as_of (2024-06-01) -- every one of Jan..May must
    # get a row, none silently missing.
    as_of = date(2024, 6, 1)
    ce_rows = []
    re_rows = []
    for i in range(5):
        ce_rows.append(
            {
                "author_raw_type": "git_email",
                "author_raw_value": f"jan{i}@example.org",
                "occurred_at": _ts(2024, 1, 5),
            }
        )
        re_rows.append(
            {
                "source": "commit_trailer",
                "reviewer_raw_type": "git_name",
                "reviewer_raw_value": f"Jan Reviewer {i}",
                "occurred_at": _ts(2024, 1, 5),
            }
        )
    for i in range(5):
        ce_rows.append(
            {
                "author_raw_type": "git_email",
                "author_raw_value": f"apr{i}@example.org",
                "occurred_at": _ts(2024, 4, 5),
            }
        )
        re_rows.append(
            {
                "source": "jira_field",
                "reviewer_raw_type": "jira_username",
                "reviewer_raw_value": f"apr{i}",
                "occurred_at": _ts(2024, 4, 5),
            }
        )

    contribution_event = contribution_events(ce_rows)
    review_event = review_events(re_rows)
    identity_link = identity_link_for(
        contribution_events=contribution_event, review_events=review_event, now=NOW
    )

    result = compute_all(
        {
            "contribution_event": contribution_event,
            "review_event": review_event,
            "identity_link": identity_link,
        },
        as_of=as_of,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    expected_months = [date(2024, m, 1) for m in (1, 2, 3, 4, 5)]

    for metric_id in ("active_contributors_monthly", "new_contributors_monthly"):
        rows = {r["window_start"]: r for r in _rows_for(result, metric_id)}
        assert sorted(rows) == expected_months
        assert rows[date(2024, 1, 1)]["n"] == 5
        assert rows[date(2024, 1, 1)]["flag"] == "ok"
        assert rows[date(2024, 4, 1)]["n"] == 5
        assert rows[date(2024, 4, 1)]["flag"] == "ok"
        # Fully empty gap months are still headcounts: n=0, flag='ok',
        # value=0.0 (issue #27) -- never insufficient_data.
        for gap_month in (date(2024, 2, 1), date(2024, 3, 1), date(2024, 5, 1)):
            gap_row = rows[gap_month]
            assert gap_row["n"] == 0
            assert gap_row["value"] == 0.0
            assert gap_row["flag"] == "ok"

    reviewer_rows = {r["window_start"]: r for r in _rows_for(result, "unique_reviewers_monthly")}
    assert sorted(reviewer_rows) == expected_months
    assert reviewer_rows[date(2024, 1, 1)]["n"] == 5
    assert reviewer_rows[date(2024, 4, 1)]["n"] == 5
    for gap_month in (date(2024, 2, 1), date(2024, 3, 1), date(2024, 5, 1)):
        gap_row = reviewer_rows[gap_month]
        assert gap_row["n"] == 0
        assert gap_row["value"] == 0.0
        assert gap_row["flag"] == "ok"
        assert _details(gap_row) == {
            "commit_trailer_count": 0,
            "jira_field_count": 0,
            "union_count": 0,
        }


# --- reviewer_hhi -------------------------------------------------------------


def test_reviewer_hhi_golden_concentration_reliable_from_flag_and_source_split():
    re_rows = []
    # Pre-2017 window: 5 commit_trailer reviewers, evenly split ->
    # HHI = 5 * (1/5)^2 = 0.2, and this window ends before
    # reviewer_extraction.reliable_from (2017-01-01).
    for i in range(5):
        re_rows.append(
            {
                "source": "commit_trailer",
                "reviewer_raw_type": "git_name",
                "reviewer_raw_value": f"Old Reviewer {i}",
                "occurred_at": _ts(2010, 1, 10),
            }
        )
    # 2024 window, commit_trailer credits: 3, 1, 1, 1, 2 -> HHI = (9+1+1+1+4)/64
    # = 16/64 = 0.25 exactly, effective_reviewer_population = 4.
    for _ in range(3):
        re_rows.append(
            {
                "source": "commit_trailer",
                "reviewer_raw_type": "git_name",
                "reviewer_raw_value": "Alpha Reviewer",
                "occurred_at": _ts(2024, 1, 10),
            }
        )
    for name in ["Bravo Reviewer", "Charlie Reviewer", "Delta Reviewer"]:
        re_rows.append(
            {
                "source": "commit_trailer",
                "reviewer_raw_type": "git_name",
                "reviewer_raw_value": name,
                "occurred_at": _ts(2024, 1, 11),
            }
        )
    for _ in range(2):
        re_rows.append(
            {
                "source": "commit_trailer",
                "reviewer_raw_type": "git_name",
                "reviewer_raw_value": "Echo Reviewer",
                "occurred_at": _ts(2024, 1, 12),
            }
        )
    # 2024 window, jira_field credits: 5 reviewers, 1 credit each -> a
    # cross-check only, never mixed into the primary value.
    for i in range(5):
        re_rows.append(
            {
                "source": "jira_field",
                "reviewer_raw_type": "jira_username",
                "reviewer_raw_value": f"j{i}",
                "occurred_at": _ts(2024, 1, 13),
            }
        )

    review_event = review_events(re_rows)
    identity_link = identity_link_for(review_events=review_event, now=NOW)

    result = compute_all(
        {"review_event": review_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = {r["window_end"]: r for r in _rows_for(result, "reviewer_hhi")}
    # Dense: every completed month from 2010-01 through 2024-02 (AS_OF's
    # last-completed month) gets a row, not just the two months with data.
    assert date(2010, 1, 31) in rows
    assert date(2024, 1, 31) in rows
    assert len(rows) == 170  # 2010-01 .. 2024-02 inclusive, one row per month

    # A gap month between the two data points is still emitted, empty.
    gap_window = rows[date(2015, 6, 30)]
    assert gap_window["n"] == 0
    assert gap_window["value"] is None
    assert gap_window["flag"] == "insufficient_data"
    assert _details(gap_window)["jira_field_hhi"] is None
    assert _details(gap_window)["union_hhi"] is None

    old_window = rows[date(2010, 1, 31)]
    assert old_window["window_start"] == date(2009, 2, 1)
    assert old_window["n"] == 5
    assert old_window["value"] == pytest.approx(0.2)
    assert old_window["flag"] == "ok"
    old_details = _details(old_window)
    assert old_details["effective_reviewer_population"] == pytest.approx(5.0)
    assert old_details["before_reliable_from"] is True
    assert old_details["jira_field_hhi"] is None
    assert old_details["union_hhi"] == pytest.approx(0.2)

    new_window = rows[date(2024, 1, 31)]
    assert new_window["window_start"] == date(2023, 2, 1)
    assert new_window["n"] == 5
    assert new_window["value"] == pytest.approx(0.25)
    assert new_window["flag"] == "ok"
    new_details = _details(new_window)
    assert new_details["effective_reviewer_population"] == pytest.approx(4.0)
    assert new_details["before_reliable_from"] is False
    assert new_details["jira_field_hhi"] == pytest.approx(0.2)
    expected_union_hhi = (3 / 13) ** 2 + (2 / 13) ** 2 + 8 * (1 / 13) ** 2
    assert new_details["union_hhi"] == pytest.approx(expected_union_hhi)


def test_reviewer_hhi_dense_windows_continue_past_the_last_event():
    # A repro straight from the fixup-cycle-1 review comment: review events
    # only span March-August 2025, but as_of is 2026-01-15 -- windows ending
    # September 2025 through December 2025 must still be emitted (their
    # trailing-12m lookback still covers the March-August events), not
    # silently stop once the raw events run out.
    months = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8)]
    re_rows = [
        {
            "source": "commit_trailer",
            "reviewer_raw_type": "git_name",
            "reviewer_raw_value": f"Reviewer {i}",
            "occurred_at": _ts(year, month, 15),
        }
        for i, (year, month) in enumerate(months)
    ]
    review_event = review_events(re_rows)
    identity_link = identity_link_for(review_events=review_event, now=NOW)

    as_of = date(2026, 1, 15)
    result = compute_all(
        {"review_event": review_event, "identity_link": identity_link},
        as_of=as_of,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = {r["window_end"]: r for r in _rows_for(result, "reviewer_hhi")}
    expected_ends = [
        date(2025, 3, 31),
        date(2025, 4, 30),
        date(2025, 5, 31),
        date(2025, 6, 30),
        date(2025, 7, 31),
        date(2025, 8, 31),
        date(2025, 9, 30),
        date(2025, 10, 31),
        date(2025, 11, 30),
        date(2025, 12, 31),
    ]
    assert sorted(rows) == expected_ends

    # Windows ending before 5 reviewers have accumulated are insufficient_data.
    for window_end in expected_ends[:4]:  # Mar, Apr, May, Jun -> n = 1..4
        assert rows[window_end]["flag"] == "insufficient_data"

    # July: exactly 5 distinct reviewers accumulated -> meets the floor.
    july = rows[date(2025, 7, 31)]
    assert july["n"] == 5
    assert july["value"] == pytest.approx(0.2)  # 5 reviewers, 1 credit each
    assert july["flag"] == "ok"

    # August onward: 6 distinct reviewers, all within every later trailing
    # window too (no new events after August, but the last four windows'
    # 12-month lookback still fully covers March-August) -- so August,
    # September, October, November and December all carry the SAME value:
    # this is exactly the "dense windows after the last event" fix.
    expected_hhi_six = 6 * (1 / 6) ** 2
    for window_end in expected_ends[5:]:  # Aug, Sep, Oct, Nov, Dec
        row = rows[window_end]
        assert row["n"] == 6
        assert row["value"] == pytest.approx(expected_hhi_six)
        assert row["flag"] == "ok"


# --- median_resolution_latency_jira -------------------------------------------


def test_median_resolution_latency_jira_golden():
    issue_rows = []
    latencies_days = [2, 4, 6, 10, 20]
    for i, days in enumerate(latencies_days):
        issue_rows.append(
            {
                "issue_key": f"CASSANDRA-{1000 + i}",
                "created_at": _ts(2024, 1, 1, hh=0),
                "updated_at": _ts(2024, 1, 1, hh=0) + _days(days),
                "resolved_at": _ts(2024, 1, 1, hh=0) + _days(days),
            }
        )
    # February: only 1 resolved issue -> below the floor.
    issue_rows.append(
        {
            "issue_key": "CASSANDRA-2000",
            "created_at": _ts(2024, 2, 1, hh=0),
            "updated_at": _ts(2024, 2, 3, hh=0),
            "resolved_at": _ts(2024, 2, 3, hh=0),
        }
    )
    # March (as_of month): must be excluded entirely.
    issue_rows.append(
        {
            "issue_key": "CASSANDRA-3000",
            "created_at": _ts(2024, 3, 1, hh=0),
            "updated_at": _ts(2024, 3, 2, hh=0),
            "resolved_at": _ts(2024, 3, 2, hh=0),
        }
    )
    # An issue still open must never be treated as "resolved this month".
    issue_rows.append(
        {
            "issue_key": "CASSANDRA-4000",
            "created_at": _ts(2024, 1, 1, hh=0),
            "updated_at": _ts(2024, 1, 2, hh=0),
            "resolved_at": None,
        }
    )

    issue_table = issues(issue_rows)

    result = compute_all(
        {"issue": issue_table},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "median_resolution_latency_jira")
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]

    jan, feb = rows
    assert jan["n"] == 5
    assert jan["value"] == pytest.approx(6.0)
    assert jan["flag"] == "ok"
    jan_details = _details(jan)
    assert jan_details["n"] == 5
    assert jan_details["p90_days"] == pytest.approx(16.0)

    assert feb["n"] == 1
    assert feb["value"] is None
    assert feb["flag"] == "insufficient_data"


def test_median_resolution_latency_jira_is_dense_across_a_gap_month():
    as_of = date(2024, 6, 1)
    issue_rows = [
        {
            "issue_key": "CASSANDRA-1",
            "created_at": _ts(2024, 1, 1, hh=0),
            "updated_at": _ts(2024, 1, 3, hh=0),
            "resolved_at": _ts(2024, 1, 3, hh=0),
        },
        {
            "issue_key": "CASSANDRA-2",
            "created_at": _ts(2024, 4, 1, hh=0),
            "updated_at": _ts(2024, 4, 5, hh=0),
            "resolved_at": _ts(2024, 4, 5, hh=0),
        },
    ]
    issue_table = issues(issue_rows)

    result = compute_all(
        {"issue": issue_table},
        as_of=as_of,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = {r["window_start"]: r for r in _rows_for(result, "median_resolution_latency_jira")}
    expected_months = [date(2024, m, 1) for m in (1, 2, 3, 4, 5)]
    assert sorted(rows) == expected_months

    for data_month in (date(2024, 1, 1), date(2024, 4, 1)):
        assert rows[data_month]["n"] == 1
        # below the floor (5), but still a row, not a missing one.
        assert rows[data_month]["flag"] == "insufficient_data"

    for gap_month in (date(2024, 2, 1), date(2024, 3, 1), date(2024, 5, 1)):
        gap_row = rows[gap_month]
        assert gap_row["n"] == 0
        assert gap_row["value"] is None
        assert gap_row["flag"] == "insufficient_data"
        assert _details(gap_row) == {"p90_days": None, "n": 0}


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


# --- stale_jira_rate -----------------------------------------------------


def test_stale_jira_rate_golden_single_snapshot_row():
    issue_rows = [
        # Stale: updated well before the 90-day cutoff (2023-12-15).
        {
            "issue_key": "CASSANDRA-1",
            "created_at": _ts(2019, 1, 1),
            "updated_at": _ts(2022, 1, 1),
        },
        {
            "issue_key": "CASSANDRA-2",
            "created_at": _ts(2019, 1, 1),
            "updated_at": _ts(2022, 6, 1),
        },
        {
            "issue_key": "CASSANDRA-3",
            "created_at": _ts(2019, 1, 1),
            "updated_at": _ts(2020, 1, 1),
        },
        # Not stale: updated after the cutoff.
        {
            "issue_key": "CASSANDRA-4",
            "created_at": _ts(2023, 1, 1),
            "updated_at": _ts(2024, 3, 1),
        },
        {
            "issue_key": "CASSANDRA-5",
            "created_at": _ts(2023, 1, 1),
            "updated_at": _ts(2024, 2, 1),
        },
        # Resolved -- must not count in either open-issue bucket.
        {
            "issue_key": "CASSANDRA-6",
            "created_at": _ts(2019, 1, 1),
            "updated_at": _ts(2019, 2, 1),
            "resolved_at": _ts(2019, 2, 1),
        },
    ]
    issue_table = issues(issue_rows)

    result = compute_all(
        {"issue": issue_table},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "stale_jira_rate")
    assert len(rows) == 1
    row = rows[0]
    assert row["window_start"] == AS_OF
    assert row["window_end"] == AS_OF
    assert row["n"] == 5
    assert row["value"] == pytest.approx(0.6)
    assert row["flag"] == "ok"
    details = _details(row)
    assert details["n_open"] == 5
    assert details["n_stale"] == 3
    assert details["threshold_days"] == 90


# --- Reproducibility (ARCHITECTURE.md §9) -------------------------------


def _build_all_tables_fixture() -> dict[str, pa.Table]:
    contribution_event = contribution_events(
        [
            {
                "author_raw_type": "git_email",
                "author_raw_value": f"alice{i}@example.org",
                "occurred_at": _ts(2024, 1, 5),
            }
            for i in range(5)
        ]
    )
    review_event = review_events(
        [
            {
                "source": "commit_trailer",
                "reviewer_raw_type": "git_name",
                "reviewer_raw_value": f"Reviewer {i}",
                "occurred_at": _ts(2024, 1, 10),
            }
            for i in range(5)
        ]
    )
    issue_table = issues(
        [
            {
                "issue_key": f"CASSANDRA-{i}",
                "created_at": _ts(2024, 1, 1),
                "updated_at": _ts(2024, 1, 1) + _days(i + 1),
                "resolved_at": _ts(2024, 1, 1) + _days(i + 1),
            }
            for i in range(5)
        ]
    )
    identity_link = identity_link_for(
        contribution_events=contribution_event,
        review_events=review_event,
        now=NOW,
    )
    return {
        "contribution_event": contribution_event,
        "review_event": review_event,
        "issue": issue_table,
        "identity_link": identity_link,
    }


def test_compute_all_is_reproducible_excluding_run_id_and_computed_at():
    tables = _build_all_tables_fixture()

    first = compute_all(
        tables,
        as_of=AS_OF,
        run_id="run-a",
        computed_at=datetime(2026, 1, 1, tzinfo=UTC),
        config=CONFIG,
    )
    second = compute_all(
        tables,
        as_of=AS_OF,
        run_id="run-b",
        computed_at=datetime(2026, 1, 2, tzinfo=UTC),
        config=CONFIG,
    )

    drop = ["run_id", "computed_at"]
    first_comparable = first.drop_columns(drop).to_pylist()
    second_comparable = second.drop_columns(drop).to_pylist()
    assert first_comparable == second_comparable
    assert first.num_rows == second.num_rows > 0


# --- pmc_joins_quarterly (headcount: reports any n, issue #27) -----


def test_pmc_joins_quarterly_includes_zero_join_quarters():
    """Test that zero-join quarters are reported with value=0.0, flag=ok.

    Headcount metrics like pmc_joins_quarterly must report any n including 0,
    with flag='ok', per issue #27. This golden test verifies quarterly join
    counts including quarters with zero new PMC joins.
    """
    roster_rows = [
        # Q3 2023: 2 PMC joins
        {
            "asf_id": "alice",
            "role": "pmc",
            "project": "cassandra",
            "effective_from": date(2023, 7, 15),
        },
        {
            "asf_id": "bob",
            "role": "pmc",
            "project": "cassandra",
            "effective_from": date(2023, 8, 1),
        },
        # Q4 2023: no joins (zero-join quarter, must still appear in output)
        # Non-PMC members should not be counted
        {
            "asf_id": "dave",
            "role": "committer",
            "project": "cassandra",
            "effective_from": None,
        },
    ]
    roster_table = roster_entries(roster_rows)

    result = compute_all(
        {"roster_entry": roster_table},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "pmc_joins_quarterly")
    # Should have rows for Q3 2023 and Q4 2023 (both completed before as_of=2024-03-15)
    quarters = [r["window_start"] for r in rows]
    assert len(rows) == 2, f"Expected 2 quarters, got {len(rows)}: {quarters}"

    q3, q4 = rows
    # Q3 2023: 2 joins
    assert q3["window_start"] == date(2023, 7, 1)
    assert q3["window_end"] == date(2023, 9, 30)
    assert q3["n"] == 2
    assert q3["value"] == 2.0
    assert q3["flag"] == "ok"
    assert q3["definition_version"] == "1.0"
    details_q3 = _details(q3)
    assert details_q3["pmc_new_joins"] == 2

    # Q4 2023: 0 joins (zero-join quarter must have value=0.0, flag=ok)
    assert q4["window_start"] == date(2023, 10, 1)
    assert q4["window_end"] == date(2023, 12, 31)
    assert q4["n"] == 0
    assert q4["value"] == 0.0
    assert q4["flag"] == "ok"  # Critical: zero-count headcount metric must be flag=ok
    assert q4["definition_version"] == "1.0"
    details_q4 = _details(q4)
    assert details_q4["pmc_new_joins"] == 0


# --- No per-person values (D2 rule 4 spirit) ----------------------------


def test_output_contains_no_raw_per_person_identifiers():
    tables = _build_all_tables_fixture()
    result = compute_all(
        tables,
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    dumped = json.dumps(result.to_pylist(), default=str)
    for marker in ("alice0@example.org", "alice4@example.org", "Reviewer 0", "Reviewer 4"):
        assert marker not in dumped

    # metric_value has no column that could carry a raw identifier or
    # identity_id at all -- aggregate-only by construction (schema/README.md).
    assert set(result.column_names) == {
        "metric_id",
        "definition_version",
        "window_start",
        "window_end",
        "value",
        "n",
        "flag",
        "run_id",
        "computed_at",
        "details_json",
    }
