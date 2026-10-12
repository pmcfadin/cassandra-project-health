"""Tests for /releases/ page wiring in project_health.site.generate (issue #166).

Builds a minimal site the same way `tests/test_site_peers.py` does for
`/peers/` -- an empty main metrics snapshot + manifest, with synthetic
`release_lines`/`release` raw partitions written directly via
`storage.write_partition` -- then calls `generate.generate(...)` and reads
the resulting HTML. Checks the page is generated, linked from nav/Home/
Community/Governance, anchors match "/releases/#6-0" (issue #166's own
literal example), chart metadata is present for the expand dialog (issue
#156), each of the three sources (JIRA/Butler/Jenkins) degrades to its own
"unavailable"/stale banner independently without the page failing to
build, and the page never renders ready/not-ready verdict vocabulary (D25).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from project_health import storage
from project_health.schema import get_schema, validate
from project_health.site.generate import generate

RUN_ID = "2026-10-12-abc123"
CODE_SHA = "abc123def4567890abc123def4567890abc1234"
BUILD_TIME = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _table(name: str, rows: list[dict]) -> pa.Table:
    schema = get_schema(name)
    if not rows:
        return schema.empty_table()
    return validate(name, pa.Table.from_pylist(rows, schema=schema))


def _write_main_snapshot(data_dir: Path) -> None:
    snapshot_dir = data_dir / "snapshots" / RUN_ID
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(_table("metric_value", []), snapshot_dir / "metrics.parquet")


def _write_manifest(data_dir: Path) -> None:
    manifest = {
        "run_id": RUN_ID,
        "trigger": "schedule",
        "started_at": "2026-10-12T06:17:00Z",
        "completed_at": (BUILD_TIME - timedelta(hours=1)).isoformat(),
        "pipeline_code_sha": CODE_SHA,
        "sources": {},
        "metrics_computed": [],
        "metrics_skipped_insufficient_data": [],
        "data_branch_commit": "d4e5f6",
        "site_deploy_status": "ok",
    }
    manifests_dir = data_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{RUN_ID}.json").write_text(json.dumps(manifest))


def _jira_version_rows() -> list[dict]:
    collected_at = BUILD_TIME
    rows = []
    for name, description, major_minor in [
        ("6.0-alpha", "Placeholder for blockers to 6.0-beta1 (unresolved tickets only)", "6.0"),
        ("6.0-beta", "Placeholder for blockers to 6.0-rc1 (unresolved tickets only)", "6.0"),
        ("6.0-rc", "Placeholder for blockers to 6.0.0 (GA) (unresolved tickets only)", "6.0"),
        ("6.0.x", None, "6.0"),
        ("6.0", None, "6.0"),
        ("6.0-alpha3", None, "6.0"),
        ("5.0.x", "Placeholder for a 5.0.x series change (unresolved tickets only)", "5.0"),
        ("5.0.10", None, "5.0"),
    ]:
        rows.append(
            {
                "version_id": f"id-{name}",
                "name": name,
                "description": description,
                "released": False,
                "release_date": None,
                "major_minor": major_minor,
                "source_snapshot_id": f"{RUN_ID}:release_lines",
                "collected_at": collected_at,
            }
        )
    return rows


def _blocker_issue_rows() -> list[dict]:
    collected_at = BUILD_TIME
    rows = []
    for version_name, issue_key, summary, status, priority, assignee in [
        (
            "6.0-alpha",
            "CASSANDRA-11745",
            "Add bytes limit to queries and paging",
            "Review In Progress",
            "Normal",
            "mck",
        ),
        (
            "6.0-alpha",
            "CASSANDRA-21340",
            "GROUP BY queries silently return incomplete results",
            "Open",
            "Normal",
            None,
        ),
        (
            "6.0.x",
            "CASSANDRA-18931",
            "ci job to build and push new in-tree docker images",
            "Patch Available",
            "Normal",
            "mmuzaf",
        ),
    ]:
        rows.append(
            {
                "version_name": version_name,
                "issue_key": issue_key,
                "summary": summary,
                "status": status,
                "priority": priority,
                "assignee_raw": assignee,
                "created_at": datetime(2020, 1, 1, tzinfo=UTC),
                "updated_at": datetime(2026, 10, 1, tzinfo=UTC),
                "source_snapshot_id": f"{RUN_ID}:release_lines",
                "collected_at": collected_at,
            }
        )
    return rows


def _ci_job_rows() -> list[dict]:
    collected_at = BUILD_TIME
    rows = []
    for job_name, branch_line, color, number, result in [
        ("Cassandra-6.0", "6.0", "red", 20, "FAILURE"),
        ("Cassandra-5.0", "5.0", "yellow", 678, "UNSTABLE"),
        ("Cassandra-trunk", "trunk", "red", 2635, "FAILURE"),
    ]:
        rows.append(
            {
                "job_name": job_name,
                "branch_line": branch_line,
                "color": color,
                "last_build_number": number,
                "last_build_result": result,
                "last_build_timestamp": datetime(2026, 10, 10, tzinfo=UTC),
                "source_snapshot_id": f"{RUN_ID}:release_lines",
                "collected_at": collected_at,
            }
        )
    return rows


def _ci_trend_rows() -> list[dict]:
    collected_at = BUILD_TIME
    rows = []
    for workflow, branch_line, label, failures in [
        ("Cassandra-6.0", "6.0", "#19", 30),
        ("Cassandra-6.0", "6.0", "#20", 33),
        ("Cassandra-5.0", "5.0", "#678", 3),
        ("Cassandra-trunk", "trunk", "#2635", 33),
    ]:
        rows.append(
            {
                "workflow": workflow,
                "branch_line": branch_line,
                "build_label": label,
                "build_timestamp": datetime(2026, 10, 10, tzinfo=UTC),
                "test_failures": failures,
                "test_runs": 300000,
                "duration_minutes": 400.0,
                "source_snapshot_id": f"{RUN_ID}:release_lines",
                "collected_at": collected_at,
            }
        )
    return rows


def _write_release_lines_partitions(
    data_dir: Path,
    *,
    run_id: str = RUN_ID,
    jira: bool = True,
    jenkins: bool = True,
    butler: bool = True,
) -> None:
    partition_date = BUILD_TIME.date()
    if jira:
        storage.write_partition(
            data_dir,
            "release_lines",
            "jira_version",
            partition_date,
            run_id,
            _table("jira_version", _jira_version_rows()),
        )
        storage.write_partition(
            data_dir,
            "release_lines",
            "release_blocker_issue",
            partition_date,
            run_id,
            _table("release_blocker_issue", _blocker_issue_rows()),
        )
    if jenkins:
        storage.write_partition(
            data_dir,
            "release_lines",
            "ci_job_status",
            partition_date,
            run_id,
            _table("ci_job_status", _ci_job_rows()),
        )
    if butler:
        storage.write_partition(
            data_dir,
            "release_lines",
            "ci_trend_point",
            partition_date,
            run_id,
            _table("ci_trend_point", _ci_trend_rows()),
        )


def _visible_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


class TestNoDataNoPage:
    def test_no_release_lines_data_no_page_no_nav_link(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)

        assert not (out_dir / "releases" / "index.html").exists()
        home_html = (out_dir / "index.html").read_text()
        assert 'href="./releases/"' not in home_html
        assert "Release lines" not in home_html


class TestReleasesPageRendering:
    def test_page_generated_with_summary_table_and_ladder(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)

        releases_html = (out_dir / "releases" / "index.html").read_text()

        # Summary table: both tracked lines appear, with their CI results.
        assert "FAILURE" in releases_html
        assert "UNSTABLE" in releases_html

        # issue #166: quote placeholder descriptions verbatim.
        assert "Placeholder for blockers to 6.0-beta1 (unresolved tickets only)" in releases_html
        assert "CASSANDRA-11745" in releases_html
        assert "CASSANDRA-21340" in releases_html
        assert "https://issues.apache.org/jira/browse/CASSANDRA-11745" in releases_html

        # issue #166: "anchors /releases/#6-0 etc."
        assert 'id="6-0"' in releases_html
        assert 'id="5-0"' in releases_html
        assert 'id="trunk"' in releases_html
        assert 'href="#6-0"' in releases_html

    def test_chart_metadata_present_for_expand_dialog(self, tmp_path):
        """issue #166/#156: every chart uses chart_shell + metadata."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        assert 'data-chart-expand' in releases_html
        assert 'data-chart-id="releases-ci-6-0"' in releases_html
        pattern = (
            r'data-chart-meta=\'([^\']*)\'[^>]*data-chart-id="releases-ci-6-0"'
            r'|data-chart-id="releases-ci-6-0"[^>]*data-chart-meta=\'([^\']*)\''
        )
        match = re.search(pattern, releases_html)
        assert match is not None
        meta_json = match.group(1) or match.group(2)
        meta = json.loads(meta_json.replace("&#34;", '"'))
        assert meta["id"] == "releases-ci-6-0"
        assert meta["timeField"] == "build_timestamp"

    def test_nav_and_cross_links_present_when_available(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)

        home_html = (out_dir / "index.html").read_text()
        assert 'href="./releases/"' in home_html
        assert "Release lines" in home_html

        community_html = (out_dir / "community" / "index.html").read_text()
        assert 'href="../releases/"' in community_html

        governance_html = (out_dir / "governance" / "index.html").read_text()
        assert 'href="../releases/"' in governance_html
        assert "post-commit" in governance_html.lower()

    def test_home_release_lines_row_shows_next_target_and_ci(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        home_html = (out_dir / "index.html").read_text()

        assert "6.0-alpha3" in home_html
        assert "5.0.10" in home_html


class TestIndependentSourceDegradation:
    """issue #166: "a source outage renders 'unavailable' for that block
    and never fails the nightly" -- each of JIRA/Jenkins/Butler is checked
    independently."""

    def test_jira_never_collected_renders_unavailable_but_ci_still_shows(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir, jira=False)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)

        releases_html = (out_dir / "releases" / "index.html").read_text()
        assert "JIRA versions/blockers: unavailable." in releases_html
        # CI facts for a line still render even though JIRA never collected.
        assert "FAILURE" in releases_html

    def test_jenkins_never_collected_renders_unavailable(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir, jenkins=False)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()
        assert "Jenkins CI status: unavailable." in releases_html
        # JIRA ladder facts still render.
        assert "CASSANDRA-11745" in releases_html

    def test_butler_never_collected_renders_unavailable(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir, butler=False)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()
        assert "Butler CI trends: unavailable." in releases_html
        assert "FAILURE" in releases_html  # Jenkins fact unaffected

    def test_stale_jira_banner_when_latest_partition_is_from_an_earlier_run(self, tmp_path):
        """A prior run collected JIRA successfully; THIS run's own JIRA
        fetch failed (no new partition written for run_id=RUN_ID) -- the
        page must still show the prior data, with a staleness banner, not
        'unavailable'."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        # Prior run wrote jira_version/release_blocker_issue successfully...
        _write_release_lines_partitions(
            data_dir, run_id="2026-10-11-priorrun", jenkins=False, butler=False
        )
        # ...this run only managed Jenkins + Butler.
        _write_release_lines_partitions(data_dir, run_id=RUN_ID, jira=False)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        assert "showing the last successfully collected data" in releases_html
        assert "JIRA versions/blockers: unavailable." not in releases_html
        # The prior run's real ladder data is still shown.
        assert "CASSANDRA-11745" in releases_html


class TestVerdictVocabulary:
    def test_releases_page_never_renders_verdict_vocabulary(self, tmp_path):
        """D25: neutral facts only. The page's own sanctioned disclaimer
        sentence ("it never judges whether a release is ready") is the one
        allowed use of "ready" and is excluded before scanning, same
        pattern as the Governance page's own sanctioned "does not judge
        compliance" sentence."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        html = (out_dir / "releases" / "index.html").read_text()
        scan_text = html.replace("it never judges whether a release is ready", "")
        text = _visible_text(scan_text).lower()

        for banned in (
            "ready",
            "not ready",
            "pass rate",
            "exempt",
            "not in force",
            "verdict",
            "threshold",
            "healthy",
            "unhealthy",
            "compliance",
        ):
            assert re.search(rf"\b{re.escape(banned)}\b", text) is None, banned

    def test_home_release_lines_row_never_renders_verdict_vocabulary(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        home_html = (out_dir / "index.html").read_text()
        text = _visible_text(home_html).lower()
        for banned in ("ready", "not ready", "verdict", "healthy", "unhealthy"):
            assert re.search(rf"\b{re.escape(banned)}\b", text) is None, banned
