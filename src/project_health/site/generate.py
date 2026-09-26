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
- `/conversations/` — dev@ mailing-list responsiveness metrics (issue #35,
  D16), rendered the same dimension-grouped-card way as `/community/`, plus
  a "what's coming" section for the still-gated Phase 2a/2b metrics.
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
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from jinja2 import Environment, FileSystemLoader, select_autoescape

from project_health import storage
from project_health.governance.metrics import metric_id_for_check
from project_health.metrics.windows import add_months, month_start
from project_health.schema import get_schema, validate
from project_health.site import chart_spec
from project_health.site.manifest import RunManifest, load_manifest
from project_health.site.governance_page import build_governance_page_context
from project_health.site.leaderboard_page import build_leaderboard_page_context
from project_health.site.scoring_page import build_scoring_page_context
from project_health.site.staleness import source_staleness_badges
from project_health.site.metrics_meta import (
    GOVERNANCE_METRICS,
    HOME_CARD_METRIC_LIMIT,
    M0_METRICS,
    PAGES,
    MetricMeta,
    PageMeta,
    format_days,
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
GOVERNANCE_SPEC_URL = (
    "https://github.com/pmcfadin/cassandra-project-health/blob/main/docs/spec/GOVERNANCE.md"
)
# GitHub's auto-generated heading anchor for GOVERNANCE.md's Security
# section (issue #55) — see that section for the per-check citations
# `_SECURITY_CHECK_NOTES` below condenses.
GOVERNANCE_SECURITY_ANCHOR = (
    "11-security-openssf-scorecard--cve-advisory-history-issue-55"
)
# GitHub's auto-generated heading anchors for DECISIONS.md's D14/D15
# sections (governance.html deep-links straight to them).
DECISIONS_D14_ANCHOR = (
    "d14-governance-per-commit-minimums-from-a-versioned-owner-approved-policy"
)
DECISIONS_D15_ANCHOR = "d15-governance-transparency-full-per-commit-detail-including-names"
# GitHub's auto-generated heading anchor for DECISIONS.md's D19 section
# (issue #56's contributor leaderboard) — community.html deep-links to it.
DECISIONS_D19_ANCHOR = "d19-contributor-leaderboard-amends-d2-rule-7"

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
    # issue #79/#35: this window's own `details_json.backfill_in_progress`
    # flag (`metrics/dev_metrics.py`'s `time_to_first_response_jira`,
    # `metrics/engine.py`'s devlist metrics) -- `False` for every metric that
    # doesn't set it, never inferred from `flag` alone (an `insufficient_data`
    # window can be a genuine small sample with no backfill involved at all).
    # Also used (fixup, orchestrator review of issue #57) so a card showing
    # this point as "the latest value" can flag it rather than presenting a
    # not-yet-caught-up point as though it were current.
    backfill_in_progress: bool = False


@dataclass(frozen=True)
class MetricSeries:
    """A metric's full rendered history, plus its display metadata."""

    meta: MetricMeta
    definition_version: str | None
    points: list[MetricPoint]

    @property
    def backfill_in_progress(self) -> bool:
        """True when this metric's chronologically most recent window is
        still `backfill_in_progress` (issue #79) -- checked against the last
        entry of `points` (sorted by `window_start` ascending, `_build_
        series`), not `.latest` below (which only considers `flag == 'ok'`
        points and so could miss a truly-latest window that's still
        `insufficient_data` precisely because of the gap this flag
        discloses)."""
        if not self.points:
            return False
        return self.points[-1].backfill_in_progress

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
    series_by_id = _build_series(metrics_table, M0_METRICS)

    # Governance compliance pass-rate metrics (issue #36, GOVERNANCE_METRICS)
    # live in their own snapshot file, entirely outside `metrics.parquet`'s
    # M0 pipeline (`governance/metrics.py`'s module docstring) -- optional,
    # since a run without the governance source configured never writes it.
    governance_metrics_table = _read_optional_metric_value_table(
        data_dir, run_id, "governance_metric_value.parquet"
    )
    series_by_id.update(_build_series(governance_metrics_table, GOVERNANCE_METRICS))

    out_dir.mkdir(parents=True, exist_ok=True)
    data_out = out_dir / "data"
    data_out.mkdir(parents=True, exist_ok=True)

    for metric_id, series in series_by_id.items():
        _write_json(data_out / f"{metric_id}.json", series, manifest)
        _write_csv(data_out / f"{metric_id}.csv", series, manifest)

    _copy_static(out_dir)
    _render_pages(out_dir, series_by_id, manifest, build_time, data_dir, run_id)


def _read_metrics(data_dir: Path, run_id: str) -> pa.Table:
    path = data_dir / "snapshots" / run_id / "metrics.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"metrics snapshot not found: {path}")
    return validate("metric_value", pq.read_table(path))


def _read_optional_metric_value_table(data_dir: Path, run_id: str, filename: str) -> pa.Table:
    """Same `metric_value` shape/validation as `_read_metrics`, but for a
    snapshot file that may legitimately not exist yet (governance's own
    snapshot, written by a separate pipeline step than `metrics.parquet`) --
    an empty, schema-valid table rather than `FileNotFoundError`."""
    path = data_dir / "snapshots" / run_id / filename
    if not path.is_file():
        return get_schema("metric_value").empty_table()
    return validate("metric_value", pq.read_table(path))


def _row_backfill_in_progress(row: dict[str, Any]) -> bool:
    """`row["details_json"].backfill_in_progress`, or `False` when
    `details_json` is absent/unparseable/doesn't carry that key -- a
    metric_value row from a metric that never sets this flag (every M0
    metric except the two dev@ metrics, `metrics/engine.py`) always reads
    `False` here."""
    raw = row.get("details_json")
    if not raw:
        return False
    try:
        details = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return bool(details.get("backfill_in_progress", False))


# Orchestrator review of issue #57: a card showing "the latest value" must
# not present a stale point as though it were current -- production showed
# "Time to First Reply 0.0 days, Jul 2023" while dev@ was still backfilling.
# A point counts as backfill-affected for card display if either its own
# row was flagged `backfill_in_progress` (the dev@ metrics' own honest
# signal), or -- as a general safety net covering any metric, not just the
# two that set that flag -- its month is more than 2 months older than the
# run's own last completed month (D5: the month containing the run's
# `completed_at` is never itself complete).
STALE_CARD_MONTHS = 2


def _last_completed_month(manifest: RunManifest, build_time: datetime) -> date:
    reference = manifest.completed_at or build_time
    return add_months(month_start(reference.date()), -1)


def _latest_point_is_backfill_pending(point: MetricPoint, last_completed_month: date) -> bool:
    if point.backfill_in_progress:
        return True
    return month_start(point.window_end) < add_months(last_completed_month, -STALE_CARD_MONTHS)


def _build_series(table: pa.Table, metrics_map: dict[str, MetricMeta]) -> dict[str, MetricSeries]:
    """Group `metric_value` rows by metric_id for every metric in `metrics_map`.

    A metric with no rows in `table` still gets an (empty) `MetricSeries` so
    `generate` always writes its `data/<id>.json`/`.csv` pair — the site
    always shows every declared card, with "insufficient data" where a
    metric hasn't produced a value yet.
    """
    rows_by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in table.to_pylist():
        rows_by_metric.setdefault(row["metric_id"], []).append(row)

    series_by_id: dict[str, MetricSeries] = {}
    for metric_id, meta in metrics_map.items():
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
                backfill_in_progress=_row_backfill_in_progress(r),
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
#
# Domain padding, tick spacing, the recent/full-history window, and the
# low-n de-emphasis threshold are shared with governance_page.py's
# multi-series compliance-trend charts -- see `site/chart_spec.py` (issue
# #28).


def _vega_lite_spec(series: MetricSeries) -> dict[str, Any]:
    """Build the chart's Vega-Lite spec.

    Points with `value = None` (insufficient_data windows) are passed
    through as JSON `null` in `data.values`; Vega-Lite's line mark renders
    a `null` value as a gap in the line rather than interpolating through
    it or drawing it at zero — this is what "gaps show as gaps, never as
    zero" means in the actual chart, not just in the download files.

    The spec is a two-layer chart (a full-opacity connecting line, plus a
    point layer whose opacity is conditioned on each datum's own `low_n`
    field) rather than a single `line, point: true` mark, so a point
    flagged `low_n` (issue #28: below `chart_spec.LOW_N_DISPLAY_FLOOR`, or
    `insufficient_data`) renders de-emphasised without fading the trend
    line itself. `n` and `flag` ride along in `data.values` purely for the
    tooltip (issue #28) — neither is ever used to compute `value`.
    """
    dates = [point.window_end for point in series.points]
    values = [
        {
            "window_end": point.window_end.isoformat(),
            "value": point.value,
            "n": point.n,
            "flag": point.flag,
            "low_n": chart_spec.is_low_n(point.n, point.flag, value_kind=series.meta.value_kind),
            # Precomputed display string for "days" metrics only (orchestrator
            # review of issue #57): a sub-day duration needs a unit switch
            # (hours/minutes) a single d3-format spec on the raw day-value
            # can't express, so the tooltip renders this nominal field
            # instead of formatting `value` itself for this value_kind --
            # see the tooltip encoding below and `format_days`.
            "value_display": (
                format_days(point.value)
                if point.value is not None and series.meta.value_kind == "days"
                else None
            ),
        }
        for point in series.points
    ]

    window = chart_spec.chart_window(dates)
    x_axis: dict[str, Any] = {
        "format": "%b %Y",
        "tickCount": {
            "interval": "month",
            "step": window["tickStep"]["recent"] if window else 1,
        },
    }
    x_encoding: dict[str, Any] = {
        "field": "window_end",
        "type": "temporal",
        "timeUnit": "yearmonth",
        "title": None,
        "axis": x_axis,
    }
    if window is not None:
        # `nice: False` because the domain is already explicitly padded —
        # letting Vega-Lite "nice"-round it further is what produces the
        # zero-span/hour-tick bug (issue #16) for a single point. Defaults
        # to the *recent* window (issue #28); `static/app.js`'s "Full
        # history" toggle swaps in `usermeta.chartWindow`'s `full` domain/
        # tickStep client-side.
        x_encoding["scale"] = {"domain": window["domain"]["recent"], "nice": False}

    y_axis: dict[str, Any] = {"format": series.meta.axis_format}
    if series.meta.axis_label_expr is not None:
        y_axis["labelExpr"] = series.meta.axis_label_expr
    y_encoding: dict[str, Any] = {
        "field": "value",
        "type": "quantitative",
        "title": None,
        "axis": y_axis,
    }
    # Restricting the x-domain alone doesn't stop a low-n outlier month from
    # still flattening the y-axis once it scrolls out of the default view
    # (issue #28) -- Vega-Lite's y-scale auto-fits to every value in
    # `data.values`, not just the ones inside the visible x-domain. Padding
    # `[0, max]` from only the recent window's own values fixes that; `None`
    # (an all-insufficient_data recent window) falls back to the original
    # whole-series auto-scale.
    recent_y_domain = chart_spec.recent_value_domain(
        [(p.window_end, p.value) for p in series.points]
    )
    if recent_y_domain is not None:
        y_encoding["scale"] = {"domain": recent_y_domain}

    if series.meta.value_kind == "days":
        # A static d3-format spec can't switch units (days/hours/minutes)
        # per point, so a "days" metric's tooltip shows the same precomputed,
        # unit-aware string (`format_days`) its card's big number uses
        # (orchestrator review of issue #57), as a nominal field rather than
        # a quantitative one formatted from the raw day-value.
        value_tooltip: dict[str, Any] = {
            "field": "value_display",
            "type": "nominal",
            "title": series.meta.tooltip_title,
        }
    else:
        value_tooltip = {
            "field": "value",
            "type": "quantitative",
            "title": series.meta.tooltip_title,
            "format": series.meta.vega_format,
        }
    tooltip = [
        {"field": "window_end", "type": "temporal", "title": "Month", "format": "%b %Y"},
        value_tooltip,
        {"field": "n", "type": "quantitative", "title": "n"},
        {"field": "flag", "type": "nominal", "title": "Flag"},
    ]

    spec: dict[str, Any] = {
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
        # Shared by every layer below (Vega-Lite merges a layered spec's
        # top-level `encoding` into each layer, letting a layer override or
        # add its own channels) — x/y/tooltip are identical across both
        # layers; only the point layer's `opacity` differs.
        "encoding": {"x": x_encoding, "y": y_encoding, "tooltip": tooltip},
        "layer": [
            # `clip: True` keeps the mark inside the plot area at any
            # container width (issue #16) even if a point ever falls
            # outside the padded domain above.
            {"mark": {"type": "line", "clip": True}},
            {
                "mark": {"type": "point", "clip": True, "filled": True},
                "encoding": {"opacity": chart_spec.LOW_N_OPACITY_ENCODING},
            },
        ],
        "config": {"view": {"stroke": None}},
    }
    if window is not None:
        # `yDomain.full: None` tells `static/app.js` to remove its y-scale
        # override entirely on "Full history" -- falling back to
        # Vega-Lite's own auto-scale over the whole series, the same
        # (unflattened-view-optional) behavior the chart had before issue
        # #28.
        spec["usermeta"] = {
            "chartWindow": {**window, "yDomain": {"recent": recent_y_domain, "full": None}}
        }
    return spec


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


# --- Per-card staleness badges (issue #86, ARCHITECTURE.md §7.3) -----------
#
# The header's `source_badges` (`_common_page_context` below) already show
# every source's status once, site-wide. `site.staleness` is the *per-card*
# badge ARCHITECTURE.md §7.3 actually specifies: "the site renders an
# explicit staleness badge on every card/metric that depends on that
# source" -- so a reader looking at one specific card sees, right there,
# which of *that card's own* sources is stale and since when, rather than
# having to cross-reference the header pill against `MetricMeta.sources`
# themselves. Factored into its own module (not defined here) since
# `leaderboard_page.py` needs the same logic for its metric-id-less
# leaderboard section without importing back into this module.
_source_staleness_badges = source_staleness_badges


# --- Governance / Security section (issue #55, D21 item 3) -----------------
#
# Deliberately separate from the `metric_value` machinery above: OpenSSF
# Scorecard's per-check results and NVD's per-CVE records are discrete
# facts, not monthly-windowed rates, so this reads the `security` source's
# raw tables directly from `data_dir` (same accumulated-history read
# `run_pipeline` itself uses for metrics, `storage.read_table`) rather than
# registering a poorly-fitting `metric_value` metric for them.

# Short, source-backed notes on *why* an ASF-hosted project like Cassandra
# scores the way it does on a given Scorecard check -- shown next to that
# check's row, only where there's a verified, specific reason to give one.
# See docs/spec/GOVERNANCE.md §11 for the full citations behind each note.
_SECURITY_CHECK_NOTES: dict[str, str] = {
    "Maintained": (
        "Scorecard's own evidence records “0 issue activity” because GitHub "
        "Issues is disabled on apache/cassandra — Cassandra tracks issues in JIRA, "
        "not GitHub — not because the project is inactive (verified: RESEARCH.md §6.2)."
    ),
    "Code-Review": (
        "This check only counts approved GitHub pull-request reviews. Cassandra's actual "
        "review process runs through commit-message trailers (“patch by X; reviewed by "
        "Y”) and JIRA reviewer fields, both invisible to it — see this page's own "
        "governance compliance results above for the real review-coverage numbers "
        "(verified: RESEARCH.md §6.2, GOVERNANCE.md R1)."
    ),
    "Branch-Protection": (
        "Scorecard's own evidence: force pushes are enabled and no required status checks "
        "were found on ‘trunk’ — a directly observable GitHub setting, not an artifact "
        "of Cassandra's review process living elsewhere. Unverified: whether this reflects "
        "a deliberate ASF Infra/commit-workflow choice rather than an oversight — not "
        "confirmed against an ASF Infra source."
    ),
    "Signed-Releases": (
        "This check only recognizes GitHub Releases, and apache/cassandra publishes none "
        "(0 GitHub Releases; DATA-SOURCES.md §5). ASF signs and checksums releases on "
        "downloads.apache.org/archive.apache.org instead — verified live: each release "
        "carries a .asc GPG signature plus .sha256/.sha512 checksums, and a public KEYS "
        "file is published alongside them. Scorecard has no visibility into that channel."
    ),
    "Packaging": (
        "Scorecard found no GitHub/GitLab publishing workflow. Cassandra's release process "
        "is the ASF dist/archive pipeline noted under Signed-Releases, not GitHub Packages "
        "or Actions-based publishing — same blind spot."
    ),
}


@dataclass(frozen=True)
class SecurityContext:
    """Everything the Security partial (`templates/_security.html`) needs."""

    scorecard: dict[str, Any] | None
    scorecard_run_count: int
    scorecard_history: list[dict[str, Any]]
    advisories: list[dict[str, Any]]
    advisory_count: int
    advisories_by_year: list[tuple[int, int]]


def _dedupe_latest(rows: list[dict[str, Any]], key: str, tiebreak: str) -> list[dict[str, Any]]:
    """Keep the row with the greatest `tiebreak` value per `key`.

    Mirrors `pipeline._dedupe_security_advisories`'s logic, duplicated here
    (rather than imported) to avoid a site -> pipeline import cycle
    (`pipeline` already imports `site.generate`).
    """
    best: dict[Any, dict[str, Any]] = {}
    for row in rows:
        current = best.get(row[key])
        if current is None or row[tiebreak] > current[tiebreak]:
            best[row[key]] = row
    return list(best.values())


def _read_security_context(data_dir: Path) -> SecurityContext:
    scorecard_rows = storage.read_table(data_dir, "security", "scorecard_check").to_pylist()
    advisory_rows = storage.read_table(data_dir, "security", "security_advisory").to_pylist()

    scorecard: dict[str, Any] | None = None
    scorecard_history: list[dict[str, Any]] = []
    runs: dict[str, list[dict[str, Any]]] = {}
    for row in scorecard_rows:
        runs.setdefault(row["source_snapshot_id"], []).append(row)

    if runs:
        ordered_snapshot_ids = sorted(
            runs, key=lambda sid: max(r["collected_at"] for r in runs[sid])
        )
        scorecard_history = [
            {
                "scorecard_date": runs[sid][0]["scorecard_date"],
                "overall_score": runs[sid][0]["overall_score"],
            }
            for sid in ordered_snapshot_ids
        ]
        latest_rows = sorted(runs[ordered_snapshot_ids[-1]], key=lambda r: r["check_name"])
        scorecard = {
            "repo": latest_rows[0]["repo"],
            "scorecard_date": latest_rows[0]["scorecard_date"],
            "scorecard_version": latest_rows[0]["scorecard_version"],
            "overall_score": latest_rows[0]["overall_score"],
            "checks": [
                {
                    "name": row["check_name"],
                    "score": row["check_score"],
                    "reason": row["check_reason"],
                    "details_summary": row["check_details_summary"],
                    "note": _SECURITY_CHECK_NOTES.get(row["check_name"]),
                }
                for row in latest_rows
            ],
        }

    deduped_advisories = _dedupe_latest(advisory_rows, "cve_id", "collected_at")
    deduped_advisories.sort(
        key=lambda r: r["published_date"] or date.min, reverse=True
    )

    by_year: dict[int, int] = {}
    for row in deduped_advisories:
        if row["published_date"] is not None:
            year = row["published_date"].year
            by_year[year] = by_year.get(year, 0) + 1

    return SecurityContext(
        scorecard=scorecard,
        scorecard_run_count=len(runs),
        scorecard_history=scorecard_history,
        advisories=deduped_advisories,
        advisory_count=len(deduped_advisories),
        advisories_by_year=sorted(by_year.items(), reverse=True),
    )


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


def _card_context(
    series: MetricSeries, base_prefix: str, last_completed_month: date, manifest: RunManifest
) -> dict[str, Any]:
    latest = series.latest
    return {
        "metric_id": series.meta.metric_id,
        "name": series.meta.name,
        "tier": series.meta.tier,
        # issue #86, ARCHITECTURE.md §7.3: this card's own per-source
        # staleness badge(s) -- distinct from the site-wide header pills
        # (`_common_page_context`'s `source_badges`) and from the
        # 'partial'/backfill-pending badge above, which is a different,
        # honest-progress signal, not a collection failure.
        "staleness_badges": _source_staleness_badges(series.meta.sources, manifest),
        "direction_of_good": series.meta.direction_of_good,
        "latest_value_display": series.meta.format_value(latest.value) if latest else None,
        # All six M0 metrics have a monthly window (METRICS.md §1), so the
        # window's end date collapses to a plain month label, e.g. "Aug
        # 2026", rather than the raw ISO end-of-window date.
        "latest_month_label": latest.window_end.strftime("%b %Y") if latest else None,
        # issue #79/#35 + orchestrator review of issue #57, merged into one
        # flag/badge rather than two: a card's "backfill pending" tag fires
        # if either the chronologically truest-latest window (which may
        # itself be `insufficient_data` precisely because of the gap,
        # `series.backfill_in_progress`, issue #79) is still backfilling, or
        # the most recent *displayed* (`flag == 'ok'`) point is stale enough
        # relative to the run's own last completed month to be misleading if
        # shown without a caveat (`_latest_point_is_backfill_pending`).
        "latest_is_backfill_pending": (
            series.backfill_in_progress
            or (
                _latest_point_is_backfill_pending(latest, last_completed_month)
                if latest
                else False
            )
        ),
        "vega_spec_json": json.dumps(_vega_lite_spec(series)),
        "json_href": f"{base_prefix}data/{series.meta.metric_id}.json",
        "csv_href": f"{base_prefix}data/{series.meta.metric_id}.csv",
    }


def _format_share(share: float) -> str:
    """A pass/fail/unknown share as a percentage, one decimal place --
    matching `MetricMeta.format_value`'s own `value_kind == "percent"`
    formatting (`metrics_meta.py`) so a governance headline's three shares
    read consistently with every other percent metric on the site (issue
    #69)."""
    return f"{share * 100:.1f}%"


def _headline_metric_context(series: MetricSeries, last_completed_month: date) -> dict[str, Any]:
    """A metric's home-page summary-card row: name, latest value, month —
    no chart, no tier badge (D13: "headline metrics (latest value, month)
    and a link")."""
    latest = series.latest
    return {
        "name": series.meta.name,
        "value_display": series.meta.format_value(latest.value) if latest else None,
        "month_label": latest.window_end.strftime("%b %Y") if latest else None,
        "is_backfill_pending": (
            _latest_point_is_backfill_pending(latest, last_completed_month) if latest else False
        ),
    }


def _summary_card_context(
    page: PageMeta,
    page_series: list[MetricSeries],
    last_completed_month: date,
    manifest: RunManifest,
) -> dict[str, Any]:
    """A home-page summary card. Summary cards only ever render on the home
    page (`/`), so their link is relative to the site *root*, not to a
    subpage — `HOME_BASE_PREFIX + page.path` (e.g. `"./community/"`), never
    `SUBPAGE_BASE_PREFIX`."""
    headline = page_series[:HOME_CARD_METRIC_LIMIT]
    # issue #86: the home summary card represents the *whole* page, so its
    # staleness badge(s) are the union across every metric on that page
    # (`page_series`), not just the (at most `HOME_CARD_METRIC_LIMIT`)
    # headline metrics actually listed on the card.
    page_sources: list[str] = []
    for s in page_series:
        for source in s.meta.sources:
            if source not in page_sources:
                page_sources.append(source)
    return {
        "page_id": page.page_id,
        "title": page.title,
        "summary": page.summary,
        "href": HOME_BASE_PREFIX + page.path,
        "metrics": [_headline_metric_context(s, last_completed_month) for s in headline],
        "staleness_badges": _source_staleness_badges(tuple(page_sources), manifest),
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
        "decisions_d19_anchor": DECISIONS_D19_ANCHOR,
        "governance_spec_url": GOVERNANCE_SPEC_URL,
        "governance_security_anchor": GOVERNANCE_SECURITY_ANCHOR,
        "vega_version": VEGA_VERSION,
        "vega_lite_version": VEGA_LITE_VERSION,
        "vega_embed_version": VEGA_EMBED_VERSION,
        # Issue #28: `_chart_window_toggle.html`'s wording ("last N years")
        # derives from the same constant the chart specs themselves use, so
        # the copy can never drift from the actual default domain.
        "chart_window_months": chart_spec.DEFAULT_WINDOW_MONTHS,
    }


def _render_pages(
    out_dir: Path,
    series_by_id: dict[str, MetricSeries],
    manifest: RunManifest,
    build_time: datetime,
    data_dir: Path,
    run_id: str,
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
    last_completed_month = _last_completed_month(manifest, build_time)

    # Home (`/`).
    summary_cards = [
        _summary_card_context(page, series_by_page[page_id], last_completed_month, manifest)
        for page_id, page in PAGES.items()
    ]
    # Composite health score + dimension breakdown (D20, issue #57) — a
    # small, additive call, same reasoning as the leaderboard's own wiring
    # immediately below: `scoring_page.py` and `scoring/engine.py` are the
    # only things that read/write this data.
    scoring_context = build_scoring_page_context(
        data_dir, run_id, out_dir, base_prefix=HOME_BASE_PREFIX
    )
    home_html = env.get_template("home.html").render(
        current_page="home",
        base_prefix=HOME_BASE_PREFIX,
        summary_cards=summary_cards,
        scoring=scoring_context,
        **common_ctx,
    )
    (out_dir / "index.html").write_text(home_html)

    # Community (`/community/`).
    community_dimensions = [
        {
            "dimension": dimension,
            "metrics": [
                _card_context(s, SUBPAGE_BASE_PREFIX, last_completed_month, manifest)
                for s in series_list
            ],
        }
        for dimension, series_list in _group_by_dimension(series_by_page["community"])
    ]
    # Contributor leaderboard (D19, issue #56) — a ranked top-N table, kept
    # entirely out of the `metric_value`/M0 machinery above; see
    # `leaderboard_page.py`'s module docstring for why this stays a small,
    # additive call here rather than threading leaderboard concerns through
    # this function's other logic.
    leaderboard_context = build_leaderboard_page_context(
        data_dir, run_id, out_dir, base_prefix=SUBPAGE_BASE_PREFIX, manifest=manifest
    )
    community_html = env.get_template("community.html").render(
        current_page="community",
        base_prefix=SUBPAGE_BASE_PREFIX,
        dimensions=community_dimensions,
        leaderboard=leaderboard_context,
        **common_ctx,
    )
    _write_subpage(out_dir, "community", community_html)

    # Conversations (`/conversations/`) — dev@ mailing-list metadata metrics
    # (issue #35, D16): cards render the same dimension-grouped shape as
    # `/community/` when metrics exist, replacing the page's honest empty
    # state; the "what's coming" section (Phase 2a/2b) always renders too.
    conversations_dimensions = [
        {
            "dimension": dimension,
            "metrics": [
                _card_context(s, SUBPAGE_BASE_PREFIX, last_completed_month, manifest)
                for s in series_list
            ],
        }
        for dimension, series_list in _group_by_dimension(series_by_page["conversations"])
    ]
    conversations_html = env.get_template("conversations.html").render(
        current_page="conversations",
        base_prefix=SUBPAGE_BASE_PREFIX,
        dimensions=conversations_dimensions,
        **common_ctx,
    )
    _write_subpage(out_dir, "conversations", conversations_html)

    # Governance (`/governance/`) — per-commit compliance trends and detail
    # (issue #37, D14/D15), the Security section (OpenSSF Scorecard + CVE/
    # advisory history, issue #55, D21 item 3) rendered from its own partial
    # template (`_security.html`) so it stays isolated from this page
    # section's own churn, same reasoning `governance_page.py`'s own
    # docstring gives for staying out of this module.
    security_context = _read_security_context(data_dir)
    governance_context = build_governance_page_context(
        data_dir, run_id, out_dir, manifest, base_prefix=SUBPAGE_BASE_PREFIX
    )
    # Each scored check gets one card: the same latest-value/tier/JSON-CSV
    # card shell every M0 metric uses (`_card_context`, keyed by this
    # check's `governance_*_pass_rate` metric_id), but with its chart swapped
    # for the compliance-trend module's per-check pass/fail/unknown/exempt
    # breakdown (`governance_context.compliance_trends`) instead of the
    # generic single-line pass-rate chart -- the breakdown is strictly more
    # informative (D15: "every result showing its evidence" extends to the
    # aggregate view never hiding fail/unknown/exempt behind a bare rate).
    governance_cards_by_metric = {
        s.meta.metric_id: _card_context(s, SUBPAGE_BASE_PREFIX, last_completed_month, manifest)
        for s in series_by_page["governance"]
    }
    governance_trend_cards = []
    for chart in governance_context.compliance_trends:
        card = governance_cards_by_metric.get(metric_id_for_check(chart["check_id"]))
        # The pass/fail/unknown shares beside the headline pass rate (issue
        # #69) -- same scored (pass+fail+unknown) denominator the pass rate
        # itself is computed over (`governance_page._latest_scored_shares`),
        # never a re-derived total, so a card never shows numbers that don't
        # add up to the metric it's displaying.
        shares = chart["latest_shares"]
        governance_trend_cards.append(
            {
                "check_id": chart["check_id"],
                "name": card["name"] if card else chart["check_id"],
                "tier": card["tier"] if card else None,
                "latest_value_display": card["latest_value_display"] if card else None,
                "latest_month_label": card["latest_month_label"] if card else None,
                "latest_pass_share_display": _format_share(shares["pass"]) if shares else None,
                "latest_unknown_share_display": (
                    _format_share(shares["unknown"]) if shares else None
                ),
                "latest_fail_share_display": _format_share(shares["fail"]) if shares else None,
                "backfill_pending": chart["backfill_pending"],
                # issue #86: the check's own card already computed its
                # staleness badge(s) from `MetricMeta.sources` -- reuse it
                # rather than recomputing.
                "staleness_badges": card["staleness_badges"] if card else [],
                "json_href": card["json_href"] if card else None,
                "csv_href": card["csv_href"] if card else None,
                "vega_spec_json": chart["vega_spec_json"],
                "has_data": chart["has_data"],
            }
        )
    governance_html = env.get_template("governance.html").render(
        current_page="governance",
        base_prefix=SUBPAGE_BASE_PREFIX,
        security=security_context,
        governance=governance_context,
        governance_trend_cards=governance_trend_cards,
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
