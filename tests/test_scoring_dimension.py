"""Golden tests for `scoring/dimension.py` (issue #57, SCORING.md §5.3-§5.4):
the worst-key-metric dimension-status rule.

`_status` below builds a minimal `BaselineStatusResult` with only the field
`compute_dimension_status` actually reads (`status`) populated meaningfully --
mirrors SCORING.md §5.3's own worked example table (contributor
sustainability: `truck_factor` declining while `sustained_contributor_count`/
`contributor_hhi` read stable -> dimension declining, driven by
`truck_factor` alone).
"""

from __future__ import annotations

from datetime import date

from project_health.scoring.baseline import BaselineStatusResult
from project_health.scoring.dimension import compute_dimension_status

WINDOW_END = date(2025, 1, 1)


def _status(status: str) -> BaselineStatusResult:
    return BaselineStatusResult(
        metric_id="unused",
        window_end=WINDOW_END,
        current_value=None,
        baseline_median=None,
        baseline_mad=None,
        baseline_months=24,
        modified_z=None,
        status=status,
        confirmed=None,
        notable_single_month_event=None,
    )


def test_declining_key_metric_wins_even_with_other_improving_and_stable_keys():
    """SCORING.md §5.3's own worked example, transcribed: `truck_factor`
    declining, `contributor_hhi` stable, `sustained_contributor_count`
    stable -> dimension declining, driven by `truck_factor` alone."""
    result = compute_dimension_status(
        "contributor sustainability",
        {
            "truck_factor": _status("declining"),
            "contributor_hhi": _status("stable"),
            "sustained_contributor_count": _status("stable"),
        },
    )
    assert result.status == "declining"
    assert result.driven_by == ("truck_factor",)


def test_declining_wins_over_a_simultaneously_improving_key_metric():
    """A dimension never averages/votes: one declining key metric outweighs
    another key metric that is simultaneously improving."""
    result = compute_dimension_status(
        "reviewer capacity",
        {
            "reviewer_hhi": _status("declining"),
            "review_latency": _status("improving"),
            "unique_reviewers_monthly": _status("stable"),
        },
    )
    assert result.status == "declining"
    assert result.driven_by == ("reviewer_hhi",)


def test_improving_only_when_no_key_metric_is_declining():
    result = compute_dimension_status(
        "responsiveness",
        {
            "time_to_first_response_jira": _status("improving"),
            "stale_jira_rate": _status("stable"),
            "time_to_first_reply_devlist": _status("insufficient_data"),
        },
    )
    assert result.status == "improving"
    assert result.driven_by == ("time_to_first_response_jira",)


def test_stable_when_no_key_metric_is_declining_or_improving():
    result = compute_dimension_status(
        "organizational diversity",
        {"elephant_factor": _status("stable"), "organizational_hhi": _status("stable")},
    )
    assert result.status == "stable"
    assert result.driven_by == ()


def test_insufficient_data_when_every_key_metric_is_insufficient_data():
    """SCORING.md §5.4: the dimension only reads insufficient_data when *no*
    key metric has enough data to classify."""
    result = compute_dimension_status(
        "release cadence", {"release_frequency": _status("insufficient_data")}
    )
    assert result.status == "insufficient_data"
    assert result.driven_by == ()


def test_insufficient_data_when_dimension_has_no_key_metrics_at_all():
    """A dimension whose key metric(s) never produced a row this run (e.g.
    `release_frequency` isn't implemented yet) is handed an empty mapping by
    the caller -- treated the same as all-insufficient_data."""
    result = compute_dimension_status("release cadence", {})
    assert result.status == "insufficient_data"


def test_one_real_key_metric_status_is_enough_even_if_a_sibling_key_metric_is_insufficient():
    """SCORING.md §5.4: a dimension is not insufficient_data as long as at
    least one key metric has a real status, even if another key metric in
    the same dimension is insufficient_data this month."""
    result = compute_dimension_status(
        "contributor sustainability",
        {
            "truck_factor": _status("stable"),
            "contributor_hhi": _status("insufficient_data"),
            "sustained_contributor_count": _status("insufficient_data"),
        },
    )
    assert result.status == "stable"
