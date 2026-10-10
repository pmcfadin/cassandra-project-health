"""`/peers/` page context builder (issue #145, DECISIONS.md D30).

Reads the latest `snapshots/peers/<run_id>/<project_id>/{metric_value,
pr_backlog}.parquet` files (written by `peers.pipeline.run_peers_collection`)
for Cassandra and every configured peer, and builds one collapsible
`templates/_sections.html` section per metric -- reusing that macro
unchanged (issue #144/#145 coordination: its own module docstring already
says "issue #145 (peer comparison) reuses it unchanged ... so its
signature/markup stays generic"). Each section's collapsed summary row
names all six projects (current value + sparkline), and its expanded body
holds the multi-line comparison chart (every project named in the
legend/tooltip, Cassandra's own line visually distinguished).

D30 (no targets, no ranking): every string here is a plain fact ("current
value," "as of month X") -- never "ahead of," "behind," "healthier than,"
or any comparative verdict language. The CHAOSS disclaimer
(`CHAOSS_DISCLAIMER`) is rendered once, prominently, at the top of the page.
Each section's `chaoss_label` links the metric's own CHAOSS Knowledge Base
page directly (e.g. "CHAOSS: Time to First Response"), not a practitioner
guide -- there is no single practitioner-guide topic that covers "peer
comparison," so `COMMUNITY_SECTIONS`' default label would be misleading
here; this mirrors `metrics_meta.COMMUNITY_SECTIONS`' own "releases"
section, which links a metric page under the same `chaoss_label=` override
for the same reason (no release practitioner guide exists).

No snapshot yet -> `available=False`, same "no snapshot -> no page" gate
`site/generate.py`'s other optional pages (thread explorer, conversation
patterns) already use.

## Completeness gating (issue #150, live, 2026-10-10)

`/peers/` is generated and published every night (`nightly.yml`), but a
peer's own GitHub-PR backfill (`peers/collect.py`'s three bounded-recency-
window passes) can legitimately still be in progress -- it is resumable
*across* this module's own weekly `peers.yml` runs (that module's own
docstring), not something guaranteed complete by the time any given
nightly run reads its latest snapshot. Rendering a GitHub-derived metric's
number while its backfill is still partway through silently understates
it (real-run finding: Flink's own open PR backlog rendered 25 against a
locally-verified 360, purely because its `open_prs` pass had never
completed a single run yet) -- indistinguishable, to a reader, from a
genuinely small number.

Each GitHub-derived metric (`GITHUB_METRIC_GATING_PASS` below) is gated on
its own one pass's settlement (`peers.collect.peer_pass_settlement`,
written per peer into that peer's own snapshot directory as
`settlement.json` by `peers.pipeline.run_peers_collection` -- read here via
`_read_settlement`, never recomputed from raw watermarks, so this module
makes no `project_health.storage`/GitHub-API calls of its own). An
unsettled peer renders "Collecting -- N of 3 passes complete" in place of
a value everywhere that metric would otherwise show one -- summary-row
card, multi-line chart (that peer's line is omitted entirely, never a
zero/null point), and the current-month table cell -- never a number, D30-
style ("these are shown for context only," never a guess dressed up as a
fact). **Contributor Absence Factor and Release Frequency are git-/
release-derived, not GitHub-PR-derived, and are never gated** -- the
problem this fixes (and `METRICS.md` §13's own "Collection" section) is
specific to the three-pass GitHub PR backfill. Cassandra's own GitHub
collection is `nightly.yml`'s ordinary incremental collector, already
caught up for months -- it never has a `settlement.json` of its own, and
a missing file reads as "fully settled" (`_read_settlement`'s own
default), so Cassandra is never gated either.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.metrics.pr_backlog import (
    AGE_1_3Y,
    AGE_30_90D,
    AGE_90D_1Y,
    AGE_GT_3Y,
    AGE_LT_30D,
    TOTAL,
    other_repo_metric_id,
)
from project_health.metrics.peer_metrics import TIME_TO_FIRST_RESPONSE_PR
from project_health.peers.collect import PASS_NAMES
from project_health.peers.pipeline import CASSANDRA_PROJECT_ID, latest_peers_run_id
from project_health.site import chart_spec

CHAOSS_DISCLAIMER = (
    "CHAOSS does not set targets or rank projects. These numbers are shown for context only "
    "-- Cassandra and five comparable Apache Software Foundation projects, chosen by the "
    "project owner, computed with the same code and the same definitions."
)

METRICS_SPEC_URL = (
    "https://github.com/pmcfadin/cassandra-project-health/blob/main/docs/spec/METRICS.md"
    "#13-peer-context-chaoss-starter-metrics-across-comparable-asf-projects-issue-145"
    "-decisionsmd-d30"
)

AGE_BUCKET_ORDER: tuple[tuple[str, str], ...] = (
    (AGE_LT_30D, "<30d"),
    (AGE_30_90D, "30-90d"),
    (AGE_90D_1Y, "90d-1y"),
    (AGE_1_3Y, "1-3y"),
    (AGE_GT_3Y, ">3y"),
)

# Project display order -- Cassandra always first (D30: Cassandra's own
# line is highlighted, never ranked against the rest).
_PEER_ORDER: tuple[tuple[str, str], ...] = (
    (CASSANDRA_PROJECT_ID, "Apache Cassandra"),
    ("kafka", "Apache Kafka"),
    ("spark", "Apache Spark"),
    ("flink", "Apache Flink"),
    ("pulsar", "Apache Pulsar"),
    ("datafusion", "Apache DataFusion"),
)

SPARKLINE_MONTHS = 24

# (metric_id, title, value_label, chaoss_url, chaoss_label) -- `chaoss_url`/
# `chaoss_label` link each section's own CHAOSS Knowledge Base metric page
# directly (module docstring: no practitioner-guide topic covers "peer
# comparison"), all curl-verified live 200, 2026-10-09 (same URLs METRICS.md
# §13 cites).
METRIC_ORDER: tuple[tuple[str, str, str, str, str], ...] = (
    (
        TIME_TO_FIRST_RESPONSE_PR,
        "Time to First Response (GitHub PRs)",
        "Time to First Response (GitHub PRs)",
        "https://chaoss.community/kb/metric-time-to-first-response/",
        "CHAOSS: Time to First Response",
    ),
    (
        "change_request_closure_ratio_pr",
        "Change Request Closure Ratio",
        "Change Request Closure Ratio",
        "https://chaoss.community/kb/metric-change-request-closure-ratio/",
        "CHAOSS: Change Request Closure Ratio",
    ),
    (
        "contributor_absence_factor",
        "Contributor Absence Factor",
        "Contributor Absence Factor",
        "https://chaoss.community/kb/metric-contributor-absence-factor/",
        "CHAOSS: Contributor Absence Factor",
    ),
    (
        "release_frequency",
        "Release Frequency (trailing 24 months)",
        "Release Frequency (trailing 24 months)",
        "https://chaoss.community/kb/metric-release-frequency/",
        "CHAOSS: Release Frequency",
    ),
)

BACKLOG_CHAOSS_URL = "https://chaoss.community/kb/metric-change-requests/"
BACKLOG_CHAOSS_LABEL = "CHAOSS: Change Requests (adapted)"

# Issue #150: which one of a peer's three GitHub-PR passes
# (`peers.collect.PASS_NAMES`) each GitHub-derived metric needs settled
# before it's safe to render a number for that peer -- the module
# docstring's own "Completeness gating" section. `contributor_absence_
# factor`/`release_frequency` are deliberately absent: git-/release-
# derived, never gated by this workflow's own GitHub-PR backfill state.
GITHUB_METRIC_GATING_PASS: dict[str, str] = {
    TIME_TO_FIRST_RESPONSE_PR: "created_desc",
    "change_request_closure_ratio_pr": "closed_search",
    "open_pr_backlog_total": "open_prs",
}


def _read_settlement(data_dir: Path, run_id: str, project_id: str) -> dict[str, bool]:
    """`project_id`'s own `settlement.json` (`peers.pipeline._write_
    settlement`), or "every pass settled" if that file doesn't exist --
    Cassandra never has one (module docstring: its GitHub collection isn't
    this module's three-pass peer backfill at all), and neither does any
    peer snapshot written before this issue shipped -- both read as fully
    settled, exactly today's un-gated behavior, rather than a hard failure
    or a false "still collecting" state."""
    settled_default = {pass_name: True for pass_name in PASS_NAMES}
    path = data_dir / "snapshots" / "peers" / run_id / project_id / "settlement.json"
    if not path.is_file():
        return settled_default
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return settled_default
    if not isinstance(raw, dict):
        return settled_default
    return {pass_name: bool(raw.get(pass_name, True)) for pass_name in PASS_NAMES}


def _passes_complete(settlement: dict[str, bool]) -> int:
    return sum(1 for settled in settlement.values() if settled)


def _collecting_label(passes_complete: int) -> str:
    return f"Collecting — {passes_complete} of {len(PASS_NAMES)} passes complete"


def _read_optional_table(path: Path, schema_table_name: str) -> pa.Table:
    from project_health.schema import get_schema, validate

    if not path.is_file():
        return get_schema(schema_table_name).empty_table()
    return validate(schema_table_name, pq.read_table(path))


def _details(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("details_json"):
        return {}
    return json.loads(row["details_json"])


def _backlog_total_rows(pr_backlog_rows: list[dict], project_id: str) -> list[dict]:
    """`open_pr_backlog_total`-equivalent rows for `project_id`.

    Cassandra's own filtered comparison tables carry `repo ==
    pr_backlog.PRIMARY_REPO`, so `compute_pr_backlog` emits the plain,
    unsuffixed ids for it; every peer repo is never `PRIMARY_REPO`, so it
    emits `other_repo_metric_id(..., repo)`-suffixed ids instead (module
    docstring of `metrics/pr_backlog.py`) -- this helper picks the right
    metric_id family per project rather than assuming one shape."""
    if project_id == CASSANDRA_PROJECT_ID:
        total_id, age_ids = TOTAL, dict(AGE_BUCKET_ORDER)
    else:
        repo = f"apache/{project_id}"
        total_id = other_repo_metric_id(TOTAL, repo)
        age_ids = {other_repo_metric_id(mid, repo): label for mid, label in AGE_BUCKET_ORDER}
    return [r for r in pr_backlog_rows if r["metric_id"] in {total_id, *age_ids}], total_id, age_ids


def _peer_sparkline_spec(rows: list[dict], months: int = SPARKLINE_MONTHS) -> dict[str, Any] | None:
    """A compact, axis-free trend line for one project's summary-row entry
    -- same shape/size as `generate.py::_sparkline_spec`, adapted to work
    from raw `metric_value` row dicts (this module never builds a
    `site.generate.MetricSeries`, since its data spans six projects' worth
    of independently-snapshotted tables, not one page's own `metrics.
    parquet`)."""
    series = sorted(rows, key=lambda r: r["window_start"])[-months:]
    if not series:
        return None
    values = [{"window_end": r["window_end"].isoformat(), "value": r["value"]} for r in series]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 28,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "background": None,
        "data": {"values": values},
        "mark": {"type": "line", "clip": True, "strokeWidth": 1.5},
        "encoding": {
            "x": {"field": "window_end", "type": "temporal", "axis": None},
            "y": {"field": "value", "type": "quantitative", "axis": None},
        },
        "config": {"view": {"stroke": None}},
    }


def _peer_headline_item(
    display_name: str, rows: list[dict], *, collecting_label: str | None = None
) -> dict[str, Any]:
    """One summary-row entry (name, latest value/month/n, sparkline) for a
    single project within one metric's section -- mirrors `generate.py::
    _section_headline_item`'s shape so `templates/_sections.html`'s macro
    renders it identically regardless of which page built it.

    `collecting_label` (issue #150): when set, this project is gated for
    this metric (module docstring's "Completeness gating") -- no value, no
    month, no sparkline, regardless of what `rows` holds, and
    `templates/_sections.html` renders `empty_label` in place of its own
    hard-coded "insufficient data" text."""
    if collecting_label is not None:
        return {
            "name": display_name,
            "value_display": None,
            "month_label": None,
            "n": None,
            "sparkline_spec_json": None,
            "empty_label": collecting_label,
        }
    series = sorted(rows, key=lambda r: r["window_start"])
    latest = series[-1] if series else None
    value = latest["value"] if latest else None
    return {
        "name": display_name,
        "value_display": round(value, 2) if value is not None else None,
        "month_label": latest["window_end"].strftime("%b %Y") if latest else None,
        "n": latest["n"] if latest else None,
        "sparkline_spec_json": json.dumps(spec) if (spec := _peer_sparkline_spec(rows)) else None,
        "empty_label": None,
    }


def _line_chart_spec(
    metric_id: str,
    series_by_project: dict[str, list[dict]],
    *,
    value_label: str,
    excluded_projects: set[str] | None = None,
) -> str | None:
    """One multi-line Vega-Lite spec per metric -- every project its own
    line, Cassandra's own styled distinctly (bold stroke) rather than
    colour alone, so it reads in both light and dark/greyscale contexts.
    Every project named in the legend and the tooltip (issue #145's own
    spec: "each peer named in legend/tooltip").

    `excluded_projects` (issue #150): project ids gated for this metric
    (module docstring's "Completeness gating") -- omitted from this chart
    entirely, never a zero/null point standing in for "still collecting" data."""
    excluded_projects = excluded_projects or set()
    values: list[dict[str, Any]] = []
    for project_id, display_name in _PEER_ORDER:
        if project_id in excluded_projects:
            continue
        for row in series_by_project.get(project_id, []):
            if row["metric_id"] != metric_id or row["value"] is None:
                continue
            values.append(
                {
                    "project": display_name,
                    "month": row["window_start"].isoformat(),
                    "value": row["value"],
                    "n": row["n"],
                    "is_cassandra": project_id == CASSANDRA_PROJECT_ID,
                }
            )
    if not values:
        return None

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 220,
        "data": {"values": values},
        "layer": [
            {
                "mark": {"type": "line", "interpolate": "linear"},
                "encoding": {
                    "x": {"field": "month", "type": "temporal", "title": None},
                    "y": {"field": "value", "type": "quantitative", "title": value_label},
                    "color": {"field": "project", "type": "nominal", "title": None},
                    "strokeWidth": {
                        "field": "is_cassandra",
                        "type": "nominal",
                        "legend": None,
                        "scale": {"domain": [False, True], "range": [1.5, 3.5]},
                    },
                    "tooltip": [
                        {"field": "project", "type": "nominal", "title": "Project"},
                        {"field": "month", "type": "temporal", "title": "Month"},
                        {"field": "value", "type": "quantitative", "title": value_label},
                        {"field": "n", "type": "quantitative", "title": "n"},
                    ],
                },
            }
        ],
    }
    return json.dumps(spec)


def build_peers_context(data_dir: str | Path, run_id: str | None = None) -> dict[str, Any]:
    """Build `/peers/` page context. `run_id` defaults to the latest
    `snapshots/peers/<run_id>/` directory (`latest_peers_run_id`)."""
    data_dir = Path(data_dir)
    run_id = run_id or latest_peers_run_id(data_dir)
    if run_id is None:
        return {"available": False, "chaoss_disclaimer": CHAOSS_DISCLAIMER}

    metric_rows_by_project: dict[str, list[dict]] = {}
    backlog_rows_by_project: dict[str, list[dict]] = {}
    for project_id, _ in _PEER_ORDER:
        snapshot_dir = data_dir / "snapshots" / "peers" / run_id / project_id
        metric_rows_by_project[project_id] = _read_optional_table(
            snapshot_dir / "metric_value.parquet", "metric_value"
        ).to_pylist()
        # `pr_backlog.parquet` is also `metric_value`-shaped output
        # (`compute_pr_backlog`'s own return type) -- a separate file, same
        # schema, per `peers.pipeline._write_snapshot`.
        backlog_rows_by_project[project_id] = _read_optional_table(
            snapshot_dir / "pr_backlog.parquet", "metric_value"
        ).to_pylist()

    if not any(metric_rows_by_project.values()):
        return {"available": False, "chaoss_disclaimer": CHAOSS_DISCLAIMER}

    # Issue #150: each project's own per-pass settlement (`settlement.json`,
    # `peers.pipeline._write_settlement`) -- "fully settled" (every pass
    # `True`) for Cassandra and for any peer with no such file yet
    # (`_read_settlement`'s own default). Read once, used by every gated
    # metric's section below and by the current-month table.
    settlement_by_project: dict[str, dict[str, bool]] = {
        project_id: _read_settlement(data_dir, run_id, project_id)
        for project_id, _ in _PEER_ORDER
    }
    passes_complete_by_project: dict[str, int] = {
        project_id: _passes_complete(settlement)
        for project_id, settlement in settlement_by_project.items()
    }

    def _gated_projects(gating_pass: str | None) -> set[str]:
        if gating_pass is None:
            return set()
        return {
            project_id
            for project_id, _ in _PEER_ORDER
            if not settlement_by_project[project_id].get(gating_pass, True)
        }

    sections = []
    for metric_id, title, value_label, chaoss_url, chaoss_label in METRIC_ORDER:
        gated_projects = _gated_projects(GITHUB_METRIC_GATING_PASS.get(metric_id))
        summary_items = [
            _peer_headline_item(
                display_name,
                [
                    r
                    for r in metric_rows_by_project.get(project_id, [])
                    if r["metric_id"] == metric_id
                ],
                collecting_label=(
                    _collecting_label(passes_complete_by_project[project_id])
                    if project_id in gated_projects
                    else None
                ),
            )
            for project_id, display_name in _PEER_ORDER
        ]
        sections.append(
            {
                "id": metric_id,
                "title": title,
                "chaoss_url": chaoss_url,
                "chaoss_label": chaoss_label,
                "summary_items": summary_items,
                "chart_spec": _line_chart_spec(
                    metric_id,
                    metric_rows_by_project,
                    value_label=value_label,
                    excluded_projects=gated_projects,
                ),
                "chart_id": metric_id,
                "chart_meta_json": chart_spec.chart_meta_json(
                    chart_id=metric_id,
                    title=f"{title} by project, over time",
                    time_field="month",
                    time_type="month",
                    series_field="project",
                ),
            }
        )

    # Open PR backlog (total) section -- its own metric_id per project (see
    # `_backlog_total_rows`), normalized to one shared metric_id
    # ("open_pr_backlog_total") here purely so `_line_chart_spec`/the
    # summary-row builder -- which key their per-row filter on a single
    # literal metric_id -- can treat every project's rows the same way
    # despite their differing raw ids.
    backlog_series: dict[str, list[dict]] = {}
    for project_id, _ in _PEER_ORDER:
        rows, total_id, _age_ids = _backlog_total_rows(
            backlog_rows_by_project.get(project_id, []), project_id
        )
        backlog_series[project_id] = [
            {**r, "metric_id": "open_pr_backlog_total"}
            for r in rows
            if r["metric_id"] == total_id
        ]
    backlog_gated_projects = _gated_projects(GITHUB_METRIC_GATING_PASS["open_pr_backlog_total"])
    backlog_summary_items = [
        _peer_headline_item(
            display_name,
            backlog_series.get(project_id, []),
            collecting_label=(
                _collecting_label(passes_complete_by_project[project_id])
                if project_id in backlog_gated_projects
                else None
            ),
        )
        for project_id, display_name in _PEER_ORDER
    ]
    sections.append(
        {
            "id": "open_pr_backlog_total",
            "title": "Open PR backlog (total)",
            "chaoss_url": BACKLOG_CHAOSS_URL,
            "chaoss_label": BACKLOG_CHAOSS_LABEL,
            "summary_items": backlog_summary_items,
            "chart_spec": _line_chart_spec(
                "open_pr_backlog_total",
                backlog_series,
                value_label="Open PRs",
                excluded_projects=backlog_gated_projects,
            ),
            "chart_id": "open_pr_backlog_total",
            "chart_meta_json": chart_spec.chart_meta_json(
                chart_id="open_pr_backlog_total",
                title="Open PR backlog (total) by project, over time",
                time_field="month",
                time_type="month",
                series_field="project",
            ),
        }
    )

    # Current-month table: one row per project, one column per metric.
    table_rows = []
    for project_id, display_name in _PEER_ORDER:
        rows_by_metric: dict[str, list[dict]] = {}
        for row in metric_rows_by_project.get(project_id, []):
            rows_by_metric.setdefault(row["metric_id"], []).append(row)

        cells = []
        as_of_month = None
        collecting_label = _collecting_label(passes_complete_by_project[project_id])
        for metric_id, _title, _value_label, _chaoss_url, _chaoss_label in METRIC_ORDER:
            gating_pass = GITHUB_METRIC_GATING_PASS.get(metric_id)
            if gating_pass is not None and not settlement_by_project[project_id].get(
                gating_pass, True
            ):
                cells.append(collecting_label)
                continue
            series = sorted(rows_by_metric.get(metric_id, []), key=lambda r: r["window_start"])
            latest = series[-1] if series else None
            if latest and latest["value"] is not None:
                cells.append(round(latest["value"], 2))
                as_of_month = latest["window_end"]
            else:
                cells.append(None)

        backlog_rows, backlog_total_id, _age_ids = _backlog_total_rows(
            backlog_rows_by_project.get(project_id, []), project_id
        )
        backlog_series_sorted = sorted(
            (r for r in backlog_rows if r["metric_id"] == backlog_total_id),
            key=lambda r: r["window_start"],
        )
        backlog_latest = backlog_series_sorted[-1] if backlog_series_sorted else None
        if not settlement_by_project[project_id].get(
            GITHUB_METRIC_GATING_PASS["open_pr_backlog_total"], True
        ):
            cells.append(collecting_label)
        else:
            backlog_value = None
            if backlog_latest and backlog_latest["value"] is not None:
                backlog_value = int(backlog_latest["value"])
            cells.append(backlog_value)
            if backlog_latest:
                as_of_month = as_of_month or backlog_latest["window_end"]

        table_rows.append(
            {
                "project_id": project_id,
                "display_name": display_name,
                "is_cassandra": project_id == CASSANDRA_PROJECT_ID,
                "as_of_month": as_of_month,
                "cells": cells,
            }
        )

    table_columns = [title for _, title, *_rest in METRIC_ORDER] + ["Open PR backlog (total)"]

    return {
        "available": True,
        # Named distinctly from the main pipeline's own `run_id` (already in
        # `common_ctx`, every page's shared render context) -- this is the
        # separate, weekly `peers.yml` run_id this snapshot came from.
        "peers_run_id": run_id,
        "chaoss_disclaimer": CHAOSS_DISCLAIMER,
        "metrics_spec_url": METRICS_SPEC_URL,
        # Named `peer_sections`, not `sections` -- `peers.html` does
        # `{% import "_sections.html" as sections %}`, which would collide
        # with a context variable of the same name.
        "peer_sections": sections,
        "table_columns": table_columns,
        "table_rows": table_rows,
    }
