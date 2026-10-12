"""Tests for project_health.release_lines_model (issue #166).

Uses synthetic rows shaped exactly like the `jira_version`/
`release_blocker_issue`/`ci_job_status`/`ci_trend_point`/`release` tables'
`to_pylist()` output -- the model never touches storage or HTTP directly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from project_health.release_lines_model import build_release_line_model


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_6_0_line_full_model():
    jira_version_rows = [
        {
            "version_id": "1",
            "name": "6.0-alpha",
            "description": "Placeholder for blockers to 6.0-beta1 (unresolved tickets only)",
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
        {
            "version_id": "2",
            "name": "6.0-beta",
            "description": "Placeholder for blockers to 6.0-rc1 (unresolved tickets only)",
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
        {
            "version_id": "3",
            "name": "6.0-rc",
            "description": "Placeholder for blockers to 6.0.0 (GA) (unresolved tickets only)",
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
        {
            "version_id": "4",
            "name": "6.0.x",
            "description": None,
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
        {
            "version_id": "5",
            "name": "6.0",
            "description": None,
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
        {
            "version_id": "6",
            "name": "6.0-alpha3",
            "description": None,
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
    ]
    blocker_issue_rows = [
        {
            "version_name": "6.0-alpha",
            "issue_key": "CASSANDRA-11745",
            "summary": "Add bytes limit to queries and paging",
            "status": "Review In Progress",
            "priority": "Normal",
            "assignee_raw": "mck",
            "created_at": _dt("2020-01-01T00:00:00"),
            "updated_at": _dt("2026-10-01T00:00:00"),
        },
        {
            "version_name": "6.0-alpha",
            "issue_key": "CASSANDRA-21340",
            "summary": "GROUP BY queries silently return incomplete results",
            "status": "Open",
            "priority": "Normal",
            "assignee_raw": None,
            "created_at": _dt("2025-01-01T00:00:00"),
            "updated_at": _dt("2026-09-01T00:00:00"),
        },
        {
            "version_name": "6.0.x",
            "issue_key": "CASSANDRA-18931",
            "summary": "ci job to build and push new in-tree docker images",
            "status": "Patch Available",
            "priority": "Normal",
            "assignee_raw": "mmuzaf",
            "created_at": _dt("2023-01-01T00:00:00"),
            "updated_at": _dt("2026-09-01T00:00:00"),
        },
    ]
    ci_job_rows = [
        {
            "job_name": "Cassandra-6.0",
            "branch_line": "6.0",
            "color": "red",
            "last_build_number": 20,
            "last_build_result": "FAILURE",
            "last_build_timestamp": _dt("2026-10-10T16:06:25"),
        }
    ]
    ci_trend_rows = [
        {
            "workflow": "Cassandra-6.0",
            "branch_line": "6.0",
            "build_label": "#20",
            "build_timestamp": _dt("2026-10-10T16:06:25"),
            "test_failures": 33,
            "test_runs": 322907,
            "duration_minutes": 438.0,
        }
    ]
    release_rows = [
        {"major_minor": "6.0", "version": "6.0-alpha2", "release_date": date(2026, 8, 7)},
        {"major_minor": "6.0", "version": "6.0-alpha1", "release_date": date(2026, 3, 21)},
    ]

    views = build_release_line_model(
        active_lines=["6.0"],
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=blocker_issue_rows,
        ci_job_rows=ci_job_rows,
        ci_trend_rows=ci_trend_rows,
        release_rows=release_rows,
        jira_available=True,
    )
    # trunk (CI-only) is always appended
    assert [v.line for v in views] == ["6.0", "trunk"]
    six = views[0]

    assert [s.version_name for s in six.ladder] == ["6.0-alpha", "6.0-beta", "6.0-rc"]
    assert six.ladder[0].open_count == 2
    assert six.ladder[0].unassigned_count == 1
    assert six.ladder[1].open_count == 0
    assert six.series.version_name == "6.0.x"
    assert six.series.open_count == 1
    assert six.series.status_counts == {"Patch Available": 1}
    assert six.ga_target.version_name == "6.0"
    assert six.next_target == "6.0-alpha3"

    # timeline sorted ascending, latest_release is the most recent
    assert [t.version for t in six.timeline] == ["6.0-alpha1", "6.0-alpha2"]
    assert six.latest_release.version == "6.0-alpha2"

    assert six.ci.job_name == "Cassandra-6.0"
    assert six.ci.color == "red"
    assert six.ci.last_build_result == "FAILURE"
    assert six.ci.latest_trend_point.test_failures == 33
    assert six.ci.jenkins_available is True
    assert six.ci.butler_available is True

    trunk = views[1]
    assert trunk.jira_available is False
    assert trunk.ci.job_name is None  # no fixture row for Cassandra-trunk in this test


def test_jira_unavailable_renders_no_ladder_but_keeps_ci():
    """A total JIRA outage (never collected) must still let CI-only facts
    through -- D25/issue #166: "a source outage renders 'unavailable' for
    that block" (not the whole page)."""
    ci_job_rows = [
        {
            "job_name": "Cassandra-5.0",
            "branch_line": "5.0",
            "color": "yellow",
            "last_build_number": 678,
            "last_build_result": "UNSTABLE",
            "last_build_timestamp": _dt("2026-10-08T03:14:16"),
        }
    ]
    views = build_release_line_model(
        active_lines=["5.0"],
        jira_version_rows=[],
        blocker_issue_rows=[],
        ci_job_rows=ci_job_rows,
        ci_trend_rows=[],
        release_rows=[],
        jira_available=False,
    )
    five = views[0]
    assert five.jira_available is False
    assert five.ladder == ()
    assert five.series is None
    assert five.ci.job_name == "Cassandra-5.0"
    assert five.ci.color == "yellow"


