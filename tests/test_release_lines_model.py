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


def test_total_open_blockers_sums_only_the_ladder_not_ga_target_or_series():
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
    # Only the ladder's own 1 (6.0-alpha) counts as a "blocker" (issue #166
    # orchestrator review of PR #167): the ga_target's 1 ticket is merely
    # targeted at 6.0, not blocking anything, and series's 1 is excluded
    # the same way it always was.
    assert six.total_open_blockers == 1
    assert six.ga_target.open_count == 1


def test_pre_release_timeline_from_jira_when_line_has_no_ga_tag_yet():
    """Orchestrator review of PR #167: a line with no GA release yet (6.0)
    still has a real release history -- its own alpha/beta/rc releases,
    which `collectors/release.py`'s git-tag table never sees (it excludes
    pre-release tags). The model must pull these from JIRA's own
    `released_versions` instead, with `release_rows` (git tags) empty."""
    jira_version_rows = [
        {
            "version_id": "1",
            "name": "6.0-alpha1",
            "description": None,
            "released": True,
            "release_date": date(2026, 3, 21),
            "major_minor": "6.0",
        },
        {
            "version_id": "2",
            "name": "6.0-alpha2",
            "description": None,
            "released": True,
            "release_date": date(2026, 8, 7),
            "major_minor": "6.0",
        },
    ]
    views = build_release_line_model(
        active_lines=["6.0"],
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=[],
        ci_job_rows=[],
        ci_trend_rows=[],
        release_rows=[],  # no GA tag yet -- empty, same as production today
        jira_available=True,
    )
    six = views[0]
    assert [t.version for t in six.timeline] == ["6.0-alpha1", "6.0-alpha2"]
    assert six.latest_release.version == "6.0-alpha2"
    assert six.latest_release.release_date == date(2026, 8, 7)


def test_pre_release_timeline_never_duplicates_ga_tag_rows():
    """A line with both GA history (git tags) and its own earlier pre-
    release history (JIRA) gets both, merged by date, with no duplicate
    rows -- the two sources are structurally disjoint (dash- vs dot-shaped
    names), never double-counted."""
    jira_version_rows = [
        {
            "version_id": "1",
            "name": "5.0-alpha2",
            "description": None,
            "released": True,
            "release_date": date(2023, 10, 30),
            "major_minor": "5.0",
        },
    ]
    release_rows = [
        {"major_minor": "5.0", "version": "5.0.9", "release_date": date(2026, 8, 7)},
        {"major_minor": "5.0", "version": "5.0.8", "release_date": date(2026, 4, 16)},
    ]
    views = build_release_line_model(
        active_lines=["5.0"],
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=[],
        ci_job_rows=[],
        ci_trend_rows=[],
        release_rows=release_rows,
        jira_available=True,
    )
    five = views[0]
    assert [t.version for t in five.timeline] == ["5.0-alpha2", "5.0.8", "5.0.9"]
    assert five.latest_release.version == "5.0.9"


def test_ladder_target_label_carried_through_to_the_view():
    """Issue #166 orchestrator review of PR #167: the summary table's
    "Blockers to next milestone" column reads each ladder step's own
    `target_label` (e.g. "beta1", "rc1", "GA") -- the view must carry it
    through from `collectors.release_lines.LadderStep`."""
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
            "name": "6.0-rc",
            "description": "Placeholder for blockers to 6.0.0 (GA) (unresolved tickets only)",
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
    ]
    views = build_release_line_model(
        active_lines=["6.0"],
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=[],
        ci_job_rows=[],
        ci_trend_rows=[],
        release_rows=[],
        jira_available=True,
    )
    six = views[0]
    labels = {s.version_name: s.target_label for s in six.ladder}
    assert labels == {"6.0-alpha": "beta1", "6.0-rc": "GA"}


def test_next_target_step_carries_its_own_open_issues():
    jira_version_rows = [
        {
            "version_id": "1",
            "name": "6.0-alpha3",
            "description": None,
            "released": False,
            "release_date": None,
            "major_minor": "6.0",
        },
    ]
    blocker_issue_rows = [
        {
            "version_name": "6.0-alpha3",
            "issue_key": "CASSANDRA-99",
            "summary": "x",
            "status": "Open",
            "priority": "Normal",
            "assignee_raw": None,
            "created_at": None,
            "updated_at": None,
        },
        {
            "version_name": "6.0-alpha3",
            "issue_key": "CASSANDRA-100",
            "summary": "y",
            "status": "Open",
            "priority": "Normal",
            "assignee_raw": "mck",
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
    assert six.next_target == "6.0-alpha3"
    assert six.next_target_step.version_name == "6.0-alpha3"
    assert six.next_target_step.open_count == 2


def test_unreleased_line_with_no_placeholders_yet_is_handled_gracefully():
    """A hypothetical 7.0 line with no ladder/series/next/released
    versions tracked at all yet must still produce a valid, empty view --
    never crash."""
    views = build_release_line_model(
        active_lines=["7.0"],
        jira_version_rows=[],
        blocker_issue_rows=[],
        ci_job_rows=[],
        ci_trend_rows=[],
        release_rows=[],
        jira_available=True,
    )
    seven = views[0]
    assert seven.line == "7.0"
    assert seven.ladder == ()
    assert seven.series is None
    assert seven.ga_target is None
    assert seven.next_target_step is None
    assert seven.timeline == ()
    assert seven.latest_release is None
    assert seven.total_open_blockers == 0
    assert seven.has_ga_release is False


def test_has_ga_release_true_once_a_ga_tag_exists_for_the_line():
    """Orchestrator review of PR #167, second round: a line with a row in
    the git-tag `release` table has shipped a GA -- `has_ga_release` must
    be True, even though its own legacy alpha/beta/rc ladder placeholders
    are still present in JIRA (unrelated to this flag; the page decides
    what to do with them)."""
    jira_version_rows = [
        {
            "version_id": "1",
            "name": "5.0-alpha",
            "description": "Placeholder for blockers to 5.0-beta1 (unresolved tickets only)",
            "released": False,
            "release_date": None,
            "major_minor": "5.0",
        },
    ]
    release_rows = [
        {"major_minor": "5.0", "version": "5.0.9", "release_date": date(2026, 8, 7)},
    ]
    views = build_release_line_model(
        active_lines=["5.0"],
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=[],
        ci_job_rows=[],
        ci_trend_rows=[],
        release_rows=release_rows,
        jira_available=True,
    )
    five = views[0]
    assert five.has_ga_release is True
    # The ladder's own real data is still carried -- suppression is a
    # page-level display decision, not a data-loss one.
    assert [s.version_name for s in five.ladder] == ["5.0-alpha"]


def test_has_ga_release_false_for_a_line_with_no_ga_tag_yet():
    """6.0 has no row in the git-tag `release` table (no GA shipped yet)
    -- `has_ga_release` must be False even though it has its own released
    pre-release history (6.0-alpha1/alpha2)."""
    jira_version_rows = [
        {
            "version_id": "1",
            "name": "6.0-alpha1",
            "description": None,
            "released": True,
            "release_date": date(2026, 3, 21),
            "major_minor": "6.0",
        },
    ]
    views = build_release_line_model(
        active_lines=["6.0"],
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=[],
        ci_job_rows=[],
        ci_trend_rows=[],
        release_rows=[],  # no GA tag
        jira_available=True,
    )
    six = views[0]
    assert six.has_ga_release is False
