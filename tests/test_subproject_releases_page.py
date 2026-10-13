"""Tests for project_health.site.subproject_releases_page (issue #175).

Pure page-context tests, same "write a real raw-table-shaped partition via
`storage.write_partition`, then call the builder and read the dict" pattern
`tests/test_site_releases.py` uses for Cassandra's own `build_releases_
context` -- no HTTP, no template rendering here (that's covered by
`test_site_releases.py`'s own subproject-rendering additions).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa

from project_health import storage
from project_health.schema import get_schema, validate
from project_health.site.subproject_releases_page import build_subproject_releases_context

RUN_ID = "2026-10-12-abc123"
BUILD_TIME = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _table(name: str, rows: list[dict]) -> pa.Table:
    schema = get_schema(name)
    if not rows:
        return schema.empty_table()
    return validate(name, pa.Table.from_pylist(rows, schema=schema))


def _write_release_rows(data_dir: Path, rows: list[dict]) -> None:
    storage.write_partition(
        data_dir, "release", "release", BUILD_TIME.date(), RUN_ID, _table("release", rows)
    )


def _write_jira_rows(data_dir: Path, rows: list[dict]) -> None:
    storage.write_partition(
        data_dir,
        "subproject_releases",
        "subproject_release_version",
        BUILD_TIME.date(),
        RUN_ID,
        _table("subproject_release_version", rows),
    )


def _write_ci_rows(data_dir: Path, rows: list[dict]) -> None:
    storage.write_partition(
        data_dir,
        "subproject_releases",
        "ci_workflow_run",
        BUILD_TIME.date(),
        RUN_ID,
        _table("ci_workflow_run", rows),
    )


def _ga_row(version: str, major_minor: str, release_date: date, repo: str = "apache/x") -> dict:
    return {
        "release_id": f"x-{version}",
        "tag_name": f"x-{version}",
        "version": version,
        "major_minor": major_minor,
        "release_date": release_date,
        "release_date_source": "git_tag",
        "archive_verified": None,
        "archive_date": None,
        "repo": repo,
        "source_snapshot_id": f"{RUN_ID}:release",
        "collected_at": BUILD_TIME,
    }


def _jira_row(name: str, open_count: int, total_count: int, **overrides) -> dict:
    row = {
        "version_id": f"id-{name}",
        "name": name,
        "description": None,
        "release_date": None,
        "major_minor": None,
        "open_count": open_count,
        "total_count": total_count,
        "issues_url": f"https://issues.apache.org/jira/issues/?jql=fixVersion%3D{name}",
        "source_snapshot_id": f"{RUN_ID}:subproject_releases",
        "collected_at": BUILD_TIME,
    }
    row.update(overrides)
    return row


def _ci_row(workflow_name: str, conclusion: str | None, **overrides) -> dict:
    row = {
        "repo": "apache/x",
        "workflow_name": workflow_name,
        "workflow_path": f".github/workflows/{workflow_name}.yml",
        "status": "completed",
        "conclusion": conclusion,
        "run_number": 42,
        "html_url": "https://github.com/apache/x/actions/runs/1",
        "run_started_at": BUILD_TIME,
        "source_snapshot_id": f"{RUN_ID}:subproject_releases",
        "collected_at": BUILD_TIME,
    }
    row.update(overrides)
    return row


class TestEmptyProject:
    def test_no_data_at_all_is_unavailable(self, tmp_path: Path):
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=True)
        assert ctx["available"] is False
        assert ctx["ga_available"] is False
        assert ctx["ga_lines"] == []
        assert ctx["jira_versions"] == []
        assert ctx["ci_tracked"] is False
        assert ctx["ci_workflows"] == []


class TestGaReleases:
    def test_grouped_by_line_latest_first(self, tmp_path: Path):
        _write_release_rows(
            tmp_path,
            [
                _ga_row("4.19.3", "4.19", date(2026, 6, 1)),
                _ga_row("4.19.4", "4.19", date(2026, 7, 1)),
                _ga_row("4.18.0", "4.18", date(2025, 1, 1)),
            ],
        )
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=False)
        assert ctx["available"] is True
        assert ctx["ga_available"] is True
        lines = {line["line"]: line for line in ctx["ga_lines"]}
        assert ctx["ga_lines"][0]["line"] == "4.19"  # newest line first
        assert lines["4.19"]["latest"]["version"] == "4.19.4"
        # `history` is newest-first and INCLUDES the latest release at
        # index 0 (the template shows `history[1:]` as "earlier releases"
        # alongside the `latest` field above) -- not just the earlier ones.
        assert lines["4.19"]["history"] == [
            {"version": "4.19.4", "release_date": date(2026, 7, 1)},
            {"version": "4.19.3", "release_date": date(2026, 6, 1)},
        ]
        assert lines["4.18"]["history"] == [
            {"version": "4.18.0", "release_date": date(2025, 1, 1)}
        ]

    def test_bare_major_line_sorts_numerically(self, tmp_path: Path):
        """easy-stress's own `ga_major_only` shape -- `major_minor` is a
        bare major version with no dot (e.g. "9"), and "10" must sort
        after "9", not before it lexicographically."""
        _write_release_rows(
            tmp_path,
            [
                _ga_row("9", "9", date(2026, 1, 1)),
                _ga_row("10", "10", date(2026, 2, 1)),
                _ga_row("2", "2", date(2020, 1, 1)),
            ],
        )
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=False)
        assert [line["line"] for line in ctx["ga_lines"]] == ["10", "9", "2"]


class TestJiraVersions:
    def test_has_jira_false_yields_no_jira_versions(self, tmp_path: Path):
        _write_jira_rows(tmp_path, [_jira_row("5.0.0", 5, 10)])
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=False)
        assert ctx["has_jira"] is False
        assert ctx["jira_versions"] == []

    def test_has_jira_true_reads_counts_and_link(self, tmp_path: Path):
        _write_jira_rows(
            tmp_path,
            [
                _jira_row("4.18.0", 0, 1),
                _jira_row("5.0.0", 5, 10),
            ],
        )
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=True)
        assert ctx["has_jira"] is True
        assert ctx["available"] is True
        by_name = {v["name"]: v for v in ctx["jira_versions"]}
        assert by_name["5.0.0"]["open_count"] == 5
        assert by_name["5.0.0"]["total_count"] == 10
        assert by_name["5.0.0"]["issues_url"].startswith("https://issues.apache.org/")
        # sorted by name
        assert [v["name"] for v in ctx["jira_versions"]] == ["4.18.0", "5.0.0"]

    def test_dedupes_to_latest_collected_at(self, tmp_path: Path):
        _write_jira_rows(tmp_path, [_jira_row("5.0.0", 5, 10, collected_at=BUILD_TIME)])
        from datetime import timedelta

        later = BUILD_TIME + timedelta(hours=1)
        storage.write_partition(
            tmp_path,
            "subproject_releases",
            "subproject_release_version",
            later.date(),
            "later-run",
            _table("subproject_release_version", [_jira_row("5.0.0", 2, 10, collected_at=later)]),
        )
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=True)
        assert len(ctx["jira_versions"]) == 1
        assert ctx["jira_versions"][0]["open_count"] == 2


class TestCiWorkflows:
    def test_not_tracked_when_no_rows(self, tmp_path: Path):
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=False)
        assert ctx["ci_tracked"] is False
        assert ctx["ci_workflows"] == []

    def test_tracked_workflows_sorted_by_name(self, tmp_path: Path):
        _write_ci_rows(
            tmp_path,
            [
                _ci_row("Test", "failure"),
                _ci_row("Build", "success"),
            ],
        )
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=False)
        assert ctx["ci_tracked"] is True
        assert ctx["available"] is True
        assert [w["workflow_name"] for w in ctx["ci_workflows"]] == ["Build", "Test"]
        assert ctx["ci_workflows"][0]["conclusion"] == "success"
        assert ctx["ci_workflows"][0]["run_started_at"] == BUILD_TIME.date().isoformat()


class TestGitTagCrossReference:
    """issue #175 review (PR #185, round 1): a JIRA fixVersion still
    marked unreleased by JIRA, but whose own version string already has a
    GA git tag in the `release` table, must show that tag rather than
    reading as a contradiction next to the GA releases table above --
    verified live for java-driver (3.13.0/4.18.0/4.19.4) and nodejs-driver
    (4.10.0), see `collectors/subproject_releases.py`."""

    def test_version_with_matching_git_tag_gets_the_tag_fact(self, tmp_path: Path):
        _write_release_rows(tmp_path, [_ga_row("4.19.4", "4.19", date(2026, 9, 24))])
        _write_jira_rows(tmp_path, [_jira_row("4.19.4", 0, 2)])
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=True)
        by_name = {v["name"]: v for v in ctx["jira_versions"]}
        assert by_name["4.19.4"]["git_tag"] == {
            "version": "4.19.4",
            "release_date": date(2026, 9, 24),
        }

    def test_version_with_no_matching_tag_gets_none(self, tmp_path: Path):
        _write_release_rows(tmp_path, [_ga_row("4.19.4", "4.19", date(2026, 9, 24))])
        _write_jira_rows(tmp_path, [_jira_row("5.0.0", 5, 10)])
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=True)
        by_name = {v["name"]: v for v in ctx["jira_versions"]}
        assert by_name["5.0.0"]["git_tag"] is None

    def test_match_requires_exact_version_string(self, tmp_path: Path):
        """sidecar's own real shape: JIRA's "0.5" never matches a tag
        named "0.5.0" -- string equality only, no fuzzy normalization."""
        _write_release_rows(tmp_path, [_ga_row("0.5.0", "0.5", date(2026, 1, 1))])
        _write_jira_rows(tmp_path, [_jira_row("0.5", 5, 26)])
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=True)
        assert ctx["jira_versions"][0]["git_tag"] is None

    def test_untagged_versions_sort_before_tagged_ones(self, tmp_path: Path):
        _write_release_rows(tmp_path, [_ga_row("4.18.0", "4.18", date(2024, 5, 20))])
        _write_jira_rows(
            tmp_path,
            [
                _jira_row("4.18.0", 0, 1),  # has a tag
                _jira_row("5.0.0", 5, 10),  # no tag -- genuinely upcoming
                _jira_row("3.13.0", 0, 3),  # no tag written here -- untagged
            ],
        )
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=True)
        names_in_order = [v["name"] for v in ctx["jira_versions"]]
        # Untagged group (alphabetical), then tagged group (alphabetical).
        assert names_in_order == ["3.13.0", "5.0.0", "4.18.0"]


class TestGaLinesPagination:
    """issue #175 review (PR #185, round 1): java-driver alone has 37 GA
    lines -- only the most recent `GA_LINES_VISIBLE` (8) render inline,
    the rest go in `ga_lines_older` for the template's own "Older release
    lines (N)" `<details>`."""

    def test_eight_or_fewer_lines_all_recent_none_older(self, tmp_path: Path):
        rows = [_ga_row(f"{n}.0.0", f"{n}.0", date(2020 + n, 1, 1)) for n in range(1, 9)]
        _write_release_rows(tmp_path, rows)
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=False)
        assert len(ctx["ga_lines"]) == 8
        assert len(ctx["ga_lines_recent"]) == 8
        assert ctx["ga_lines_older"] == []

    def test_more_than_eight_lines_splits_at_eight(self, tmp_path: Path):
        rows = [_ga_row(f"{n}.0.0", f"{n}.0", date(2000 + n, 1, 1)) for n in range(1, 13)]
        _write_release_rows(tmp_path, rows)
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=False)
        assert len(ctx["ga_lines"]) == 12
        assert len(ctx["ga_lines_recent"]) == 8
        assert len(ctx["ga_lines_older"]) == 4
        # Still newest-first across the whole set, split exactly at 8.
        assert ctx["ga_lines_recent"][0]["line"] == "12.0"
        assert ctx["ga_lines_recent"][-1]["line"] == "5.0"
        assert ctx["ga_lines_older"][0]["line"] == "4.0"
        assert ctx["ga_lines_older"][-1]["line"] == "1.0"


class TestAllThreeSources:
    def test_all_available_together(self, tmp_path: Path):
        _write_release_rows(tmp_path, [_ga_row("1.0.0", "1.0", date(2025, 1, 1))])
        _write_jira_rows(tmp_path, [_jira_row("2.0.0", 1, 1)])
        _write_ci_rows(tmp_path, [_ci_row("CI", "success")])
        ctx = build_subproject_releases_context(tmp_path, RUN_ID, has_jira=True)
        assert ctx["available"] is True
        assert ctx["ga_available"] is True
        assert ctx["jira_versions"]
        assert ctx["ci_tracked"] is True
