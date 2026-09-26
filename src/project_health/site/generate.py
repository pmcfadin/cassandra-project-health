"""Static site generator (ARCHITECTURE.md §7.5, §8; D8, D13; task #8, #34).

`generate(data_dir, run_id, out_dir)` reads that run's computed metrics
(`snapshots/<run_id>/metrics.parquet`, the `metric_value` output contract —
`schema/README.md`) and its manifest (`manifests/<run_id>.json`, via
`project_health.site.manifest.load_manifest`) and writes a fully static,
multi-page site (D13):

- `/` (`index.html`) — a home page with one summary card per top-level
  page: its headline metrics (latest value, month) and a link.
- `/community/` — one dimension-grouped card per M0 metric (contributor
  sustainability, reviewer capacity, responsiveness), each with a tier
  badge, a Vega-Lite history chart, and downloadable
  `data/<metric_id>.json` / `.csv` siblings carrying the chart's
  provenance (ARCHITECTURE.md §5's per-chart download contract).
- `/conversations/` — an honest empty state plus a "what's coming"
  section, until #35 lands mailing-list metrics (D16).
- `/governance/` — a placeholder explaining what's coming (D14, D15).

Every page shares one Jinja base template (`templates/base.html`) with a
tab-style nav (current page marked via `aria-current="page"`) and the same
freshness banner / per-source badges. Which page a metric's card renders
on is declared once, in `metrics_meta.MetricMeta.page` — this module never
hard-codes a metric_id to page-id mapping.

Everything the generated HTML references is a *relative* URL (`data/...`,
`static/...`, and `../data/...`, `../static/...` from a page in a
subdirectory) so the site works unmodified under the
`/cassandra-project-health/` GitHub Pages subpath (ARCHITECTURE.md §8).

`insufficient_data` points are written as JSON `null` / an empty CSV cell,
never `0` — a metric-computation bug that leaves a non-null `value` next to
`flag = 'insufficient_data'` is not trusted here; this module always
re-derives the rendered value from `flag`, never from `value` alone
(METRICS.md §0.6, schema/README.md's `metric_value` contract).
"""

from __future__ import annotations

import csv
import io
import json
import math
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from jinja2 import Environment, FileSystemLoader, select_autoescape

from project_health.schema import validate
from project_health.site.manifest import RunManifest, load_manifest
from project_health.site.metrics_meta import (
    HOME_CARD_METRIC_LIMIT,
    M0_METRICS,
    PAGES,
    MetricMeta,
    PageMeta,
)

# Pinned CDN versions (cdn.jsdelivr.net) — issue #8: pinned, not `@latest`,
# so a chart never silently changes rendering behavior underneath a
# published monthly report (ARCHITECTURE.md §8, D5).
VEGA_VERSION = "5.30.0"
VEGA_LITE_VERSION = "5.21.0"
VEGA_EMBED_VERSION = "6.26.0"

# ARCHITECTURE.md §7.1 mitigation 2: warn if the manifest's completed_at is
# older than this, relative to build time.
FRESHNESS_WARNING_HOURS = 36

_PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = _PACKAGE_DIR / "templates"
STATIC_DIR = _PACKAGE_DIR / "static"

METHODOLOGY_URL = "https://github.com/pmcfadin/cassandra-project-health/tree/main/docs/spec"
DECISIONS_URL = "https://github.com/pmcfadin/cassandra-project-health/blob/main/docs/spec/DECISIONS.md"
COMMUNITY_HEALTH_SPEC_URL = (
    "https://github.com/pmcfadin/cassandra-project-health/blob/main/docs/spec/COMMUNITY-HEALTH.md"
)
# GitHub's auto-generated heading anchors for DECISIONS.md's D14/D15
# sections (governance.html deep-links straight to them).
DECISIONS_D14_ANCHOR = (
    "d14-governance-per-commit-minimums-from-a-versioned-owner-approved-policy"
)
DECISIONS_D15_ANCHOR = "d15-governance-transparency-full-per-commit-detail-including-names"

# The site root is `index.html`; every other page lives one directory down
# (`community/index.html`, etc.), so its relative links to `static/` and
# `data/` need one extra `../` (D13, issue #34). `base_prefix` is prepended
# to every such link in `templates/base.html` and this module.
HOME_BASE_PREFIX = "./"
SUBPAGE_BASE_PREFIX = "../"


def _version_key(version: str) -> tuple[int, ...]:
    """Parse a semver-ish `definition_version` string (e.g. "1.10") into a
    tuple of ints for numeric comparison -- lexicographic string comparison
    would rank "1.10" below "1.9", which is wrong (ARCHITECTURE.md §4.4)."""
    return tuple(int(part) for part in version.split("."))


