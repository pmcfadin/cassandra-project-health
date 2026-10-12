"""`/releases/` page context builder (issue #166).

Reads the four `release_lines` raw tables plus the existing `release` table
(GA git tags, issue #135) via `storage.read_table` -- the same
union-every-partition-ever-written + dedupe-at-read pattern every other
raw-table-backed page in this project uses (`_dedupe_latest` below mirrors
`pipeline._dedupe_security_advisories`/`_dedupe_release_rows`, duplicated
here rather than imported to avoid a site -> pipeline import cycle,
matching this module's own existing `_dedupe_latest` convention for the
Security partial).

The per-line ladder/series/GA-target/CI model itself is built by
`release_lines_model.build_release_line_model` (issue #166) -- this module
only (a) reads + dedupes the raw rows, (b) decides each of the three
sources' `available`/`fresh` flags, and (c) adapts the resulting
`ReleaseLineView`s into the plain dict/attribute shape `templates/
releases.html` renders, including the CI trend chart's Vega-Lite spec and
`chart_meta_json` (issue #156: every chart needs this for the expand
dialog).

## Availability vs. freshness

`available` is False only when NO row has EVER been collected for that
source (a first-run outage, or before the first nightly run at all).
`fresh` is True only when the table's own raw partition for the page's
current `run_id` exists on disk -- i.e. that source's fetch succeeded in
*this* run. A source can be `available` (there's historical data to show)
but not `fresh` (this run's fetch failed) at the same time; the page
renders a banner distinguishing the two rather than collapsing them into
one state (D25: plain facts, never implying more or less confidence than
the data supports).

Design rules (issue #166, D25): neutral facts only -- no ready/not-ready
verdict; every JIRA placeholder description quoted verbatim; the blocker
ladder's order comes from `collectors/release_lines.classify_versions`/
`order_ladder` (via `release_lines_model`), which themselves derive it from
each placeholder's own description text, never a hard-coded version name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from project_health import storage
from project_health.release_lines_model import (
    CiView,
    LadderStepView,
    ReleaseLineView,
    build_release_line_model,
)
from project_health.site import chart_spec

DEFAULT_ACTIVE_LINES: tuple[str, ...] = ("6.0", "5.0", "4.1", "4.0", "3.11", "3.0")
TRUNK_LINE = "trunk"

SOURCES_AND_LIMITS = (
    "Butler's own build history starts around June 2026 -- the 3.0 and 3.11 "
    "lines show only the handful of builds Butler has recorded for them so "
    "far. Per-test failure detail isn't shown in v1 (Butler's own per-suite "
    "endpoint times out after 60 seconds). This page states what the "
    "public record shows; it never judges whether a release is ready."
)


def _dedupe_latest(
    rows: list[dict[str, Any]], key_fields: tuple[str, ...], tiebreak: str
) -> list[dict[str, Any]]:
    """Keep the row with the greatest `tiebreak` value per `key_fields`
    (a composite key when more than one field is given).

    Mirrors `pipeline._dedupe_release_rows`'s logic (and this module's own
    sibling `_dedupe_latest` in `generate.py` for the Security partial),
    duplicated here rather than imported to avoid a site -> pipeline import
    cycle (`pipeline` already imports `site.generate`).
    """
    best: dict[Any, dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[f] for f in key_fields)
        current = best.get(key)
        if current is None or row[tiebreak] > current[tiebreak]:
            best[key] = row
    return list(best.values())


def _table_available(data_dir: Path, source: str, table: str) -> bool:
    return storage.read_table(data_dir, source, table).num_rows > 0


def _table_fresh(data_dir: Path, source: str, table: str, run_id: str | None) -> bool:
    if run_id is None:
        return False
    table_dir = storage.raw_table_dir(data_dir, source, table)
    return any(table_dir.glob(f"date=*/part-{run_id}.parquet"))


# issue #166 orchestrator review of PR #167: a build gap wider than this
# (Butler's own build cadence is irregular -- idle lines can go weeks
# between builds) must not be drawn as a straight connecting line implying
# continuous data that was never collected.
_CI_TREND_GAP_DAYS = 7


def _ci_trend_spec(points) -> str | None:
    if not points:
        return None
    # Break the line wherever two consecutive builds are more than
    # `_CI_TREND_GAP_DAYS` apart: each such gap starts a new `segment`:
    # `detail` encoding draws each segment as its own connected line (and,
    # since `point: true` lives on the same mark, every point --
    # including a segment of just one -- still renders), so a real gap in
    # Butler's own build history reads as a gap on the chart, not a
    # fabricated trend across it.
    values = []
    segment = 0
    prev_ts = None
    for p in points:
        if prev_ts is not None and (p.build_timestamp - prev_ts).days > _CI_TREND_GAP_DAYS:
            segment += 1
        prev_ts = p.build_timestamp
        values.append(
            {
                "build_timestamp": p.build_timestamp.isoformat(),
                "build_label": p.build_label,
                "test_failures": p.test_failures,
                "test_runs": p.test_runs,
                "segment": segment,
            }
        )
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 160,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "background": None,
        "data": {"values": values},
        "mark": {"type": "line", "point": True, "clip": True},
        "encoding": {
            "x": {"field": "build_timestamp", "type": "temporal", "title": None},
            "y": {"field": "test_failures", "type": "quantitative", "title": "Test failures"},
            "detail": {"field": "segment", "type": "nominal"},
            "tooltip": [
                {"field": "build_label", "type": "nominal", "title": "Build"},
                {
                    "field": "build_timestamp",
                    "type": "temporal",
                    "title": "Date",
                    "format": "%Y-%m-%d",
                },
                {"field": "test_failures", "type": "quantitative", "title": "Failures"},
                {"field": "test_runs", "type": "quantitative", "title": "Tests run"},
            ],
        },
    }
    return json.dumps(spec)


def _ci_dict(ci: CiView, chart_id: str, line_label: str) -> dict[str, Any]:
    spec_json = _ci_trend_spec(ci.trend)
    latest = ci.latest_trend_point
    return {
        "job_name": ci.job_name,
        "color": ci.color,
        "last_build_number": ci.last_build_number,
        "last_build_result": ci.last_build_result,
        # issue #166 orchestrator review of PR #167: plain calendar date
        # ("2026-10-10"), not the full ISO timestamp with microseconds/
        # offset -- nothing on this page needs sub-day precision for a
        # build's own date.
        "last_build_timestamp": (
            ci.last_build_timestamp.date().isoformat() if ci.last_build_timestamp else None
        ),
        "latest_test_failures": latest.test_failures if latest else None,
        "latest_test_runs": latest.test_runs if latest else None,
        "has_trend": bool(ci.trend),
        "chart_id": chart_id,
        "vega_spec_json": spec_json,
        "chart_meta_json": (
            chart_spec.chart_meta_json(
                chart_id=chart_id,
                title=f"CI test failures for {line_label}",
                time_field="build_timestamp",
                time_type="day",
            )
            if spec_json
            else None
        ),
    }


def _step_dict(step: LadderStepView) -> dict[str, Any]:
    return {
        "name": step.version_name,
        "description": step.description,
        "stage": step.stage_label,
        "target_label": step.target_label,
        "issues": step.issues,
        "open_count": step.open_count,
        "unassigned_count": step.unassigned_count,
    }


def _blockers_to_next_milestone_display(
    ladder: list[LadderStepView], has_ga_release: bool
) -> str | None:
    """"beta1: 2 · rc1: 0 · GA: 0" (issue #166 orchestrator review of PR
    #167) -- each ladder step's own `target_label`/`open_count`, in the
    ladder's own chain order. `None` (rendered as "—") when this line has
    no ladder at all, OR (orchestrator review, second round) when the line
    has already shipped a GA release: its alpha/beta/rc placeholders are
    pre-GA history at that point, not an upcoming milestone path, so the
    summary stops presenting them as one -- unconditionally, regardless of
    whether any of them still happens to carry an open ticket (the
    section below surfaces that separately, under a neutral heading, if
    so)."""
    if has_ga_release or not ladder:
        return None
    return " · ".join(f"{s.target_label or '?'}: {s.open_count}" for s in ladder)


def _line_dict(view: ReleaseLineView) -> dict[str, Any]:
    line_id = view.line.replace(".", "-")
    ladder_dicts = [_step_dict(s) for s in view.ladder]
    # Orchestrator review of PR #167, second round: once a line has a GA
    # release, its ladder is pre-GA history -- the "Blocker ladder"
    # section is shown only for a line that hasn't GA'd yet (e.g. 6.0,
    # whether or not it has any placeholders tracked yet -- a ladder-less
    # pre-GA line still gets the section, with its existing empty-state
    # message, same as before this fix). A GA'd line's ladder is omitted
    # entirely unless one of its legacy placeholders still happens to
    # carry an open ticket, in which case those tickets are listed under a
    # neutral "Open tickets on pre-GA placeholder versions" heading
    # instead -- never framed as "blocks <milestone>", since that
    # milestone already passed.
    show_ladder_section = not view.has_ga_release
    pre_ga_open_issues = []
    if view.has_ga_release:
        for step in view.ladder:
            pre_ga_open_issues.extend(step.issues)
    return {
        "id": line_id,
        "line": view.line,
        "has_ga_release": view.has_ga_release,
        "show_ladder_section": show_ladder_section,
        "ladder": ladder_dicts,
        "pre_ga_open_issues": list(pre_ga_open_issues),
        "series": _step_dict(view.series) if view.series else None,
        "ga_target": _step_dict(view.ga_target) if view.ga_target else None,
        "next_versions": [{"name": n} for n in view.next_versions],
        "next_release": _step_dict(view.next_target_step) if view.next_target_step else None,
        "total_open_blockers": view.total_open_blockers,
        "blockers_to_next_milestone": _blockers_to_next_milestone_display(
            view.ladder, view.has_ga_release
        ),
        "latest_release": (
            {
                "version": view.latest_release.version,
                "release_date": view.latest_release.release_date,
            }
            if view.latest_release
            else None
        ),
        "timeline": [
            {"version": t.version, "release_date": t.release_date} for t in view.timeline
        ],
        "ci": _ci_dict(view.ci, f"releases-ci-{line_id}", view.line),
    }


def build_releases_context(
    data_dir: str | Path,
    run_id: str | None = None,
    active_lines: tuple[str, ...] = DEFAULT_ACTIVE_LINES,
) -> dict[str, Any]:
    """Build `/releases/` page context, or `{"available": False}` if the
    `release_lines` source has never collected anything at all (fresh
    deploy, before the first nightly run -- same "no snapshot -> no page"
    discipline `peers_page.py`/thread-explorer already use)."""
    data_dir = Path(data_dir)
    source = "release_lines"

    jira_available = _table_available(data_dir, source, "jira_version")
    jenkins_available = _table_available(data_dir, source, "ci_job_status")
    butler_available = _table_available(data_dir, source, "ci_trend_point")

    if not (jira_available or jenkins_available or butler_available):
        return {"available": False}

    jira_fresh = _table_fresh(data_dir, source, "jira_version", run_id)
    jenkins_fresh = _table_fresh(data_dir, source, "ci_job_status", run_id)
    butler_fresh = _table_fresh(data_dir, source, "ci_trend_point", run_id)

    jira_version_rows = _dedupe_latest(
        storage.read_table(data_dir, source, "jira_version").to_pylist(),
        ("name",),
        "collected_at",
    )
    blocker_issue_rows = _dedupe_latest(
        storage.read_table(data_dir, source, "release_blocker_issue").to_pylist(),
        ("version_name", "issue_key"),
        "collected_at",
    )
    ci_job_rows = _dedupe_latest(
        storage.read_table(data_dir, source, "ci_job_status").to_pylist(),
        ("job_name",),
        "collected_at",
    )
    ci_trend_rows = _dedupe_latest(
        storage.read_table(data_dir, source, "ci_trend_point").to_pylist(),
        ("workflow", "build_label"),
        "collected_at",
    )
    release_rows = _dedupe_latest(
        storage.read_table(data_dir, "release", "release").to_pylist(),
        ("release_id",),
        "collected_at",
    )

    views = build_release_line_model(
        active_lines=list(active_lines),
        jira_version_rows=jira_version_rows,
        blocker_issue_rows=blocker_issue_rows,
        ci_job_rows=ci_job_rows,
        ci_trend_rows=ci_trend_rows,
        release_rows=release_rows,
        jira_available=jira_available,
    )

    lines = [_line_dict(v) for v in views if v.line != "trunk"]
    trunk_view = next(v for v in views if v.line == "trunk")
    trunk_ci = _ci_dict(trunk_view.ci, "releases-ci-trunk", "trunk")

    # issue #166: "anchors /releases/#6-0 etc." -- bare line id (e.g.
    # "6-0"), matching every other section-macro page's convention of a
    # plain descriptive id (`governance.html`'s "facts-summary", `peers.
    # html`'s per-metric section ids) rather than a generic "line-" prefix.
    release_sections = (
        [{"id": line["id"], "title": line["line"]} for line in lines]
        + [{"id": "trunk", "title": "trunk (CI only)"}]
        + [{"id": "sources-and-limits", "title": "Sources & limits"}]
    )

    return {
        "available": True,
        "jira_available": jira_available,
        "jira_fresh": jira_fresh,
        "jenkins_available": jenkins_available,
        "jenkins_fresh": jenkins_fresh,
        "butler_available": butler_available,
        "butler_fresh": butler_fresh,
        "lines": lines,
        "trunk_ci": trunk_ci,
        "release_sections": release_sections,
        "sources_and_limits": SOURCES_AND_LIMITS,
    }
