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
from datetime import UTC, date, datetime, timedelta
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
    for name, description, major_minor, released, release_date in [
        (
            "6.0-alpha",
            "Placeholder for blockers to 6.0-beta1 (unresolved tickets only)",
            "6.0",
            False,
            None,
        ),
        (
            "6.0-beta",
            "Placeholder for blockers to 6.0-rc1 (unresolved tickets only)",
            "6.0",
            False,
            None,
        ),
        (
            "6.0-rc",
            "Placeholder for blockers to 6.0.0 (GA) (unresolved tickets only)",
            "6.0",
            False,
            None,
        ),
        ("6.0.x", None, "6.0", False, None),
        ("6.0", None, "6.0", False, None),
        ("6.0-alpha3", None, "6.0", False, None),
        # Orchestrator review of PR #167, second round: 5.0 already has a
        # GA release (see `_release_rows()`/`_write_release_table` below)
        # but still carries this legacy ladder placeholder in JIRA, same
        # real shape as production data -- used to test that a GA'd
        # line's ladder is suppressed in the summary/section.
        (
            "5.0-alpha",
            "Placeholder for blockers to 5.0-beta1 (unresolved tickets only)",
            "5.0",
            False,
            None,
        ),
        # issue #166 orchestrator review of PR #167: released pre-release
        # versions -- 6.0 has no GA yet, so these are its ONLY release
        # history (no row in the git-tag `release` table at all).
        ("6.0-alpha1", None, "6.0", True, date(2026, 3, 21)),
        ("6.0-alpha2", None, "6.0", True, date(2026, 8, 7)),
        (
            "5.0.x",
            "Placeholder for a 5.0.x series change (unresolved tickets only)",
            "5.0",
            False,
            None,
        ),
        ("5.0.10", None, "5.0", False, None),
    ]:
        rows.append(
            {
                "version_id": f"id-{name}",
                "name": name,
                "description": description,
                "released": released,
                "release_date": release_date,
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
    # issue #166 orchestrator review of PR #167: a real, non-midnight
    # timestamp with microseconds (matching the real live shape, e.g.
    # "2026-10-10T16:06:25.994000+00:00") -- the page must render only the
    # calendar date, never the full timestamp.
    for job_name, branch_line, color, number, result, timestamp in [
        (
            "Cassandra-6.0",
            "6.0",
            "red",
            20,
            "FAILURE",
            datetime(2026, 10, 10, 16, 6, 25, 994000, tzinfo=UTC),
        ),
        ("Cassandra-5.0", "5.0", "yellow", 678, "UNSTABLE", datetime(2026, 10, 10, tzinfo=UTC)),
        ("Cassandra-trunk", "trunk", "red", 2635, "FAILURE", datetime(2026, 10, 10, tzinfo=UTC)),
    ]:
        rows.append(
            {
                "job_name": job_name,
                "branch_line": branch_line,
                "color": color,
                "last_build_number": number,
                "last_build_result": result,
                "last_build_timestamp": timestamp,
                "source_snapshot_id": f"{RUN_ID}:release_lines",
                "collected_at": collected_at,
            }
        )
    return rows


def _ci_trend_rows() -> list[dict]:
    collected_at = BUILD_TIME
    rows = []
    # issue #166 orchestrator review of PR #167: "#19"/"#1" are > 7 days
    # apart from the next build in their own line -- the chart must break
    # the line there rather than drawing a straight connector across a gap
    # with no builds.
    for workflow, branch_line, label, timestamp, failures in [
        ("Cassandra-6.0", "6.0", "#19", datetime(2026, 8, 5, tzinfo=UTC), 20),
        ("Cassandra-6.0", "6.0", "#20", datetime(2026, 10, 10, tzinfo=UTC), 33),
        ("Cassandra-5.0", "5.0", "#678", datetime(2026, 10, 8, tzinfo=UTC), 3),
        ("Cassandra-trunk", "trunk", "#1", datetime(2026, 7, 1, tzinfo=UTC), 10),
        ("Cassandra-trunk", "trunk", "#2635", datetime(2026, 10, 10, tzinfo=UTC), 33),
    ]:
        rows.append(
            {
                "workflow": workflow,
                "branch_line": branch_line,
                "build_label": label,
                "build_timestamp": timestamp,
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


def _release_rows() -> list[dict]:
    """A real-shaped `release` (git-tag GA) row for the 5.0 line --
    orchestrator review of PR #167, second round: used to mark 5.0 as
    GA'd (`has_ga_release`), distinct from 6.0, which has no row here."""
    return [
        {
            "release_id": "cassandra-5.0.9",
            "tag_name": "cassandra-5.0.9",
            "version": "5.0.9",
            "major_minor": "5.0",
            "release_date": date(2026, 8, 7),
            "release_date_source": "git_tag",
            "archive_verified": True,
            "archive_date": date(2026, 8, 7),
            "repo": "apache/cassandra",
            "source_snapshot_id": f"{RUN_ID}:release",
            "collected_at": BUILD_TIME,
        }
    ]


def _write_release_table(data_dir: Path, *, run_id: str = RUN_ID) -> None:
    storage.write_partition(
        data_dir,
        "release",
        "release",
        BUILD_TIME.date(),
        run_id,
        _table("release", _release_rows()),
    )


def _visible_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _chart_spec(html: str, chart_id: str) -> dict:
    """The parsed `data-vega-spec` JSON for the chart with `data-chart-id`
    == `chart_id` on a rendered /releases/ page."""
    pattern = rf"data-vega-spec='([^']*)'[^>]*data-chart-id=\"{re.escape(chart_id)}\""
    match = re.search(pattern, html)
    assert match is not None, f"no chart found with data-chart-id={chart_id!r}"
    return json.loads(match.group(1).replace("&#34;", '"').replace("&#39;", "'"))


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


class TestOrchestratorReviewFixesPR167:
    """Orchestrator review of PR #167 (five fixes, 2026-10-12)."""

    def test_pre_release_history_appears_in_6_0_timeline_and_latest_release(self, tmp_path):
        """Fix 1: 6.0 has no GA tag yet, so its own alpha1/alpha2 releases
        (JIRA's own released_versions) must still show up -- both in the
        per-line Timeline list and as the summary table's Latest release."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        assert "6.0-alpha1" in releases_html
        assert "6.0-alpha2" in releases_html
        # Scoped to 6.0's own Timeline section -- 5.0's timeline is
        # legitimately empty in this fixture (no "release" git-tag table
        # written at all here), which is a different, true fact, not this
        # test's concern.
        six_section = re.search(r'<details[^>]*id="6-0">(.*?)</details>', releases_html, re.DOTALL)
        assert six_section is not None
        assert "No released versions recorded for this line yet" not in six_section.group(1)
        # Summary table's "Latest release" column for 6.0 -- the later of
        # the two pre-releases, 2026-08-07.
        assert "6.0-alpha2 (2026-08-07)" in releases_html

    def test_blocker_summary_excludes_ga_target_tickets(self, tmp_path):
        """Fix 2: the summary's "Blockers to next milestone" column must
        come from the ladder's own (target_label, open_count) pairs only
        -- never summed with tickets merely targeted at the bare GA
        version. 6.0-alpha has 2 open (-> "beta1: 2"), 6.0-beta/rc have 0;
        the 3 GA-targeted tickets never enter this column."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        assert "beta1: 2" in releases_html
        assert "rc1: 0" in releases_html
        assert "GA: 0" in releases_html
        # Next release column: 6.0-alpha3, 0 open (no issues fixtured for it).
        assert "6.0-alpha3" in releases_html
        # The GA-targeted tickets are shown separately, under their own
        # "Targeted at 6.0" heading, not folded into "blocks" language.
        assert "Targeted at 6.0" in releases_html

    def test_trunk_summary_row_cells_align_with_header(self, tmp_path):
        """Fix 3: trunk's row must have exactly as many `<td>` cells as
        there are non-"Line" headers (5), with CI landing in the CI
        column, not the Blockers column."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        header_count = len(re.findall(r"<th scope=\"col\">", releases_html))
        trunk_row_match = re.search(
            r'<th scope="row">trunk \(CI only\)</th>(.*?)</tr>', releases_html, re.DOTALL
        )
        assert trunk_row_match is not None
        trunk_cells = re.findall(r"<td>(.*?)</td>", trunk_row_match.group(1), re.DOTALL)
        assert len(trunk_cells) == header_count - 1  # every column except "Line"
        # CI result text lands in a cell, not swallowed into the blockers "—".
        ci_cell_text = " ".join(trunk_cells)
        assert "FAILURE" in ci_cell_text

    def test_ci_trend_chart_breaks_across_a_build_gap(self, tmp_path):
        """Fix 4a: a >7-day gap between builds must not be drawn as one
        continuous line -- each point gets its own `segment`, and a gap
        increments it."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        spec = _chart_spec(releases_html, "releases-ci-6-0")
        values = spec["data"]["values"]
        segments = {v["build_label"]: v["segment"] for v in values}
        # "#19" (2026-08-05) is more than 7 days before "#20" (2026-10-10)
        # -- different segments. The chart's `detail` encoding must be
        # wired to the `segment` field so Vega-Lite actually breaks there.
        assert segments["#19"] != segments["#20"]
        assert spec["encoding"]["detail"]["field"] == "segment"

    def test_ci_trend_chart_autosizes_to_container(self, tmp_path):
        """Fix 4b: the chart must fit its container (not get clipped at
        the right edge), matching every other chart on the site."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        spec = _chart_spec(releases_html, "releases-ci-6-0")
        assert spec["autosize"] == {"type": "fit-x", "contains": "padding"}
        assert spec["width"] == "container"

    def test_ci_trend_tooltip_has_build_date_failures_tests(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        spec = _chart_spec(releases_html, "releases-ci-6-0")
        tooltip_fields = {t["field"] for t in spec["encoding"]["tooltip"]}
        assert tooltip_fields == {"build_label", "build_timestamp", "test_failures", "test_runs"}
        date_tooltip = next(
            t for t in spec["encoding"]["tooltip"] if t["field"] == "build_timestamp"
        )
        assert date_tooltip.get("format") == "%Y-%m-%d"

    def test_last_build_timestamp_renders_as_plain_date(self, tmp_path):
        """Fix 5: "2026-10-10", never the full
        "2026-10-10T16:06:25.994000+00:00"."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        assert "build #20, 2026-10-10)" in releases_html
        assert "16:06:25" not in releases_html
        assert "994000" not in releases_html

    def test_5_0_and_other_already_ga_lines_are_unaffected(self, tmp_path):
        """Fix 6: the same pre-release + next-release logic must not
        regress an already-GA'd line like 5.0 -- it keeps showing its next
        patch target and isn't forced into the 6.0-only pre-release path."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()
        assert "5.0.10" in releases_html

    def test_unreleased_line_with_no_placeholders_renders_gracefully(self, tmp_path):
        """Fix 6: a hypothetical active line with zero tracked JIRA
        versions at all (e.g. a future "7.0" before any placeholder
        exists) must not crash the page builder -- it just renders an
        empty-state line. Calls `build_releases_context` directly with an
        extra "7.0" line (it takes `active_lines` as its own parameter),
        rather than through the full `generate()` pipeline, since nothing
        else about site generation needs exercising for this check."""
        data_dir = tmp_path / "data"
        _write_release_lines_partitions(data_dir)

        from project_health.site.release_lines_page import (
            DEFAULT_ACTIVE_LINES,
            build_releases_context,
        )

        ctx = build_releases_context(
            data_dir, RUN_ID, active_lines=(*DEFAULT_ACTIVE_LINES, "7.0")
        )
        seven = next(line for line in ctx["lines"] if line["line"] == "7.0")
        assert seven["id"] == "7-0"
        assert seven["ladder"] == []
        assert seven["series"] is None
        assert seven["ga_target"] is None
        assert seven["next_release"] is None
        assert seven["timeline"] == []
        assert seven["latest_release"] is None


class TestGaLineHidesPreGaLadder:
    """Orchestrator review of PR #167, second round: once a line has a GA
    release, its pre-GA alpha/beta/rc ladder reads as a live upcoming
    milestone path if still shown -- it must be hidden; a line with no GA
    yet (6.0) keeps its ladder exactly as before."""

    def test_ga_line_summary_shows_dash_not_a_stale_ladder(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)
        _write_release_table(data_dir)  # marks 5.0 as GA'd

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        # 5.0's summary row: "Blockers to next milestone" is "—", not the
        # stale "beta1: 0 · rc1: 0 · GA: 0" its leftover 5.0-alpha
        # placeholder would otherwise produce.
        row_match = re.search(
            r'<th scope="row"><a href="#5-0">5\.0</a></th>(.*?)</tr>', releases_html, re.DOTALL
        )
        assert row_match is not None
        row_cells = re.findall(r"<td>(.*?)</td>", row_match.group(1), re.DOTALL)
        blockers_cell = _visible_text(row_cells[2])
        assert blockers_cell == "—"
        assert "beta1" not in blockers_cell

    def test_ga_line_section_omits_blocker_ladder_when_no_open_tickets(self, tmp_path):
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)
        _write_release_table(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        five_section = re.search(r'<details[^>]*id="5-0">(.*?)</details>', releases_html, re.DOTALL)
        assert five_section is not None
        section_text = five_section.group(1)
        assert "Blocker ladder" not in section_text
        assert "Open tickets on pre-GA placeholder versions" not in section_text
        assert "5.0-alpha" not in section_text

    def test_ga_line_section_lists_open_tickets_under_neutral_heading(self, tmp_path):
        """If a GA'd line's legacy placeholder still carries an open
        ticket (an edge case, but real data can have it), it's shown under
        the neutral heading -- never "Blocker ladder", never "blocks
        beta1"."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)

        blocker_rows = _blocker_issue_rows() + [
            {
                "version_name": "5.0-alpha",
                "issue_key": "CASSANDRA-99999",
                "summary": "stale leftover ticket",
                "status": "Open",
                "priority": "Normal",
                "assignee_raw": None,
                "created_at": None,
                "updated_at": datetime(2020, 1, 1, tzinfo=UTC),
                "source_snapshot_id": f"{RUN_ID}:release_lines",
                "collected_at": BUILD_TIME,
            }
        ]
        storage.write_partition(
            data_dir,
            "release_lines",
            "jira_version",
            BUILD_TIME.date(),
            RUN_ID,
            _table("jira_version", _jira_version_rows()),
        )
        storage.write_partition(
            data_dir,
            "release_lines",
            "release_blocker_issue",
            BUILD_TIME.date(),
            RUN_ID,
            _table("release_blocker_issue", blocker_rows),
        )
        storage.write_partition(
            data_dir,
            "release_lines",
            "ci_job_status",
            BUILD_TIME.date(),
            RUN_ID,
            _table("ci_job_status", _ci_job_rows()),
        )
        storage.write_partition(
            data_dir,
            "release_lines",
            "ci_trend_point",
            BUILD_TIME.date(),
            RUN_ID,
            _table("ci_trend_point", _ci_trend_rows()),
        )
        _write_release_table(data_dir)

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        five_section = re.search(r'<details[^>]*id="5-0">(.*?)</details>', releases_html, re.DOTALL)
        assert five_section is not None
        section_text = five_section.group(1)
        assert "Open tickets on pre-GA placeholder versions" in section_text
        assert "CASSANDRA-99999" in section_text
        assert "Blocker ladder" not in section_text
        assert "blocks beta1" not in section_text

    def test_pre_ga_line_keeps_its_ladder_regardless(self, tmp_path):
        """6.0 has no GA tag at all in this fixture -- its ladder must
        still render exactly as before, unaffected by 5.0's suppression."""
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "out"
        _write_main_snapshot(data_dir)
        _write_manifest(data_dir)
        _write_release_lines_partitions(data_dir)
        _write_release_table(data_dir)  # only covers 5.0, not 6.0

        generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
        releases_html = (out_dir / "releases" / "index.html").read_text()

        six_section = re.search(r'<details[^>]*id="6-0">(.*?)</details>', releases_html, re.DOTALL)
        assert six_section is not None
        section_text = six_section.group(1)
        assert "Blocker ladder" in section_text
        assert "6.0-alpha" in section_text
        assert "blocks beta1" in section_text

        row_match = re.search(
            r'<th scope="row"><a href="#6-0">6\.0</a></th>(.*?)</tr>', releases_html, re.DOTALL
        )
        assert row_match is not None
        row_cells = re.findall(r"<td>(.*?)</td>", row_match.group(1), re.DOTALL)
        blockers_cell = _visible_text(row_cells[2])
        assert "beta1: 2" in blockers_cell


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