@dataclass(frozen=True)
class MetricPoint:
    """One rendered window of a metric's history."""

    window_start: date
    window_end: date
    value: float | None
    n: int
    flag: str


@dataclass(frozen=True)
class MetricSeries:
    """A metric's full rendered history, plus its display metadata."""

    meta: MetricMeta
    definition_version: str | None
    points: list[MetricPoint]

    @property
    def latest(self) -> MetricPoint | None:
        """The most recent window with `flag == 'ok'`, or `None`.

        `insufficient_data` windows are never used as "the latest value" —
        an insufficient-data window renders as a gap, not a stale number
        standing in for a real one.
        """
        ok_points = [p for p in self.points if p.flag == "ok"]
        if not ok_points:
            return None
        return max(ok_points, key=lambda p: p.window_end)


def generate(
    data_dir: str | Path,
    run_id: str,
    out_dir: str | Path,
    *,
    now: datetime | None = None,
) -> None:
    """Build the static site for `run_id` into `out_dir`.

    Reads `snapshots/<run_id>/metrics.parquet` (validated against the
    `metric_value` schema) and `manifests/<run_id>.json` under `data_dir`.
    `now` is the build-time reference used for the freshness banner
    (ARCHITECTURE.md §7.1 mitigation 2); it defaults to the current UTC
    time — tests pass an explicit value for determinism.
    """
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    build_time = now if now is not None else datetime.now(UTC)

    manifest = load_manifest(data_dir, run_id)
    metrics_table = _read_metrics(data_dir, run_id)
    series_by_id = _build_series(metrics_table)

    out_dir.mkdir(parents=True, exist_ok=True)
    data_out = out_dir / "data"
    data_out.mkdir(parents=True, exist_ok=True)

    for metric_id, series in series_by_id.items():
        _write_json(data_out / f"{metric_id}.json", series, manifest)
        _write_csv(data_out / f"{metric_id}.csv", series, manifest)

    _copy_static(out_dir)
    _render_pages(out_dir, series_by_id, manifest, build_time)


def _read_metrics(data_dir: Path, run_id: str) -> pa.Table:
    path = data_dir / "snapshots" / run_id / "metrics.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"metrics snapshot not found: {path}")
    return validate("metric_value", pq.read_table(path))


def _build_series(table: pa.Table) -> dict[str, MetricSeries]:
    """Group `metric_value` rows by metric_id for every M0 metric.

    A metric with no rows in `table` still gets an (empty) `MetricSeries`
    so `generate` always writes its `data/<id>.json`/`.csv` pair — the site
    always shows all six M0 cards, with "insufficient data" where a metric
    hasn't produced a value yet.
    """
    rows_by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in table.to_pylist():
        rows_by_metric.setdefault(row["metric_id"], []).append(row)

    series_by_id: dict[str, MetricSeries] = {}
    for metric_id, meta in M0_METRICS.items():
        metric_rows = sorted(
            rows_by_metric.get(metric_id, []), key=lambda r: r["window_start"]
        )
        points = [
            MetricPoint(
                window_start=r["window_start"],
                window_end=r["window_end"],
                # Never trust `value` on its own: only a flag == 'ok' row's
                # value is rendered. This is what keeps a gap a gap even if
                # an upstream row is malformed (value set but flag isn't
                # 'ok').
                value=r["value"] if r["flag"] == "ok" else None,
                n=r["n"],
                flag=r["flag"],
            )
            for r in metric_rows
        ]
        # The reported definition_version is the MAXIMUM version present
        # among this metric's rows, not whichever row the window_start sort
        # happens to put last -- a snapshot can hold rows from more than one
        # definition_version at once (old-version rows are never overwritten,
        # ARCHITECTURE.md §4.4), and a bump doesn't necessarily land on the
        # latest window_start.
        definition_version = (
            max((r["definition_version"] for r in metric_rows), key=_version_key)
            if metric_rows
            else None
        )
        series_by_id[metric_id] = MetricSeries(
            meta=meta, definition_version=definition_version, points=points
        )
    return series_by_id


# --- Per-chart data downloads (ARCHITECTURE.md §5) --------------------------


def _provenance_header(series: MetricSeries, manifest: RunManifest) -> dict[str, Any]:
    return {
        "metric_id": series.meta.metric_id,
        "name": series.meta.name,
        "dimension": series.meta.dimension,
        "tier": series.meta.tier,
        "direction_of_good": series.meta.direction_of_good,
        "definition_version": series.definition_version,
        "run_id": manifest.run_id,
        "pipeline_code_sha": manifest.pipeline_code_sha,
    }


