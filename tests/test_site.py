"""Tests for project_health.site.generate (ARCHITECTURE.md §5, §7.1, §7.3, §8; issue #8).

The metrics engine (#7) and manifest writer (#9) aren't built yet, so these
tests write synthetic `metric_value` Parquet snapshots and manifest JSON
files directly into `tmp_path`, validating the Parquet through
`project_health.schema.validate` the same way the real pipeline would.
"""

from __future__ import annotations

import csv
import html as html_module
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from project_health.schema import validate
from project_health.site.generate import generate
from project_health.site.metrics_meta import M0_METRICS

RUN_ID = "2026-09-25-abc123"
CODE_SHA = "abc123def4567890abc123def4567890abc1234"
BUILD_TIME = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _metric_value_row(
    metric_id: str,
    window_start: date,
    window_end: date,
    value: float | None,
    n: int,
    flag: str,
    *,
    definition_version: str = "1.0",
    run_id: str = RUN_ID,
) -> dict:
    return {
        "metric_id": metric_id,
        "definition_version": definition_version,
        "window_start": window_start,
        "window_end": window_end,
        "value": value,
        "n": n,
        "flag": flag,
        "run_id": run_id,
        "computed_at": datetime(2026, 9, 25, 6, 30, tzinfo=UTC),
        "details_json": None,
    }


def _metric_value_table(rows: list[dict]) -> pa.Table:
    columns = {name: [row[name] for row in rows] for name in rows[0]}
    table = pa.table(
        {
            "metric_id": pa.array(columns["metric_id"], type=pa.string()),
            "definition_version": pa.array(columns["definition_version"], type=pa.string()),
            "window_start": pa.array(columns["window_start"], type=pa.date32()),
            "window_end": pa.array(columns["window_end"], type=pa.date32()),
            "value": pa.array(columns["value"], type=pa.float64()),
            "n": pa.array(columns["n"], type=pa.int64()),
            "flag": pa.array(columns["flag"], type=pa.string()),
            "run_id": pa.array(columns["run_id"], type=pa.string()),
            "computed_at": pa.array(columns["computed_at"], type=pa.timestamp("us", tz="UTC")),
            "details_json": pa.array(columns["details_json"], type=pa.string()),
        }
    )
    return validate("metric_value", table)


def _default_rows() -> list[dict]:
    rows = []
    for metric_id in M0_METRICS:
        rows.append(
            _metric_value_row(
                metric_id, date(2026, 7, 1), date(2026, 7, 31), 10.0, 12, "ok"
            )
        )
        rows.append(
            _metric_value_row(
                metric_id, date(2026, 8, 1), date(2026, 8, 31), 14.0, 15, "ok"
            )
        )
        # a below-floor window: insufficient_data, value must be null
        rows.append(
            _metric_value_row(
                metric_id, date(2026, 9, 1), date(2026, 9, 24), None, 2, "insufficient_data"
            )
        )
    return rows


def _write_snapshot(data_dir: Path, run_id: str, rows: list[dict]) -> None:
    table = _metric_value_table(rows)
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, snapshot_dir / "metrics.parquet")


def _write_manifest(
    data_dir: Path,
    run_id: str,
    *,
    completed_at: datetime | None,
    code_sha: str = CODE_SHA,
    sources: dict | None = None,
) -> None:
    manifest = {
        "run_id": run_id,
        "trigger": "schedule",
        "started_at": "2026-09-25T06:17:00Z",
        "completed_at": completed_at.isoformat() if completed_at else None,
        "pipeline_code_sha": code_sha,
        "sources": sources
        if sources is not None
        else {
            "git": {"status": "ok", "watermark": "sha:deadbeef", "records_collected": 42},
            "jira": {"status": "ok", "watermark": "2026-09-25T04:00:00Z", "records_collected": 118},
        },
        "metrics_computed": list(M0_METRICS),
        "metrics_skipped_insufficient_data": [],
        "data_branch_commit": "d4e5f6",
        "site_deploy_status": "ok",
    }
    manifests_dir = data_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{run_id}.json").write_text(json.dumps(manifest))