def test_line_with_no_ladder_but_a_series_and_next_version():
    """Real live shape for already-GA'd lines like 3.0/3.11: no ladder
    placeholders with open issues worth noting, just a series backlog and
    a next-patch target."""
    jira_version_rows = [
        {
            "version_id": "1",
            "name": "3.0.x",
            "description": "Placeholder for a 3.0 series change  (unresolved tickets only)",
            "released": False,
            "release_date": None,
            "major_minor": "3.0",
        },
        {
            "version_id": "2",
            "name": "3.0.33",
            "description": None,
            "released": False,
            "release_date": None,
            "major_minor": "3.0",
        },
    ]
    views = build_release_line_model(
        active_lines=["3.0"],
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=[],
        ci_job_rows=[],
        ci_trend_rows=[],
        release_rows=[],
        jira_available=True,
    )
    three = views[0]
    assert three.ladder == ()
    assert three.series.version_name == "3.0.x"
    assert three.next_target == "3.0.33"
    assert three.total_open_blockers == 0


def test_total_open_blockers_sums_ladder_and_ga_target_not_series():
    jira_version_rows = [
        {
            "version_id": "1",
            "name": "6.0-alpha",
            "description": "Placeholder for blockers to 6.0-beta1",
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
        {
            "version_id": "2",
            "name": "6.0",
            "description": None,
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
        {
            "version_id": "3",
            "name": "6.0.x",
            "description": None,
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
    ]
    blocker_issue_rows = [
        {
            "version_name": "6.0-alpha",
            "issue_key": "CASSANDRA-1",
            "summary": "x",
            "status": "Open",
            "priority": "Normal",
            "assignee_raw": None,
            "created_at": None,
            "updated_at": None,
        },
        {
            "version_name": "6.0",
            "issue_key": "CASSANDRA-2",
            "summary": "y",
            "status": "Open",
            "priority": "Normal",
            "assignee_raw": None,
            "created_at": None,
            "updated_at": None,
        },
        {
            "version_name": "6.0.x",
            "issue_key": "CASSANDRA-3",
            "summary": "z",
            "status": "Open",
            "priority": "Normal",
            "assignee_raw": None,
            "created_at": None,
            "updated_at": None,
        },
    ]
    views = build_release_line_model(
        active_lines=["6.0"],
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=blocker_issue_rows,
        ci_job_rows=[],
        ci_trend_rows=[],
        release_rows=[],
        jira_available=True,
    )
    six = views[0]
    # 1 (ladder: 6.0-alpha) + 1 (ga_target: 6.0) = 2; series's 1 is excluded.
    assert six.total_open_blockers == 2
