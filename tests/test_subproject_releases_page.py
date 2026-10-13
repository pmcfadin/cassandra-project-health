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
