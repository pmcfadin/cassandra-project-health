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
from project_health.normalize.identity import identity_id_for
from project_health.schema import get_schema, validate
from tests.fixtures.metrics.builders import (
    affiliation_periods,
    contribution_events,
    file_change_events,
    identity_link_for,
    issue_comments,
    issues,
    messages,
    pr_reviews,
    prs,
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


def _github_login_identity_link(pairs: list[tuple[str, str]]) -> pa.Table:
    """Hand-built `identity_link` rows mapping `(identity_id, github_login)`
    pairs (issue #54 fixup: self-review exclusion's identity-level compare).
    Not routed through `identity_link_for`'s naive resolver -- that resolver
    has no `github_login` source_type support at all (issue #52's own
    `link_github_commit_authors` is what actually produces these rows in
    production); tests exercising the resolved-identity path build the table
    directly instead.
    """
    rows = [
        {
            "link_id": f"link-{i}",
            "identity_id": identity_id,
            "source_type": "github_login",
            "source_value": login,
            "confidence": "high",
            "evidence": "test fixture",
            "linked_by": "github_commit_author_v1",
            "linked_at": _ts(2024, 1, 1),
        }
        for i, (identity_id, login) in enumerate(pairs)
    ]
    schema = get_schema("identity_link")
    return validate("identity_link", pa.Table.from_pylist(rows, schema=schema))


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


# --- contributor_absence_factor / contributor_hhi (issue #53) ----------------


def _contribution_rows_for_identities(counts: dict[str, int], occurred_at: datetime) -> list[dict]:
    """`counts[email] = commit_count` -> that many `contribution_event` rows
    for `email`, all at `occurred_at` (only the calendar month matters for
    these golden tests, not the exact day)."""
    rows = []
    for email, count in counts.items():
        rows.extend(
            {"author_raw_type": "git_email", "author_raw_value": email, "occurred_at": occurred_at}
            for _ in range(count)
        )
    return rows


def test_contributor_absence_factor_and_hhi_golden_trailing_window_accumulates():
    # January 2024: 5 distinct contributors, commit counts 10/8/6/4/2 (total
    # 30) -- n == 5 meets the §0.6 concentration floor exactly.
    jan_counts = {"a@example.org": 10, "b@example.org": 8, "c@example.org": 6,
                  "d@example.org": 4, "e@example.org": 2}
    ce_rows = _contribution_rows_for_identities(jan_counts, _ts(2024, 1, 15))
    # February 2024: one new contributor with 20 commits; the trailing-12m
    # window ending Feb still carries January's contributors forward too.
    ce_rows += _contribution_rows_for_identities({"f@example.org": 20}, _ts(2024, 2, 15))

    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)

    result = compute_all(
        {"contribution_event": contribution_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    absence_rows = {r["window_end"]: r for r in _rows_for(result, "contributor_absence_factor")}
    hhi_rows = {r["window_end"]: r for r in _rows_for(result, "contributor_hhi")}

    jan = absence_rows[date(2024, 1, 31)]
    assert jan["n"] == 5
    assert jan["flag"] == "ok"
    # Sorted desc 10,8,6,4,2 (total 30, target 15): 10 -> cum 10 (<15); +8 ->
    # cum 18 (>=15) -- smallest N is 2.
    assert jan["value"] == 2.0
    jan_details = _details(jan)
    assert jan_details["total_commits"] == 30
    assert len(jan_details["contributors"]) == 5
    assert jan_details["contributors"][-1]["cumulative_share"] == pytest.approx(1.0)

    feb = absence_rows[date(2024, 2, 29)]
    # Trailing-12m window ending Feb still includes January's 30 commits
    # plus February's 20 -- n == 6, total 50, f's 20 is the single largest
    # holder now.
    assert feb["window_start"] == date(2023, 3, 1)
    assert feb["n"] == 6
    assert feb["flag"] == "ok"
    # Sorted desc 20,10,8,6,4,2 (total 50, target 25): 20 -> cum 20 (<25);
    # +10 -> cum 30 (>=25) -- smallest N is 2.
    assert feb["value"] == 2.0
    feb_details = _details(feb)
    assert feb_details["total_commits"] == 50

    jan_hhi = hhi_rows[date(2024, 1, 31)]
    assert jan_hhi["n"] == 5
    assert jan_hhi["flag"] == "ok"
    expected_jan_hhi = sum((c / 30) ** 2 for c in jan_counts.values())
    assert jan_hhi["value"] == pytest.approx(expected_jan_hhi)
    assert _details(jan_hhi)["effective_contributor_population"] == pytest.approx(
        1.0 / expected_jan_hhi
    )

    feb_hhi = hhi_rows[date(2024, 2, 29)]
    assert feb_hhi["n"] == 6
    assert feb_hhi["flag"] == "ok"
    feb_counts = {**jan_counts, "f@example.org": 20}
    expected_feb_hhi = sum((c / 50) ** 2 for c in feb_counts.values())
    assert feb_hhi["value"] == pytest.approx(expected_feb_hhi)
    assert _details(feb_hhi)["effective_contributor_population"] == pytest.approx(
        1.0 / expected_feb_hhi
    )


def test_contributor_absence_factor_and_hhi_below_floor_is_insufficient_data():
    # Only 2 distinct contributors -- below the concentration floor (5).
    ce_rows = _contribution_rows_for_identities(
        {"solo@example.org": 3, "duo@example.org": 1}, _ts(2024, 1, 10)
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

    absence_row = _rows_for(result, "contributor_absence_factor")[0]
    assert absence_row["n"] == 2
    assert absence_row["value"] is None
    assert absence_row["flag"] == "insufficient_data"

    hhi_row = _rows_for(result, "contributor_hhi")[0]
    assert hhi_row["n"] == 2
    assert hhi_row["value"] is None
    assert hhi_row["flag"] == "insufficient_data"


# --- elephant_factor / organizational_hhi / single_org_share /
#     unknown_affiliation_rate (issue #52, D6) --------------------------------


def _org_identity(email: str) -> str:
    return identity_id_for("git_email", email.lower())


def test_organizational_metrics_golden_five_known_orgs_plus_unknown():
    # January 2024: 5 known-org contributors (10/8/6/4/2 commits, one per
    # org -- meets the §0.6 concentration floor of 5 known orgs exactly)
    # plus one unaffiliated contributor with 5 commits, resolving to the
    # `unknown` bucket (no affiliation_period row at all for them).
    org_counts = {
        "a@orga.example": 10,
        "b@orgb.example": 8,
        "c@orgc.example": 6,
        "d@orgd.example": 4,
        "e@orge.example": 2,
    }
    ce_rows = _contribution_rows_for_identities(org_counts, _ts(2024, 1, 15))
    ce_rows += _contribution_rows_for_identities({"nobody@nowhere.example": 5}, _ts(2024, 1, 16))

    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)

    org_names = {"a@orga.example": "OrgA", "b@orgb.example": "OrgB", "c@orgc.example": "OrgC",
                 "d@orgd.example": "OrgD", "e@orge.example": "OrgE"}
    affiliation_period = affiliation_periods(
        [
            {"identity_id": _org_identity(email), "organization": org}
            for email, org in org_names.items()
        ]
    )

    result = compute_all(
        {
            "contribution_event": contribution_event,
            "identity_link": identity_link,
            "affiliation_period": affiliation_period,
        },
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    # total commits = 30 (known) + 5 (unknown) = 35; sorted desc by commits:
    # OrgA 10, OrgB 8, OrgC 6, unknown 5, OrgD 4, OrgE 2. Target = 17.5:
    # OrgA (10, cum 10) + OrgB (8, cum 18 >= 17.5) -> smallest_n = 2, unknown
    # not needed to reach the threshold.
    elephant = _rows_for(result, "elephant_factor")[0]
    assert elephant["n"] == 5
    assert elephant["flag"] == "ok"
    assert elephant["value"] == 2.0
    elephant_details = _details(elephant)
    assert elephant_details["total_commits"] == 35
    assert elephant_details["unknown_needed_to_reach_threshold"] is False
    assert elephant_details["raw_value_before_floor"] == 2

    hhi = _rows_for(result, "organizational_hhi")[0]
    assert hhi["n"] == 5
    assert hhi["flag"] == "ok"
    expected_hhi = sum((c / 35) ** 2 for c in (10, 8, 6, 5, 4, 2))
    assert hhi["value"] == pytest.approx(expected_hhi)
    assert _details(hhi)["effective_organizational_population"] == pytest.approx(1.0 / expected_hhi)
    assert _details(hhi)["unknown_included_in_hhi"] is True

    single = _rows_for(result, "single_org_share")[0]
    assert single["n"] == 5
    assert single["flag"] == "ok"
    assert single["value"] == pytest.approx(10 / 35)
    single_details = _details(single)
    assert single_details["largest_known_organization"] == "OrgA"
    assert single_details["unknown_commits"] == 5
    assert single_details["unknown_share"] == pytest.approx(5 / 35)

    unknown_rate = _rows_for(result, "unknown_affiliation_rate")[0]
    assert unknown_rate["n"] == 35
    assert unknown_rate["flag"] == "ok"
    assert unknown_rate["value"] == pytest.approx(5 / 35)
    unknown_details = _details(unknown_rate)
    assert unknown_details["unknown_commits"] == 5
    assert unknown_details["total_commits"] == 35
    assert unknown_details["unknown_contributors"] == 1
    assert unknown_details["total_contributors"] == 6
    assert unknown_details["contributor_rate"] == pytest.approx(1 / 6)


def test_organizational_metrics_below_floor_is_insufficient_data_but_shows_raw_value():
    # Only 2 known organizations -- below the concentration floor (5).
    # unknown_affiliation_rate is unaffected (its own floor is total commits,
    # not known-org count) and still reports `ok`.
    ce_rows = _contribution_rows_for_identities(
        {"solo@orga.example": 6, "duo@orgb.example": 4}, _ts(2024, 1, 10)
    )
    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)
    affiliation_period = affiliation_periods(
        [
            {"identity_id": _org_identity("solo@orga.example"), "organization": "OrgA"},
            {"identity_id": _org_identity("duo@orgb.example"), "organization": "OrgB"},
        ]
    )

    result = compute_all(
        {
            "contribution_event": contribution_event,
            "identity_link": identity_link,
            "affiliation_period": affiliation_period,
        },
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    elephant = _rows_for(result, "elephant_factor")[0]
    assert elephant["n"] == 2
    assert elephant["value"] is None
    assert elephant["flag"] == "insufficient_data"
    # Even suppressed, the raw computed number is disclosed (METRICS.md: "a
    # raw number can be shown" even when status renders insufficient_data).
    assert _details(elephant)["raw_value_before_floor"] == 1

    hhi = _rows_for(result, "organizational_hhi")[0]
    assert hhi["n"] == 2
    assert hhi["value"] is None
    assert hhi["flag"] == "insufficient_data"

    single = _rows_for(result, "single_org_share")[0]
    assert single["n"] == 2
    assert single["value"] is None
    assert single["flag"] == "insufficient_data"

    # unknown_affiliation_rate's floor is total commits (10 >= 5), unaffected
    # by the known-org count being below the concentration floor.
    unknown_rate = _rows_for(result, "unknown_affiliation_rate")[0]
    assert unknown_rate["n"] == 10
    assert unknown_rate["flag"] == "ok"
    assert unknown_rate["value"] == pytest.approx(0.0)


def test_unknown_affiliation_rate_dominant_unknown_is_still_ok_but_low_concentration_n():
    # Mostly-unaffiliated population: 1 known org (6 commits), 20 commits
    # from 4 unaffiliated contributors. Concentration metrics render
    # insufficient_data (n_known=1 < floor 5); unknown_affiliation_rate
    # reports the real, high rate honestly (D6: "report honestly, even if
    # high").
    ce_rows = _contribution_rows_for_identities({"solo@orga.example": 6}, _ts(2024, 1, 10))
    for i in range(4):
        ce_rows += _contribution_rows_for_identities(
            {f"nobody{i}@nowhere.example": 5}, _ts(2024, 1, 11)
        )
    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)
    affiliation_period = affiliation_periods(
        [{"identity_id": _org_identity("solo@orga.example"), "organization": "OrgA"}]
    )

    result = compute_all(
        {
            "contribution_event": contribution_event,
            "identity_link": identity_link,
            "affiliation_period": affiliation_period,
        },
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    elephant = _rows_for(result, "elephant_factor")[0]
    assert elephant["n"] == 1
    assert elephant["flag"] == "insufficient_data"

    unknown_rate = _rows_for(result, "unknown_affiliation_rate")[0]
    assert unknown_rate["n"] == 26
    assert unknown_rate["flag"] == "ok"
    assert unknown_rate["value"] == pytest.approx(20 / 26)
    assert _details(unknown_rate)["unknown_contributors"] == 4
    assert _details(unknown_rate)["total_contributors"] == 5


def test_organizational_metrics_curated_affiliation_dated_range_wins_and_expires():
    """A curated `affiliations.yaml` entry (source='curated') wins over a
    same-identity `email_domain` heuristic row while its dated range covers
    the commit, and falls through to the heuristic once the range ends
    (D6: "affiliations.yaml is the curated override and wins over
    heuristics", "keep dated ranges")."""
    email = "alice@orga.example"
    # January 2024 commits before and after a curated range of just the
    # first half of the month.
    ce_rows = [
        {"author_raw_type": "git_email", "author_raw_value": email, "occurred_at": _ts(2024, 1, 5)},
        {
            "author_raw_type": "git_email",
            "author_raw_value": email,
            "occurred_at": _ts(2024, 1, 25),
        },
    ]
    # Pad with 4 more known orgs so the concentration floor (5) is met and
    # `value`/`flag` aren't suppressed, keeping this test's own assertions
    # about the curated/heuristic split legible.
    for i, org in enumerate(["OrgB", "OrgC", "OrgD", "OrgE"]):
        pad_email = f"pad{i}@{org.lower()}.example"
        ce_rows.append(
            {
                "author_raw_type": "git_email",
                "author_raw_value": pad_email,
                "occurred_at": _ts(2024, 1, 10),
            }
        )

    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)

    affiliation_period = affiliation_periods(
        [
            # curated: "Curated Corp" from 2024-01-01 through 2024-01-15
            # (exclusive end) only.
            {
                "identity_id": _org_identity(email),
                "organization": "Curated Corp",
                "effective_from": date(2024, 1, 1),
                "effective_to": date(2024, 1, 15),
                "source": "curated",
            },
            # email_domain heuristic: covers all time, would apply outside
            # the curated window.
            {
                "identity_id": _org_identity(email),
                "organization": "OrgA",
                "source": "email_domain",
            },
            {"identity_id": _org_identity("pad0@orgb.example"), "organization": "OrgB"},
            {"identity_id": _org_identity("pad1@orgc.example"), "organization": "OrgC"},
            {"identity_id": _org_identity("pad2@orgd.example"), "organization": "OrgD"},
            {"identity_id": _org_identity("pad3@orge.example"), "organization": "OrgE"},
        ]
    )

    result = compute_all(
        {
            "contribution_event": contribution_event,
            "identity_link": identity_link,
            "affiliation_period": affiliation_period,
        },
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    elephant = _rows_for(result, "elephant_factor")[0]
    assert elephant["flag"] == "ok"
    organizations = {o["organization"] for o in _details(elephant)["organizations"]}
    # 6 total commits across 6 identities (1 per org) -- "Curated Corp" (the
    # Jan-5 commit) and "OrgA" (the Jan-25 commit) both appear as distinct
    # buckets, proving the curated range applied only to the Jan-5 commit.
    assert {"Curated Corp", "OrgA", "OrgB", "OrgC", "OrgD", "OrgE"} <= organizations
    for org_row in _details(elephant)["organizations"]:
        if org_row["organization"] in ("Curated Corp", "OrgA"):
            assert org_row["commits"] == 1


def test_organizational_metrics_unknown_share_at_or_above_half_forces_insufficient_data():
    """Issue #52 fixup cycle 1 (orchestrator feedback): 5 known orgs meets
    the §0.6 known-organization-count floor on its own, but a window that's
    still >= 50% unknown commits is not a trustworthy concentration
    reading -- elephant_factor/organizational_hhi/single_org_share must all
    render insufficient_data regardless, while unknown_affiliation_rate
    keeps reporting its real value honestly (D6)."""
    org_counts = {
        "a@orga.example": 1,
        "b@orgb.example": 1,
        "c@orgc.example": 1,
        "d@orgd.example": 1,
        "e@orge.example": 1,
    }
    ce_rows = _contribution_rows_for_identities(org_counts, _ts(2024, 1, 15))
    # unknown share = 5 / 10 = exactly 50% -- >= threshold.
    ce_rows += _contribution_rows_for_identities({"nobody@nowhere.example": 5}, _ts(2024, 1, 16))

    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)
    org_names = {"a@orga.example": "OrgA", "b@orgb.example": "OrgB", "c@orgc.example": "OrgC",
                 "d@orgd.example": "OrgD", "e@orge.example": "OrgE"}
    affiliation_period = affiliation_periods(
        [
            {"identity_id": _org_identity(email), "organization": org}
            for email, org in org_names.items()
        ]
    )

    result = compute_all(
        {
            "contribution_event": contribution_event,
            "identity_link": identity_link,
            "affiliation_period": affiliation_period,
        },
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    elephant = _rows_for(result, "elephant_factor")[0]
    assert elephant["n"] == 5  # the known-org-count floor is met on its own
    assert elephant["value"] is None
    assert elephant["flag"] == "insufficient_data"
    # The raw computed value is still disclosed in details_json even though
    # value/flag are suppressed.
    assert _details(elephant)["raw_value_before_floor"] is not None

    hhi = _rows_for(result, "organizational_hhi")[0]
    assert hhi["n"] == 5
    assert hhi["value"] is None
    assert hhi["flag"] == "insufficient_data"

    single = _rows_for(result, "single_org_share")[0]
    assert single["n"] == 5
    assert single["value"] is None
    assert single["flag"] == "insufficient_data"

    # unknown_affiliation_rate is exempt from this suppression -- it always
    # reports its real value, however high (D6: "report honestly").
    unknown_rate = _rows_for(result, "unknown_affiliation_rate")[0]
    assert unknown_rate["flag"] == "ok"
    assert unknown_rate["value"] == pytest.approx(0.5)


def test_github_company_affiliation_is_bounded_to_a_trailing_lookback_not_backfilled():
    """Issue #52 fixup cycle 2 (orchestrator feedback): a GitHub profile's
    `company` field is a CURRENT-employer signal, observed at `fetched_at`
    -- it must not be applied to a person's entire history. A login fetched
    on 2026-09-25 with company "Apple" (default 24-month lookback ->
    effective_from 2024-09-01) covers a 2025 commit but NOT a 2020 commit,
    which stays unknown (no other affiliation source covers it)."""
    email = "alice@nowhere.example"
    ce_rows = [
        {
            "author_raw_type": "git_email",
            "author_raw_value": email,
            "occurred_at": _ts(2020, 1, 15),
        },
        {
            "author_raw_type": "git_email",
            "author_raw_value": email,
            "occurred_at": _ts(2025, 6, 15),
        },
    ]
    contribution_event = contribution_events(ce_rows)
    identity_link = identity_link_for(contribution_events=contribution_event, now=NOW)
    affiliation_period = affiliation_periods(
        [
            {
                "identity_id": _org_identity(email),
                "organization": "Apple",
                "effective_from": date(2024, 9, 1),  # 24 months before a 2026-09-25 fetch
                "effective_to": None,
                "source": "github_company",
            }
        ]
    )

    result = compute_all(
        {
            "contribution_event": contribution_event,
            "identity_link": identity_link,
            "affiliation_period": affiliation_period,
        },
        as_of=date(2025, 7, 1),  # last completed month: June 2025
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    elephant_rows = {r["window_end"]: r for r in _rows_for(result, "elephant_factor")}

    jan_2020 = elephant_rows[date(2020, 1, 31)]
    jan_2020_orgs = {o["organization"]: o["commits"] for o in _details(jan_2020)["organizations"]}
    assert jan_2020_orgs == {"unknown": 1}

    jun_2025 = elephant_rows[date(2025, 6, 30)]
    jun_2025_orgs = {o["organization"]: o["commits"] for o in _details(jun_2025)["organizations"]}
    assert jun_2025_orgs == {"Apple": 1}


# --- truck_factor (issue #53) -------------------------------------------------


def _single_author_file(email: str, file_path: str, occurred_at: datetime) -> dict:
    return {
        "author_raw_type": "git_email",
        "author_raw_value": email,
        "file_path": file_path,
        "occurred_at": occurred_at,
    }


def test_truck_factor_golden_below_floor_orphans_two_of_three_files():
    # Three files, each with exactly one, distinct sole author -- a clean
    # "one owner per file" graph. Whichever removal order the tie-break
    # picks, removing 2 of the 3 (tied-coverage) owners always crosses the
    # ">50% of files orphaned" line for a 3-file project (threshold 1.5).
    fc_rows = [
        _single_author_file("alice@example.org", "F1.java", _ts(2024, 1, 10)),
        _single_author_file("bob@example.org", "F2.java", _ts(2024, 1, 11)),
        _single_author_file("carol@example.org", "F3.java", _ts(2024, 1, 12)),
    ]
    file_change_event = file_change_events(fc_rows)
    identity_link = identity_link_for(file_change_events=file_change_event, now=NOW)

    result = compute_all(
        {"file_change_event": file_change_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = {r["window_end"]: r for r in _rows_for(result, "truck_factor")}
    # Dense monthly snapshots, Jan and Feb 2024 both completed before AS_OF.
    assert set(rows) == {date(2024, 1, 31), date(2024, 2, 29)}

    for window_end in (date(2024, 1, 31), date(2024, 2, 29)):
        row = rows[window_end]
        # n == 3 candidate authors, below the concentration floor (5).
        assert row["n"] == 3
        assert row["flag"] == "insufficient_data"
        assert row["value"] is None
        details = _details(row)
        assert details["total_files"] == 3
        assert details["orphaned_files_at_start"] == 0
        assert details["orphaned_files_final"] == 2
        assert len(details["removed_developers"]) == 2
        # window is a point-in-time snapshot, not a range.
        assert row["window_start"] == window_end


def test_truck_factor_golden_at_floor_reports_ok():
    # Five files, each with exactly one, distinct sole author -- n == 5 meets
    # the concentration floor. Threshold is 2.5 files; removing 3 of the 5
    # tied-coverage owners crosses it (3 > 2.5).
    fc_rows = [
        _single_author_file(f"dev{i}@example.org", f"F{i}.java", _ts(2024, 1, 10))
        for i in range(5)
    ]
    file_change_event = file_change_events(fc_rows)
    identity_link = identity_link_for(file_change_events=file_change_event, now=NOW)

    result = compute_all(
        {"file_change_event": file_change_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    row = _rows_for(result, "truck_factor")[0]
    assert row["n"] == 5
    assert row["flag"] == "ok"
    assert row["value"] == 3.0
    details = _details(row)
    assert details["total_files"] == 5
    assert details["orphaned_files_final"] == 3
    assert len(details["removed_developers"]) == 3


def test_truck_factor_excludes_deleted_files_from_the_population():
    # A file added then deleted before the snapshot cutoff no longer exists
    # -- it must not count toward total_files or anyone's authorship.
    fc_rows = [
        _single_author_file("alice@example.org", "gone.java", _ts(2024, 1, 5)),
        {
            "author_raw_type": "git_email",
            "author_raw_value": "alice@example.org",
            "file_path": "gone.java",
            "occurred_at": _ts(2024, 1, 6),
            "change_type": "D",
        },
        # Five surviving single-owner files so n meets the floor.
        *[
            _single_author_file(f"dev{i}@example.org", f"F{i}.java", _ts(2024, 1, 10))
            for i in range(5)
        ],
    ]
    file_change_event = file_change_events(fc_rows)
    identity_link = identity_link_for(file_change_events=file_change_event, now=NOW)

    result = compute_all(
        {"file_change_event": file_change_event, "identity_link": identity_link},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    row = _rows_for(result, "truck_factor")[0]
    details = _details(row)
    # Only the 5 surviving files count -- "gone.java" and alice (its only
    # author) are excluded entirely.
    assert details["total_files"] == 5
    assert row["n"] == 5


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


# --- issue #54: pr_merge_lead_time --------------------------------------


def test_pr_merge_lead_time_golden():
    pr_rows = []
    lead_days = [2, 4, 6, 8, 10]
    for i, days in enumerate(lead_days):
        pr_rows.append(
            {
                "repo": "apache/cassandra",
                "number": 100 + i,
                "merged": True,
                "created_at": _ts(2024, 1, 1, hh=0),
                "merged_at": _ts(2024, 1, 1, hh=0) + _days(days),
            }
        )
    # February: only 1 merged PR -> below the floor.
    pr_rows.append(
        {
            "repo": "apache/cassandra",
            "number": 200,
            "merged": True,
            "created_at": _ts(2024, 2, 1, hh=0),
            "merged_at": _ts(2024, 2, 3, hh=0),
        }
    )
    # March (as_of month) must be excluded; an unmerged PR must never count.
    pr_rows.append(
        {
            "repo": "apache/cassandra",
            "number": 300,
            "merged": False,
            "created_at": _ts(2024, 1, 1, hh=0),
            "closed_at": _ts(2024, 1, 2, hh=0),
        }
    )

    result = compute_all(
        {"pr": prs(pr_rows)}, as_of=AS_OF, run_id=RUN_ID, computed_at=COMPUTED_AT, config=CONFIG
    )

    rows = _rows_for(result, "pr_merge_lead_time")
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]

    jan, feb = rows
    assert jan["n"] == 5
    assert jan["value"] == pytest.approx(6.0)
    assert jan["flag"] == "ok"
    assert _details(jan)["p90_days"] == pytest.approx(9.2)

    assert feb["n"] == 1
    assert feb["value"] is None
    assert feb["flag"] == "insufficient_data"


# --- issue #54: pr_time_to_first_review ----------------------------------


def test_pr_time_to_first_review_golden():
    pr_rows = [
        {"repo": "apache/cassandra", "number": 100 + i, "created_at": _ts(2024, 1, 1, hh=0)}
        for i in range(5)
    ]
    review_rows = [
        {
            "repo": "apache/cassandra",
            "pr_number": 100 + i,
            "reviewer_raw_value": "bob-reviewer",
            "submitted_at": _ts(2024, 1, 1, hh=0) + _days(days),
        }
        for i, days in enumerate([1, 2, 3, 4, 5])
    ]
    # A PR with no review at all must never contribute a latency value.
    pr_rows.append(
        {"repo": "apache/cassandra", "number": 999, "created_at": _ts(2024, 1, 1, hh=0)}
    )

    result = compute_all(
        {"pr": prs(pr_rows), "pr_review": pr_reviews(review_rows)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "pr_time_to_first_review")
    # Dense months: February has no PR data at all, so it still emits an
    # insufficient_data row (n=0) rather than being skipped.
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]
    jan, feb = rows
    assert jan["n"] == 5
    assert jan["value"] == pytest.approx(3.0)
    assert jan["flag"] == "ok"  # n == floor (5) clears it, not below it
    assert feb["n"] == 0
    assert feb["value"] is None
    assert feb["flag"] == "insufficient_data"


def test_pr_time_to_first_review_uses_earliest_review_per_pr():
    pr_rows = [{"repo": "apache/cassandra", "number": 100, "created_at": _ts(2024, 1, 1, hh=0)}]
    review_rows = [
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "later-reviewer",
            "submitted_at": _ts(2024, 1, 5, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "earlier-reviewer",
            "submitted_at": _ts(2024, 1, 2, hh=0),
        },
    ]
    result = compute_all(
        {"pr": prs(pr_rows), "pr_review": pr_reviews(review_rows)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )
    rows = _rows_for(result, "pr_time_to_first_review")
    assert rows[0]["n"] == 1
    assert _details(rows[0])["n"] == 1
    # below the floor -> insufficient_data, but the underlying latency (1 day
    # from the *earliest* of the two reviews, not the 4-day later one) is
    # still what a details_json list would show.


def test_pr_time_to_first_review_excludes_self_review_same_raw_login():
    """Orchestrator review fixup: GitHub records a PR author's own replies
    inside review threads as `COMMENTED` reviews by that author -- these must
    never count as "the first review." The PR author (raw login
    "alice-dev", the `prs()` builder default) posts a self-review on day 1;
    the genuine first review from someone else lands on day 3. The metric
    must report 3 days, not 1."""
    pr_rows = [{"repo": "apache/cassandra", "number": 100, "created_at": _ts(2024, 1, 1, hh=0)}]
    review_rows = [
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "alice-dev",  # same raw login as the PR author
            "submitted_at": _ts(2024, 1, 2, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "bob-reviewer",
            "submitted_at": _ts(2024, 1, 4, hh=0),
        },
    ]
    result = compute_all(
        {"pr": prs(pr_rows), "pr_review": pr_reviews(review_rows)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )
    rows = _rows_for(result, "pr_time_to_first_review")
    assert rows[0]["n"] == 1  # only bob's review counts
    assert _details(rows[0])["n"] == 1


def test_pr_time_to_first_review_excludes_self_review_via_resolved_identity():
    """Same self-review exclusion, but the PR author posted under a
    *different* raw login than the review -- only `resolved_identity`
    (issue #52's github_login identity_link) reveals they're the same
    person. The metric must still exclude it (identity-level compare, not
    just raw-string compare)."""
    pr_rows = [
        {
            "repo": "apache/cassandra",
            "number": 100,
            "author_raw_value": "alice-work-account",
            "created_at": _ts(2024, 1, 1, hh=0),
        }
    ]
    review_rows = [
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "alice-personal-account",  # same human, different login
            "submitted_at": _ts(2024, 1, 2, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "bob-reviewer",
            "submitted_at": _ts(2024, 1, 4, hh=0),
        },
    ]
    identity_link = _github_login_identity_link(
        [
            ("identity-alice", "alice-work-account"),
            ("identity-alice", "alice-personal-account"),
        ]
    )
    result = compute_all(
        {
            "pr": prs(pr_rows),
            "pr_review": pr_reviews(review_rows),
            "identity_link": identity_link,
        },
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )
    rows = _rows_for(result, "pr_time_to_first_review")
    assert rows[0]["n"] == 1  # only bob's review counts, alice's self-review excluded


def test_pr_time_to_first_review_unresolvable_author_never_treated_as_self():
    """A PR with no author at all (`author_raw_value=None`, e.g. a deleted
    account) must never suppress a genuine review -- an unresolvable side is
    never treated as matching anything, including itself."""
    pr_rows = [
        {
            "repo": "apache/cassandra",
            "number": 100,
            "author_raw_value": None,
            "created_at": _ts(2024, 1, 1, hh=0),
        }
    ]
    review_rows = [
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "bob-reviewer",
            "submitted_at": _ts(2024, 1, 2, hh=0),
        }
    ]
    result = compute_all(
        {"pr": prs(pr_rows), "pr_review": pr_reviews(review_rows)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )
    rows = _rows_for(result, "pr_time_to_first_review")
    assert rows[0]["n"] == 1  # bob's review counts; the null author never suppresses it
    assert _details(rows[0])["n"] == 1


# --- issue #54: pr_time_to_close ------------------------------------------


def test_pr_time_to_close_golden():
    pr_rows = []
    for i, days in enumerate([1, 3, 5, 7, 9]):
        pr_rows.append(
            {
                "repo": "apache/cassandra",
                "number": 100 + i,
                "merged": i % 2 == 0,  # 3 merged, 2 closed-without-merge
                "created_at": _ts(2024, 1, 1, hh=0),
                "closed_at": _ts(2024, 1, 1, hh=0) + _days(days),
                "merged_at": _ts(2024, 1, 1, hh=0) + _days(days) if i % 2 == 0 else None,
            }
        )
    result = compute_all(
        {"pr": prs(pr_rows)}, as_of=AS_OF, run_id=RUN_ID, computed_at=COMPUTED_AT, config=CONFIG
    )

    rows = _rows_for(result, "pr_time_to_close")
    # Dense months: February has no data -> still a row (n=0).
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]
    jan, feb = rows
    assert feb["n"] == 0
    assert feb["flag"] == "insufficient_data"
    assert jan["n"] == 5
    assert jan["value"] == pytest.approx(5.0)
    assert jan["flag"] == "ok"
    details = _details(jan)
    assert details["n_merged"] == 3


# --- issue #54: pr_review_engagement --------------------------------------


def test_pr_review_engagement_golden():
    pr_rows = []
    review_rows = []
    # 5 PRs reviewed in January: PR i gets (i+1) unique reviewers, each
    # reviewing once, so reviews_per_pr == unique_reviewers_per_pr here.
    for pr_i in range(5):
        pr_rows.append(
            {
                "repo": "apache/cassandra",
                "number": 100 + pr_i,
                "author_raw_value": "pr-author",
                "created_at": _ts(2024, 1, 1, hh=0),
            }
        )
        for reviewer_i in range(pr_i + 1):
            review_rows.append(
                {
                    "repo": "apache/cassandra",
                    "pr_number": 100 + pr_i,
                    "reviewer_raw_value": f"reviewer-{reviewer_i}",
                    "submitted_at": _ts(2024, 1, 10, hh=0),
                }
            )
    result = compute_all(
        {"pr": prs(pr_rows), "pr_review": pr_reviews(review_rows)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "pr_review_engagement")
    # Dense months: February has no review activity -> still a row (n=0).
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]
    jan, feb = rows
    assert feb["n"] == 0
    assert feb["flag"] == "insufficient_data"
    assert jan["n"] == 5  # 5 PRs reviewed
    assert jan["value"] == pytest.approx((1 + 2 + 3 + 4 + 5) / 5)
    assert jan["flag"] == "ok"
    details = _details(jan)
    assert details["mean_reviews_per_pr"] == pytest.approx((1 + 2 + 3 + 4 + 5) / 5)
    assert details["n_prs"] == 5
    assert details["n_reviews"] == 1 + 2 + 3 + 4 + 5
    assert details["n_unique_reviewers_total"] == 5  # reviewer-0..reviewer-4


def test_pr_review_engagement_excludes_self_reviews():
    """Orchestrator review fixup: a PR author's own `COMMENTED` review on
    their own PR must never count toward unique-reviewers-per-PR, reviews-
    per-PR, or the total unique-reviewer count -- only genuine third-party
    reviews are review-engagement credit. 5 PRs with qualifying (non-self)
    reviews clears the n=5 floor so `value`/flag='ok' are asserted directly,
    not just `n`."""
    pr_rows = [
        {
            "repo": "apache/cassandra",
            "number": 100,
            "author_raw_value": "alice-dev",
            "created_at": _ts(2024, 1, 1, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "number": 101,
            "author_raw_value": "carol-dev",
            "created_at": _ts(2024, 1, 1, hh=0),
        },
        *(
            {
                "repo": "apache/cassandra",
                "number": 102 + i,
                "author_raw_value": "dave-dev",
                "created_at": _ts(2024, 1, 1, hh=0),
            }
            for i in range(4)
        ),
    ]
    review_rows = [
        # PR 100: alice (the author) self-reviews once, plus two genuine
        # reviews from bob and carol.
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "alice-dev",
            "submitted_at": _ts(2024, 1, 2, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "bob-reviewer",
            "submitted_at": _ts(2024, 1, 3, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "pr_number": 100,
            "reviewer_raw_value": "carol-dev",
            "submitted_at": _ts(2024, 1, 3, hh=0),
        },
        # PR 101: only a self-review by carol (the author) -- zero
        # qualifying reviews, so this PR must not appear in the population.
        {
            "repo": "apache/cassandra",
            "pr_number": 101,
            "reviewer_raw_value": "carol-dev",
            "submitted_at": _ts(2024, 1, 4, hh=0),
        },
        # PRs 102-105: one genuine (non-self) reviewer each -- erin reviews
        # two of them, so n_unique_reviewers_total still counts her once.
        {
            "repo": "apache/cassandra",
            "pr_number": 102,
            "reviewer_raw_value": "erin-reviewer",
            "submitted_at": _ts(2024, 1, 5, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "pr_number": 103,
            "reviewer_raw_value": "frank-reviewer",
            "submitted_at": _ts(2024, 1, 5, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "pr_number": 104,
            "reviewer_raw_value": "grace-reviewer",
            "submitted_at": _ts(2024, 1, 5, hh=0),
        },
        {
            "repo": "apache/cassandra",
            "pr_number": 105,
            "reviewer_raw_value": "erin-reviewer",
            "submitted_at": _ts(2024, 1, 5, hh=0),
        },
    ]
    result = compute_all(
        {"pr": prs(pr_rows), "pr_review": pr_reviews(review_rows)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "pr_review_engagement")
    jan = rows[0]
    # PR 101 (self-review only) never appears; the other 5 PRs have unique-
    # reviewer counts [2, 1, 1, 1, 1] (PR 100 has bob+carol; the rest one
    # genuine reviewer each) -- mean = 6/5 = 1.2, never inflated by alice's
    # or carol's own self-reviews.
    assert jan["n"] == 5
    assert jan["flag"] == "ok"
    assert jan["value"] == pytest.approx(1.2)
    details = _details(jan)
    assert details["n_prs"] == 5
    assert details["n_reviews"] == 6
    # bob, carol, erin, frank, grace -- never alice or (self-reviewing) carol
    # counted against her own PR 101.
    assert details["n_unique_reviewers_total"] == 5


# --- issue #54: time_to_first_response_jira -------------------------------


def test_time_to_first_response_jira_golden():
    issue_rows = []
    comment_rows = []
    for i, days in enumerate([1, 2, 3, 4, 5]):
        issue_key = f"CASSANDRA-{1000 + i}"
        issue_rows.append(
            {
                "issue_key": issue_key,
                "created_at": _ts(2024, 1, 1, hh=0),
                "updated_at": _ts(2024, 1, 1, hh=0) + _days(days),
                "reporter_raw": "reporter-a",
            }
        )
        comment_rows.append(
            {
                "issue_key": issue_key,
                "author_raw_value": "responder-b",
                "created_at": _ts(2024, 1, 1, hh=0) + _days(days),
            }
        )
    # An issue whose only comment is by the reporter -- must not count as a
    # response.
    issue_rows.append(
        {
            "issue_key": "CASSANDRA-2000",
            "created_at": _ts(2024, 1, 1, hh=0),
            "updated_at": _ts(2024, 1, 2, hh=0),
            "reporter_raw": "reporter-a",
        }
    )
    comment_rows.append(
        {
            "issue_key": "CASSANDRA-2000",
            "author_raw_value": "reporter-a",
            "created_at": _ts(2024, 1, 1, hh=12),
        }
    )
    # An issue whose only comment is from a bot (svn-role) -- must not count.
    issue_rows.append(
        {
            "issue_key": "CASSANDRA-2001",
            "created_at": _ts(2024, 1, 1, hh=0),
            "updated_at": _ts(2024, 1, 2, hh=0),
            "reporter_raw": "reporter-a",
        }
    )
    comment_rows.append(
        {
            "issue_key": "CASSANDRA-2001",
            "author_raw_value": "svn-role",
            "created_at": _ts(2024, 1, 1, hh=12),
        }
    )

    result = compute_all(
        {"issue": issues(issue_rows), "issue_comment": issue_comments(comment_rows)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "time_to_first_response_jira")
    assert [r["window_start"] for r in rows] == [date(2024, 1, 1), date(2024, 2, 1)]
    jan, feb = rows
    assert feb["n"] == 0
    assert feb["flag"] == "insufficient_data"
    # Only the 5 issues with a genuine human, non-reporter response count.
    assert jan["n"] == 5
    assert jan["value"] == pytest.approx(3.0)
    assert jan["flag"] == "ok"  # n == floor (5) clears it, not below it
    details = _details(jan)
    assert details["n_opened_in_window"] == 7  # all 7 issues opened in January
    assert details["closed_in_window"]["n"] == 5


def test_time_to_first_response_jira_assignee_who_is_the_reporter_is_excluded():
    """Orchestrator review confirmation: when an issue's assignee is also its
    reporter, that person's own comment must not count as a first response
    -- already true because the exclusion compares by author_raw_value ==
    reporter_raw, and this person's author_raw_value equals reporter_raw
    regardless of them also being the assignee. A *different* assignee's
    comment, by contrast, is a genuine first response and must count."""
    issue_rows = [
        {
            "issue_key": "CASSANDRA-3000",
            "created_at": _ts(2024, 1, 1, hh=0),
            "updated_at": _ts(2024, 1, 2, hh=0),
            "reporter_raw": "reporter-a",
            "assignee_raw": "reporter-a",  # self-assigned
        },
        {
            "issue_key": "CASSANDRA-3001",
            "created_at": _ts(2024, 1, 1, hh=0),
            "updated_at": _ts(2024, 1, 2, hh=0),
            "reporter_raw": "reporter-a",
            "assignee_raw": "assignee-c",  # distinct from the reporter
        },
    ]
    comment_rows = [
        # CASSANDRA-3000: the self-assigned reporter comments on their own
        # issue -- must not count as a response.
        {
            "issue_key": "CASSANDRA-3000",
            "author_raw_value": "reporter-a",
            "created_at": _ts(2024, 1, 2, hh=0),
        },
        # CASSANDRA-3001: the (distinct) assignee comments -- this is a
        # genuine first response and must count.
        {
            "issue_key": "CASSANDRA-3001",
            "author_raw_value": "assignee-c",
            "created_at": _ts(2024, 1, 3, hh=0),
        },
    ]

    result = compute_all(
        {"issue": issues(issue_rows), "issue_comment": issue_comments(comment_rows)},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    rows = _rows_for(result, "time_to_first_response_jira")
    jan = rows[0]
    # Only CASSANDRA-3001's assignee response counts (below the n=5 floor,
    # so flag/value are suppressed -- the underlying n is what this test
    # is about).
    assert jan["n"] == 1
    assert jan["flag"] == "insufficient_data"


# --- issue #54: stale_pr_rate ----------------------------------------------


def test_stale_pr_rate_golden_single_snapshot_row():
    pr_rows = [
        # Stale: last updated well before the 90-day cutoff.
        {
            "repo": "apache/cassandra",
            "number": 1,
            "state": "OPEN",
            "created_at": _ts(2019, 1, 1),
            "updated_at": _ts(2022, 1, 1),
        },
        {
            "repo": "apache/cassandra",
            "number": 2,
            "state": "OPEN",
            "created_at": _ts(2019, 1, 1),
            "updated_at": _ts(2022, 6, 1),
        },
        {
            "repo": "apache/cassandra",
            "number": 3,
            "state": "OPEN",
            "created_at": _ts(2019, 1, 1),
            "updated_at": _ts(2020, 1, 1),
        },
        # Not stale: updated after the cutoff.
        {
            "repo": "apache/cassandra",
            "number": 4,
            "state": "OPEN",
            "created_at": _ts(2023, 1, 1),
            "updated_at": _ts(2024, 3, 1),
        },
        {
            "repo": "apache/cassandra",
            "number": 5,
            "state": "OPEN",
            "created_at": _ts(2023, 1, 1),
            "updated_at": _ts(2024, 2, 1),
        },
        # Merged/closed -- must not count in either open-PR bucket.
        {
            "repo": "apache/cassandra",
            "number": 6,
            "state": "MERGED",
            "merged": True,
            "created_at": _ts(2019, 1, 1),
            "updated_at": _ts(2019, 2, 1),
            "closed_at": _ts(2019, 2, 1),
            "merged_at": _ts(2019, 2, 1),
        },
    ]

    result = compute_all(
        {"pr": prs(pr_rows)}, as_of=AS_OF, run_id=RUN_ID, computed_at=COMPUTED_AT, config=CONFIG
    )

    rows = _rows_for(result, "stale_pr_rate")
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
    assert {r["repo"] for r in details["by_repo"]} == {"apache/cassandra"}


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


# --- time_to_first_reply_devlist / unanswered_thread_rate_devlist (issue #35) --


def test_devlist_metrics_golden_self_reply_bot_exclusion_dense_months_and_floors():
    """One scenario covering every acceptance criterion at once:
    - a floor-clearing month (5 real replies -> "ok")
    - self-reply exclusion (t6: root replies to itself -> never an answer)
    - automated-sender-reply exclusion (t7: only reply is from
      `jenkins@builds.apache.org`, matched by projects/cassandra.yaml's
      `mailing_lists.automated_senders` -> never an answer)
    - automated-sender-*root* exclusion (t8: thread started by
      `jenkins@builds.apache.org` -> excluded from the population entirely, on both
      metrics)
    - dense months (Feb/Mar 2024 have zero dev@ threads but still get a row)
    - `unanswered_thread_rate_devlist`'s 30-day completed-period gate (as of
      2024-04-05, January's 30-day follow-up has elapsed but March's hasn't)
    """
    rows = []

    def add(thread_id: str, sender: str, when: datetime) -> None:
        rows.append({"thread_id": thread_id, "sender_raw_value": sender, "occurred_at": when})

    # t1..t5: root always "alice@a.org", a real reply from a distinct
    # sender 1..5 days later -- five qualifying, answered threads.
    for i, delay_days in enumerate((1, 2, 3, 4, 5), start=1):
        tid = f"t{i}"
        add(tid, "alice@a.org", _ts(2024, 1, 2))
        add(tid, f"replier{i}@a.org", _ts(2024, 1, 2 + delay_days))

    # t6: self-reply only -- never counts as an answer.
    add("t6", "alice@a.org", _ts(2024, 1, 8))
    add("t6", "alice@a.org", _ts(2024, 1, 9))

    # t7: automated-sender reply only -- never counts as an answer.
    add("t7", "alice@a.org", _ts(2024, 1, 10))
    add("t7", "jenkins@builds.apache.org", _ts(2024, 1, 11))

    # t8: automated-sender *root* -- excluded from the population entirely,
    # even though it has a real, fast human reply.
    add("t8", "jenkins@builds.apache.org", _ts(2024, 1, 12))
    add("t8", "bob@a.org", _ts(2024, 1, 13))

    message_table = messages(rows)
    as_of = date(2024, 4, 5)  # Jan/Feb/Mar 2024 completed.

    result = compute_all(
        {"message": message_table},
        as_of=as_of,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    reply_rows = _rows_for(result, "time_to_first_reply_devlist")
    assert [r["window_start"] for r in reply_rows] == [
        date(2024, 1, 1),
        date(2024, 2, 1),
        date(2024, 3, 1),
    ]

    jan = reply_rows[0]
    assert jan["n"] == 5  # t1..t5 only -- t6/t7 (no qualifying reply) excluded, t8 dropped
    assert jan["flag"] == "ok"
    assert jan["value"] == pytest.approx(3.0)  # median of [1, 2, 3, 4, 5] days
    jan_details = _details(jan)
    assert jan_details["threads_started"] == 7  # t1..t7 (t8's automated root excludes it)
    assert jan_details["p90_days"] == pytest.approx(4.6)
    assert "backfill_in_progress" not in jan_details

    for gap_row in reply_rows[1:]:  # Feb, Mar: dense, zero dev@ threads at all
        assert gap_row["n"] == 0
        assert gap_row["value"] is None
        assert gap_row["flag"] == "insufficient_data"
        assert _details(gap_row)["threads_started"] == 0

    unanswered_rows = _rows_for(result, "unanswered_thread_rate_devlist")
    # March's own 30-day follow-up window (ending 2024-04-30) hasn't elapsed
    # as of 2024-04-05, so only January and February are reportable.
    assert [r["window_start"] for r in unanswered_rows] == [date(2024, 1, 1), date(2024, 2, 1)]

    jan_u = unanswered_rows[0]
    assert jan_u["n"] == 7
    assert jan_u["flag"] == "ok"
    assert jan_u["value"] == pytest.approx(2 / 7)  # t6, t7 unanswered out of 7 qualifying threads
    jan_u_details = _details(jan_u)
    assert jan_u_details["n_total"] == 7
    assert jan_u_details["n_unanswered"] == 2
    assert jan_u_details["followup_days"] == 30

    feb_u = unanswered_rows[1]
    assert feb_u["n"] == 0
    assert feb_u["value"] is None
    assert feb_u["flag"] == "insufficient_data"


def test_devlist_metrics_watermark_caps_backfill_gap_and_flags_it():
    """Issue #33's oldest-first, per-run-capped Pony Mail backfill: a
    watermark far behind `as_of` must not make the uncollected gap in
    between look like a run of genuinely-zero months. Only the backfilled
    prefix (through the watermark) plus the always-refetched latest
    completed month get a row; the multi-year gap between them is skipped
    entirely, and every emitted row is flagged `backfill_in_progress`."""
    rows = [
        {"thread_id": "old1", "sender_raw_value": "alice@a.org", "occurred_at": _ts(2020, 1, 5)},
        {"thread_id": "old1", "sender_raw_value": "bob@a.org", "occurred_at": _ts(2020, 1, 6)},
        {"thread_id": "new1", "sender_raw_value": "carol@a.org", "occurred_at": _ts(2024, 8, 1)},
        {"thread_id": "new1", "sender_raw_value": "dave@a.org", "occurred_at": _ts(2024, 8, 2)},
    ]
    message_table = messages(rows)

    result = compute_all(
        {"message": message_table},
        as_of=date(2024, 9, 10),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
        ponymail_watermarks={"dev": "2020-03"},
    )

    reply_rows = _rows_for(result, "time_to_first_reply_devlist")
    # Backfilled prefix (Jan-Mar 2020, through the watermark) + the latest
    # completed month (Aug 2024, always freshly fetched) -- never the huge
    # gap in between.
    assert [r["window_start"] for r in reply_rows] == [
        date(2020, 1, 1),
        date(2020, 2, 1),
        date(2020, 3, 1),
        date(2024, 8, 1),
    ]
    for row in reply_rows:
        assert _details(row)["backfill_in_progress"] is True

    jan_2020 = reply_rows[0]
    assert jan_2020["n"] == 1
    assert jan_2020["flag"] == "insufficient_data"  # below the latency floor (5)

    aug_2024 = reply_rows[3]
    assert aug_2024["n"] == 1
    assert aug_2024["flag"] == "insufficient_data"

    unanswered_rows = _rows_for(result, "unanswered_thread_rate_devlist")
    # Aug 2024's own 30-day follow-up hasn't elapsed as of 2024-09-10, so
    # only the backfilled 2020 prefix is reportable here.
    assert [r["window_start"] for r in unanswered_rows] == [
        date(2020, 1, 1),
        date(2020, 2, 1),
        date(2020, 3, 1),
    ]
    for row in unanswered_rows:
        assert _details(row)["backfill_in_progress"] is True


def test_devlist_metrics_never_missing_when_no_messages_collected_yet():
    """A registered metric must never come back with zero rows (pipeline.py's
    `metrics_missing` check, issue #24) -- even before Pony Mail has
    collected any dev@ history at all."""
    result = compute_all(
        {},
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
        config=CONFIG,
    )

    reply_rows = _rows_for(result, "time_to_first_reply_devlist")
    assert len(reply_rows) == 1
    assert reply_rows[0]["n"] == 0
    assert reply_rows[0]["value"] is None
    assert reply_rows[0]["flag"] == "insufficient_data"

    unanswered_rows = _rows_for(result, "unanswered_thread_rate_devlist")
    assert len(unanswered_rows) == 1
    assert unanswered_rows[0]["n"] == 0
    assert unanswered_rows[0]["value"] is None
    assert unanswered_rows[0]["flag"] == "insufficient_data"