def _row_dicts(series: MetricSeries) -> list[dict[str, Any]]:
    return [
        {
            "window_start": p.window_start.isoformat(),
            "window_end": p.window_end.isoformat(),
            "value": p.value,
            "n": p.n,
            "flag": p.flag,
        }
        for p in series.points
    ]


def _write_json(path: Path, series: MetricSeries, manifest: RunManifest) -> None:
    payload = _provenance_header(series, manifest)
    payload["rows"] = _row_dicts(series)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_csv(path: Path, series: MetricSeries, manifest: RunManifest) -> None:
    header = _provenance_header(series, manifest)
    comment_lines = [f"# {key}: {value}" for key, value in header.items()]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["window_start", "window_end", "value", "n", "flag"])
    for point in series.points:
        writer.writerow(
            [
                point.window_start.isoformat(),
                point.window_end.isoformat(),
                # insufficient_data -> empty cell, never "0"
                "" if point.value is None else point.value,
                point.n,
                point.flag,
            ]
        )

    path.write_text("\n".join(comment_lines) + "\n" + buffer.getvalue())


# --- Vega-Lite chart spec ----------------------------------------------------

# All six M0 metrics are monthly windows (METRICS.md §1); this is how many
# days before the earliest data month's start / after the latest data
# month's end the x-domain is padded (issue #16) — enough that a
# single-point series (e.g. `stale_jira_rate` in M0) doesn't sit on the
# plot's edge, and that the last point of a long series doesn't render
# flush against the right edge where its mark would otherwise clip.
_X_DOMAIN_PAD_DAYS = 15

# Target roughly this many x-axis ticks regardless of how many months of
# history a series has (issue #16: "sensible tick count").
_TARGET_TICK_COUNT = 6


def _month_tick_step(points: list[MetricPoint]) -> int:
    """Month interval between x-axis ticks, so a long history doesn't
    crowd the axis with one label per month."""
    months = {(p.window_end.year, p.window_end.month) for p in points}
    month_count = len(months) or 1
    return max(1, math.ceil(month_count / _TARGET_TICK_COUNT))


def _month_floor(d: date) -> date:
    """The first day of `d`'s month."""
    return date(d.year, d.month, 1)


def _month_ceil_exclusive(d: date) -> date:
    """The first day of the month *after* `d`'s month."""
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _padded_month_domain(points: list[MetricPoint]) -> list[str] | None:
    """A `[start, end]` ISO-date domain padded past the data's actual
    month range, or `None` when there are no points to plot.

    Anchored to calendar-month boundaries (not the raw point dates)
    because the x-axis ticks (`_month_tick_step`, `timeUnit: yearmonth`)
    land on month starts: padding by a fixed number of days around a
    single point's raw date, instead of around its *month*, can push the
    domain's start past that month's own boundary — leaving the only
    visible tick on the *next* month, not the one the point is actually
    in. A single point has a zero-width data range; without this padding,
    Vega-Lite's default "nice" rounding falls back to hour-level ticks for
    a zero-span temporal domain — this is the "05 PM" bug (issue #16) —
    and the point renders exactly on the plot's edge.
    """
    if not points:
        return None
    dates = [p.window_end for p in points]
    pad = timedelta(days=_X_DOMAIN_PAD_DAYS)
    start = _month_floor(min(dates)) - pad
    end = _month_ceil_exclusive(max(dates)) + pad
    return [start.isoformat(), end.isoformat()]