def _build_site(tmp_path: Path, rows: list[dict] | None = None, **manifest_kwargs) -> Path:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, rows if rows is not None else _default_rows())
    manifest_kwargs.setdefault("completed_at", BUILD_TIME - timedelta(hours=1))
    _write_manifest(data_dir, RUN_ID, **manifest_kwargs)
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    return out_dir


def _extract_vega_spec(html_text: str, aria_label: str) -> dict:
    """Pull one card's embedded Vega-Lite spec out of the rendered HTML.

    Mirrors what `app.js` does with `el.getAttribute("data-vega-spec")` —
    Jinja's autoescape turns `"` into `&#34;` inside the single-quoted
    attribute, which a browser decodes on `getAttribute` but a raw string
    search doesn't, so this decodes it the same way before parsing JSON.
    """
    idx = html_text.index(f'aria-label="{aria_label}"')
    card_start = html_text.rfind('<div class="chart"', 0, idx)
    spec_start = html_text.index("data-vega-spec='", card_start) + len("data-vega-spec='")
    spec_end = html_text.index("'", spec_start)
    return json.loads(html_module.unescape(html_text[spec_start:spec_end]))


# --- File layout + JSON/CSV content -----------------------------------------


def test_generate_writes_index_and_per_metric_downloads(tmp_path):
    out_dir = _build_site(tmp_path)

    assert (out_dir / "index.html").is_file()
    for metric_id in M0_METRICS:
        assert (out_dir / "data" / f"{metric_id}.json").is_file()
        assert (out_dir / "data" / f"{metric_id}.csv").is_file()
    assert (out_dir / "static" / "style.css").is_file()
    assert (out_dir / "static" / "app.js").is_file()


def test_json_output_matches_input_rows(tmp_path):
    rows = _default_rows()
    out_dir = _build_site(tmp_path, rows=rows)

    metric_id = "active_contributors_monthly"
    payload = json.loads((out_dir / "data" / f"{metric_id}.json").read_text())

    assert payload["metric_id"] == metric_id
    assert payload["run_id"] == RUN_ID
    assert payload["pipeline_code_sha"] == CODE_SHA

    input_rows = [r for r in rows if r["metric_id"] == metric_id]
    assert len(payload["rows"]) == len(input_rows)
    for out_row, in_row in zip(payload["rows"], input_rows, strict=True):
        assert out_row["window_start"] == in_row["window_start"].isoformat()
        assert out_row["window_end"] == in_row["window_end"].isoformat()
        assert out_row["n"] == in_row["n"]
        assert out_row["flag"] == in_row["flag"]
        assert out_row["value"] == in_row["value"]


def test_csv_output_has_provenance_header_and_matching_rows(tmp_path):
    out_dir = _build_site(tmp_path)
    metric_id = "stale_jira_rate"
    text = (out_dir / "data" / f"{metric_id}.csv").read_text()
    lines = text.splitlines()

    comment_lines = [line for line in lines if line.startswith("#")]
    assert any(f"metric_id: {metric_id}" in line for line in comment_lines)
    assert any(f"run_id: {RUN_ID}" in line for line in comment_lines)
    assert any(f"pipeline_code_sha: {CODE_SHA}" in line for line in comment_lines)

    data_lines = [line for line in lines if not line.startswith("#")]
    reader = csv.DictReader(data_lines)
    data_rows = list(reader)
    assert data_rows[0]["window_start"] == "2026-07-01"
    assert data_rows[-1]["flag"] == "insufficient_data"


# --- insufficient_data renders as a gap, never zero -------------------------