def _vega_lite_spec(series: MetricSeries) -> dict[str, Any]:
    """Build the chart's Vega-Lite spec.

    Points with `value = None` (insufficient_data windows) are passed
    through as JSON `null` in `data.values`; Vega-Lite's line mark renders
    a `null` value as a gap in the line rather than interpolating through
    it or drawing it at zero — this is what "gaps show as gaps, never as
    zero" means in the actual chart, not just in the download files.
    """
    values = [
        {"window_end": point.window_end.isoformat(), "value": point.value}
        for point in series.points
    ]

    x_axis: dict[str, Any] = {
        "format": "%b %Y",
        "tickCount": {"interval": "month", "step": _month_tick_step(series.points)},
    }
    x_encoding: dict[str, Any] = {
        "field": "window_end",
        "type": "temporal",
        "timeUnit": "yearmonth",
        "title": None,
        "axis": x_axis,
    }
    domain = _padded_month_domain(series.points)
    if domain is not None:
        # `nice: False` because the domain is already explicitly padded —
        # letting Vega-Lite "nice"-round it further is what produces the
        # zero-span/hour-tick bug above for a single point.
        x_encoding["scale"] = {"domain": domain, "nice": False}

    y_axis: dict[str, Any] = {"format": series.meta.axis_format}
    if series.meta.axis_label_expr is not None:
        y_axis["labelExpr"] = series.meta.axis_label_expr

    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 150,
        # Vega-Lite's default autosize (`contains: "content"`) treats
        # `width` as the plotting rectangle only, so the Y-axis tick
        # labels' width is added *outside* it — the rendered SVG ends up
        # wider than the container app.js measured, and that overhang is
        # what let the line/points run past the plot's right edge (issue
        # #16). `contains: "padding"` instead makes `width` the budget for
        # the *whole* chart (axis labels included), so it never exceeds
        # the container.
        "autosize": {"type": "fit-x", "contains": "padding"},
        "background": None,
        "data": {"values": values},
        # `clip: True` keeps the line/point mark inside the plot area at
        # any container width (issue #16) even if a future data point ever
        # falls outside the padded domain above.
        "mark": {"type": "line", "point": True, "clip": True},
        "encoding": {
            "x": x_encoding,
            "y": {"field": "value", "type": "quantitative", "title": None, "axis": y_axis},
            "tooltip": [
                {"field": "window_end", "type": "temporal", "title": "Month", "format": "%b %Y"},
                {
                    "field": "value",
                    "type": "quantitative",
                    "title": series.meta.tooltip_title,
                    "format": series.meta.vega_format,
                },
            ],
        },
        "config": {"view": {"stroke": None}},
    }


# --- Freshness / staleness (ARCHITECTURE.md §7.1 mitigation 2, §7.3) --------


def _is_stale(manifest: RunManifest, build_time: datetime) -> bool:
    """True if the manifest is missing a completion time, or it's older
    than `FRESHNESS_WARNING_HOURS` relative to `build_time`."""
    completed_at = manifest.completed_at
    if completed_at is None:
        return True
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    age = build_time - completed_at
    return age.total_seconds() > FRESHNESS_WARNING_HOURS * 3600


# --- HTML rendering -----------------------------------------------------


def _group_by_dimension(
    series_list: list[MetricSeries],
) -> list[tuple[str, list[MetricSeries]]]:
    """Group a page's metrics by `dimension`, preserving first-seen order."""
    order: list[str] = []
    groups: dict[str, list[MetricSeries]] = {}
    for series in series_list:
        dimension = series.meta.dimension
        if dimension not in groups:
            groups[dimension] = []
            order.append(dimension)
        groups[dimension].append(series)
    return [(dimension, groups[dimension]) for dimension in order]


def _group_by_page(
    series_by_id: dict[str, MetricSeries],
) -> dict[str, list[MetricSeries]]:
    """Group all metrics by `MetricMeta.page` (D13), preserving
    `series_by_id`'s insertion order within each page's list. A metric
    whose `page` isn't a key in `PAGES` is a metadata bug (metrics_meta.py
    declares an unknown page), so this fails loudly rather than silently
    dropping the metric from every page."""
    groups: dict[str, list[MetricSeries]] = {page_id: [] for page_id in PAGES}
    for series in series_by_id.values():
        page_id = series.meta.page
        if page_id not in groups:
            raise ValueError(
                f"metric {series.meta.metric_id!r} declares unknown page {page_id!r}; "
                f"add it to project_health.site.metrics_meta.PAGES"
            )
        groups[page_id].append(series)
    return groups


def _card_context(series: MetricSeries, base_prefix: str) -> dict[str, Any]:
    latest = series.latest
    return {
        "metric_id": series.meta.metric_id,
        "name": series.meta.name,
        "tier": series.meta.tier,
        "direction_of_good": series.meta.direction_of_good,
        "latest_value_display": series.meta.format_value(latest.value) if latest else None,
        # All six M0 metrics have a monthly window (METRICS.md §1), so the
        # window's end date collapses to a plain month label, e.g. "Aug
        # 2026", rather than the raw ISO end-of-window date.
        "latest_month_label": latest.window_end.strftime("%b %Y") if latest else None,
        "vega_spec_json": json.dumps(_vega_lite_spec(series)),
        "json_href": f"{base_prefix}data/{series.meta.metric_id}.json",
        "csv_href": f"{base_prefix}data/{series.meta.metric_id}.csv",
    }


def _headline_metric_context(series: MetricSeries) -> dict[str, Any]:
    """A metric's home-page summary-card row: name, latest value, month —
    no chart, no tier badge (D13: "headline metrics (latest value, month)
    and a link")."""
    latest = series.latest
    return {
        "name": series.meta.name,
        "value_display": series.meta.format_value(latest.value) if latest else None,
        "month_label": latest.window_end.strftime("%b %Y") if latest else None,
    }


def _summary_card_context(page: PageMeta, page_series: list[MetricSeries]) -> dict[str, Any]:
    """A home-page summary card. Summary cards only ever render on the home
    page (`/`), so their link is relative to the site *root*, not to a
    subpage — `HOME_BASE_PREFIX + page.path` (e.g. `"./community/"`), never
    `SUBPAGE_BASE_PREFIX`."""
    headline = page_series[:HOME_CARD_METRIC_LIMIT]
    return {
        "page_id": page.page_id,
        "title": page.title,
        "summary": page.summary,
        "href": HOME_BASE_PREFIX + page.path,
        "metrics": [_headline_metric_context(s) for s in headline],
        "metric_count": len(page_series),
        "empty_message": page.empty_message,
    }


def _common_page_context(manifest: RunManifest, build_time: datetime) -> dict[str, Any]:
    """Context shared by every page's template render: the freshness
    banner, per-source badges, footer attribution, and pinned asset
    versions (ARCHITECTURE.md §7.1 mitigation 2, §7.3, §8)."""
    source_badges = [
        {
            "source": name,
            "status": status.status,
            "last_good_snapshot": status.last_good_snapshot,
        }
        for name, status in sorted(manifest.sources.items())
    ]
    return {
        "is_stale": _is_stale(manifest, build_time),
        "completed_at": manifest.completed_at.isoformat() if manifest.completed_at else None,
        "source_badges": source_badges,
        "run_id": manifest.run_id,
        "pipeline_code_sha": manifest.pipeline_code_sha,
        "short_sha": manifest.pipeline_code_sha[:7],
        "methodology_url": METHODOLOGY_URL,
        "decisions_url": DECISIONS_URL,
        "community_health_spec_url": COMMUNITY_HEALTH_SPEC_URL,
        "decisions_d14_anchor": DECISIONS_D14_ANCHOR,
        "decisions_d15_anchor": DECISIONS_D15_ANCHOR,
        "vega_version": VEGA_VERSION,
        "vega_lite_version": VEGA_LITE_VERSION,
        "vega_embed_version": VEGA_EMBED_VERSION,
    }


def _render_pages(
    out_dir: Path,
    series_by_id: dict[str, MetricSeries],
    manifest: RunManifest,
    build_time: datetime,
) -> None:
    """Render all four top-level pages (D13): `/`, `/community/`,
    `/conversations/`, `/governance/`, sharing `templates/base.html`'s nav
    and freshness/source-badge chrome."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    common_ctx = _common_page_context(manifest, build_time)
    series_by_page = _group_by_page(series_by_id)

    # Home (`/`).
    summary_cards = [
        _summary_card_context(page, series_by_page[page_id])
        for page_id, page in PAGES.items()
    ]
    home_html = env.get_template("home.html").render(
        current_page="home",
        base_prefix=HOME_BASE_PREFIX,
        summary_cards=summary_cards,
        **common_ctx,
    )
    (out_dir / "index.html").write_text(home_html)

    # Community (`/community/`).
    community_dimensions = [
        {
            "dimension": dimension,
            "metrics": [_card_context(s, SUBPAGE_BASE_PREFIX) for s in series_list],
        }
        for dimension, series_list in _group_by_dimension(series_by_page["community"])
    ]
    community_html = env.get_template("community.html").render(
        current_page="community",
        base_prefix=SUBPAGE_BASE_PREFIX,
        dimensions=community_dimensions,
        **common_ctx,
    )
    _write_subpage(out_dir, "community", community_html)

    # Conversations (`/conversations/`) — no metrics until #35 lands (D16).
    conversations_html = env.get_template("conversations.html").render(
        current_page="conversations",
        base_prefix=SUBPAGE_BASE_PREFIX,
        **common_ctx,
    )
    _write_subpage(out_dir, "conversations", conversations_html)

    # Governance (`/governance/`) — placeholder until the compliance engine
    # ships (D14, D15).
    governance_html = env.get_template("governance.html").render(
        current_page="governance",
        base_prefix=SUBPAGE_BASE_PREFIX,
        **common_ctx,
    )
    _write_subpage(out_dir, "governance", governance_html)


def _write_subpage(out_dir: Path, dirname: str, html: str) -> None:
    page_dir = out_dir / dirname
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "index.html").write_text(html)


def _copy_static(out_dir: Path) -> None:
    dest = out_dir / "static"
    dest.mkdir(parents=True, exist_ok=True)
    for item in STATIC_DIR.iterdir():
        if item.is_file():
            shutil.copy2(item, dest / item.name)