def test_insufficient_data_serializes_as_null_not_zero(tmp_path):
    """Even a malformed upstream row (flag=insufficient_data but a non-null
    stray `value`) must never surface a plotted zero or number — the site
    always re-derives the rendered value from `flag`, not `value` alone.
    """
    metric_id = "reviewer_hhi"
    rows = [
        _metric_value_row(metric_id, date(2026, 7, 1), date(2026, 7, 31), 0.35, 8, "ok"),
        # malformed on purpose: flag says insufficient_data, but value=0.0
        _metric_value_row(
            metric_id, date(2026, 8, 1), date(2026, 8, 31), 0.0, 1, "insufficient_data"
        ),
    ]
    # fill in the other five metrics minimally so generate() has full input
    for other_id in M0_METRICS:
        if other_id == metric_id:
            continue
        rows.append(
            _metric_value_row(other_id, date(2026, 7, 1), date(2026, 7, 31), 1.0, 6, "ok")
        )

    out_dir = _build_site(tmp_path, rows=rows)
    payload = json.loads((out_dir / "data" / f"{metric_id}.json").read_text())

    insufficient_row = payload["rows"][1]
    assert insufficient_row["flag"] == "insufficient_data"
    assert insufficient_row["value"] is None

    csv_text = (out_dir / "data" / f"{metric_id}.csv").read_text()
    data_lines = [line for line in csv_text.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(data_lines)
    csv_rows = list(reader)
    assert csv_rows[1]["flag"] == "insufficient_data"
    assert csv_rows[1]["value"] == ""

    # and the chart spec embedded in the HTML must carry null too, not 0
    html_text = (out_dir / "index.html").read_text()
    idx = html_text.index('aria-label="History chart for Reviewer Concentration (HHI)"')
    card_start = html_text.rfind("<div class=\"chart\"", 0, idx)
    spec_start = html_text.index("data-vega-spec='", card_start) + len("data-vega-spec='")
    spec_end = html_text.index("'", spec_start)
    # Jinja's autoescape turns `"` into the `&#34;` entity inside the
    # single-quoted attribute; browsers decode that on `getAttribute`, so
    # decode it here too before parsing as JSON.
    spec = json.loads(html_module.unescape(html_text[spec_start:spec_end]))
    values = spec["data"]["values"]
    assert values[1]["value"] is None


# --- Freshness banner (ARCHITECTURE.md §7.1 mitigation 2) -------------------


def test_stale_manifest_renders_banner(tmp_path):
    out_dir = _build_site(tmp_path, completed_at=BUILD_TIME - timedelta(hours=48))
    html_text = (out_dir / "index.html").read_text()
    assert "Data may be stale" in html_text


def test_fresh_manifest_does_not_render_banner(tmp_path):
    out_dir = _build_site(tmp_path, completed_at=BUILD_TIME - timedelta(hours=1))
    html_text = (out_dir / "index.html").read_text()
    assert "Data may be stale" not in html_text


def test_missing_completed_at_renders_banner(tmp_path):
    out_dir = _build_site(tmp_path, completed_at=None)
    html_text = (out_dir / "index.html").read_text()
    assert "Data may be stale" in html_text


def test_per_source_staleness_badge_from_manifest(tmp_path):
    out_dir = _build_site(
        tmp_path,
        completed_at=BUILD_TIME - timedelta(hours=1),
        sources={
            "git": {"status": "ok", "watermark": "sha:aaa", "records_collected": 10},
            "ponymail": {
                "status": "stale",
                "reason": "3 retries exhausted: 503",
                "last_good_snapshot": "2026-09-24-xyz",
            },
        },
    )
    html_text = (out_dir / "index.html").read_text()
    assert "ponymail: stale" in html_text
    assert "2026-09-24-xyz" in html_text
    assert "git: ok" in html_text


# --- No absolute root URLs (site must work under /cassandra-project-health/) -


def test_no_absolute_root_urls_in_html(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = (out_dir / "index.html").read_text()

    # href="/..." or src="/..." (a single leading slash, not "//" which is
    # protocol-relative and not part of this HTML anyway) would break the
    # site once it's served under the /cassandra-project-health/ subpath.
    assert not re.search(r'(?:href|src)="/(?!/)', html_text)


def test_no_absolute_root_urls_in_css(tmp_path):
    out_dir = _build_site(tmp_path)
    css = (out_dir / "static" / "style.css").read_text()
    assert 'url("/' not in css
    assert "url('/" not in css


# --- Units and value formatting (fixup cycle 1) -----------------------------


def test_metric_meta_format_value_by_kind():
    assert M0_METRICS["active_contributors_monthly"].format_value(12.0) == "12"
    assert M0_METRICS["new_contributors_monthly"].format_value(4.0) == "4"
    assert M0_METRICS["unique_reviewers_monthly"].format_value(9.0) == "9"
    assert M0_METRICS["reviewer_hhi"].format_value(0.35) == "0.350"
    assert M0_METRICS["stale_jira_rate"].format_value(0.123) == "12.3%"
    assert M0_METRICS["median_resolution_latency_jira"].format_value(14.2) == "14.2 days"


def test_card_shows_formatted_value_and_month_label(tmp_path):
    rows = [
        _metric_value_row(
            "active_contributors_monthly", date(2026, 8, 1), date(2026, 8, 31), 12.0, 12, "ok"
        ),
        _metric_value_row(
            "reviewer_hhi", date(2026, 8, 1), date(2026, 8, 31), 0.35, 8, "ok"
        ),
        _metric_value_row(
            "stale_jira_rate", date(2026, 8, 1), date(2026, 8, 31), 0.123, 20, "ok"
        ),
        _metric_value_row(
            "median_resolution_latency_jira", date(2026, 8, 1), date(2026, 8, 31), 14.2, 6, "ok"
        ),
        _metric_value_row(
            "new_contributors_monthly", date(2026, 8, 1), date(2026, 8, 31), 4.0, 5, "ok"
        ),
        _metric_value_row(
            "unique_reviewers_monthly", date(2026, 8, 1), date(2026, 8, 31), 9.0, 9, "ok"
        ),
    ]
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = (out_dir / "index.html").read_text()

    # Counts render as bare integers, not floats.
    assert '<span class="value">12</span>' in html_text
    # reviewer_hhi: 3 decimals on its 0-1 scale.
    assert '<span class="value">0.350</span>' in html_text
    # stale_jira_rate: a percentage, not a bare 0-1 fraction.
    assert '<span class="value">12.3%</span>' in html_text
    # median_resolution_latency_jira: "N days".
    assert '<span class="value">14.2 days</span>' in html_text
    # "as of <date>" is gone; the period is a plain month label.
    assert "as of" not in html_text
    assert html_text.count('<span class="value-period">Aug 2026</span>') == 6


def test_chart_tooltip_uses_metric_specific_format_and_title(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = (out_dir / "index.html").read_text()

    idx = html_text.index('aria-label="History chart for Reviewer Concentration (HHI)"')
    card_start = html_text.rfind('<div class="chart"', 0, idx)
    spec_start = html_text.index("data-vega-spec='", card_start) + len("data-vega-spec='")
    spec_end = html_text.index("'", spec_start)
    spec = json.loads(html_module.unescape(html_text[spec_start:spec_end]))

    value_tooltip = spec["encoding"]["tooltip"][1]
    assert value_tooltip["format"] == ".3f"
    assert value_tooltip["title"] == "Reviewer Concentration (HHI) (0-1)"


# --- Chart axis: month granularity, axis format, no edge clipping (#16) -----


def test_single_point_series_uses_month_axis_with_padded_domain(tmp_path):
    """A single-point series (`stale_jira_rate` in M0) must render a
    month-level x axis, not the "05 PM" hour-level ticks a zero-span
    temporal domain falls back to by default, and the lone point must not
    sit exactly on the domain's edge.
    """
    metric_id = "stale_jira_rate"
    rows = [_metric_value_row(metric_id, date(2026, 5, 1), date(2026, 5, 31), 0.1, 20, "ok")]
    for other_id in M0_METRICS:
        if other_id == metric_id:
            continue
        rows.append(_metric_value_row(other_id, date(2026, 5, 1), date(2026, 5, 31), 1.0, 6, "ok"))

    out_dir = _build_site(tmp_path, rows=rows)
    html_text = (out_dir / "index.html").read_text()
    spec = _extract_vega_spec(html_text, "History chart for Stale JIRA Issue Rate")

    x_enc = spec["encoding"]["x"]
    assert x_enc["timeUnit"] == "yearmonth"
    assert x_enc["axis"]["format"] == "%b %Y"

    point_date = date(2026, 5, 31).isoformat()
    domain_start, domain_end = x_enc["scale"]["domain"]
    # Strictly inside the domain, not flush against either edge.
    assert domain_start < point_date < domain_end


def test_multi_point_series_domain_extends_past_last_point(tmp_path):
    """The x-domain must extend past the last plotted point so its mark
    doesn't render flush against the plot's right edge (issue #16: "Line/
    points extend past the plot's right edge").
    """
    out_dir = _build_site(tmp_path)  # _default_rows(): points through 2026-09-24
    html_text = (out_dir / "index.html").read_text()
    spec = _extract_vega_spec(html_text, "History chart for Reviewer Concentration (HHI)")

    x_enc = spec["encoding"]["x"]
    first_point_date = date(2026, 7, 31).isoformat()  # _default_rows()'s first window_end
    last_point_date = date(2026, 9, 24).isoformat()  # _default_rows()'s last window_end
    domain_start, domain_end = x_enc["scale"]["domain"]
    assert domain_start < first_point_date
    assert domain_end > last_point_date

    # The mark is clipped to the plot area too, as a second line of
    # defense against any point that ever does fall outside the domain.
    assert spec["mark"]["clip"] is True


def test_y_axis_format_matches_metric_value_kind(tmp_path):
    """Each metric's Y axis must use its own d3 format, the same units the
    tooltip already shows (issue #16: a percent metric's axis showed a
    bare 0-1 fraction, e.g. "0.4", instead of "40%").
    """
    out_dir = _build_site(tmp_path)
    html_text = (out_dir / "index.html").read_text()

    def y_axis(aria_label: str) -> dict:
        return _extract_vega_spec(html_text, aria_label)["encoding"]["y"]["axis"]

    assert y_axis("History chart for Stale JIRA Issue Rate")["format"] == ".0%"
    assert y_axis("History chart for Reviewer Concentration (HHI)")["format"] == ".3f"
    assert y_axis("History chart for Active Contributors")["format"] == ",.0f"

    days_axis = y_axis("History chart for Median JIRA Resolution Latency")
    assert days_axis["format"] == ".1f"
    assert days_axis["labelExpr"] == "datum.label + ' d'"


def test_metric_meta_axis_format_and_label_expr_by_kind():
    assert M0_METRICS["active_contributors_monthly"].axis_format == ",.0f"
    assert M0_METRICS["reviewer_hhi"].axis_format == ".3f"
    assert M0_METRICS["stale_jira_rate"].axis_format == ".0%"
    assert M0_METRICS["median_resolution_latency_jira"].axis_format == ".1f"

    assert M0_METRICS["active_contributors_monthly"].axis_label_expr is None
    assert M0_METRICS["stale_jira_rate"].axis_label_expr is None
    assert M0_METRICS["median_resolution_latency_jira"].axis_label_expr == "datum.label + ' d'"


# --- Chart sizing (fixup cycle 1: charts rendered at width=0) ---------------


def test_chart_container_css_has_no_zero_width_layout(tmp_path):
    """Regression guard for the reported bug: vega-embed measured the
    `.chart` container's width as 0 on first paint because it relied on
    Vega-Lite's `"width": "container"` autosize/ResizeObserver alone. The
    real fix lives in app.js (resolves an explicit pixel width from
    `el.clientWidth` before calling vegaEmbed) and can only be verified in
    a real browser (see the Playwright check in the task report) — this
    test is a static guard that the CSS half of the fix (a block-level,
    100%-width, non-zero-min-height container) hasn't regressed.
    """
    out_dir = _build_site(tmp_path)
    css = (out_dir / "static" / "style.css").read_text()
    chart_rule = css[css.index(".chart {") : css.index("}", css.index(".chart {"))]
    assert "width: 100%" in chart_rule
    assert "display: block" in chart_rule
    assert "min-height" in chart_rule


# --- Manifest loader ---------------------------------------------------


def test_generate_raises_if_manifest_missing(tmp_path):
    data_dir = tmp_path / "data"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    with pytest.raises(FileNotFoundError):
        generate(data_dir, RUN_ID, tmp_path / "out")


def test_generate_raises_if_snapshot_missing(tmp_path):
    data_dir = tmp_path / "data"
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME)
    with pytest.raises(FileNotFoundError):
        generate(data_dir, RUN_ID, tmp_path / "out")
