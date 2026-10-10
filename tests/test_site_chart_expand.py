"""Tests for the "Expand chart" dialog's server-side half (issue #156):
`chart_spec.chart_meta_json`'s own shape, and that every full chart on
every page carries a unique `data-chart-id` plus a valid `data-chart-meta`
blob next to an Expand button, with a `timeField`/`seriesField` that
actually exist in that chart's own embedded data.

The dialog itself (`static/chart_expand.js`) -- the date-range/series/band
controls, the deep-link parser, PNG/SVG/CSV downloads -- has no JS test
harness in this project (`pyproject.toml` has none configured), so it's
exercised by Playwright against a real build instead (issue #156's own
acceptance criteria: "Python-side metadata tests + Playwright"). These
tests are the metadata half of that pair: if a chart's declared
`timeField`/`seriesField` didn't actually match a field name in its own
`data.values`, the dialog's generic filtering would silently no-op for
that chart, so every assertion here checks the two together rather than
either alone.

Site-building fixtures are reused from their owning test modules (the same
`from tests.test_X import _private_helper` pattern `test_site_thread_
explorer.py` already uses for `test_site.py`'s own fixtures) rather than
duplicated -- a snapshot shape drifting out from under a hand-rolled copy
here would otherwise go unnoticed.
"""

from __future__ import annotations

import html as html_module
import json
import re

from project_health.site import chart_spec
from project_health.site.generate import generate

from tests.test_site import (
    BUILD_TIME,
    _build_site,
    _build_site_with_conversation_patterns,
    _build_site_with_governance,
    _build_site_with_pr_backlog,
    _build_site_with_review_responsiveness,
)
from tests.test_site_peers import (
    RUN_ID as PEERS_RUN_ID,
    _write_main_snapshot as _write_peers_main_snapshot,
    _write_manifest as _write_peers_manifest,
    _write_peers_snapshot,
)
from tests.test_site_thread_explorer import _build_site_with_threads

# Matches exactly the Expand button element's own boolean attribute, not
# `base.html`'s dialog's other `data-chart-expand-*` attributes (close/
# controls/main/overview/download), which all share that same prefix.
_EXPAND_BUTTON_RE = re.compile(r"<button[^>]*\bdata-chart-expand\b(?!-)[^>]*>")


def _chart_entries(html_text: str) -> list[tuple[str, dict]]:
    """Every `(chart_id, meta)` pair rendered on the page, decoded the same
    way a browser's `getAttribute` would (Jinja's autoescape turns `"`
    into `&#34;` inside the single-quoted attribute)."""
    out = []
    for match in re.finditer(
        r'data-chart-id="([^"]*)"[^>]*data-chart-meta=\'(.*?)\'', html_text
    ):
        chart_id, meta_raw = match.groups()
        out.append((chart_id, json.loads(html_module.unescape(meta_raw))))
    return out


def _spec_for(html_text: str, chart_id: str) -> dict:
    """The Vega-Lite spec of whichever chart-carrying attribute
    (`data-vega-spec` or, for the thread explorer's charts, `data-threads-
    chart-spec`) sits in the same opening tag as `data-chart-id`, in
    whichever attribute order the template happens to use."""
    tag_match = re.search(
        r"<div[^>]*\bdata-chart-id=\"" + re.escape(chart_id) + r"\"[^>]*>", html_text
    )
    assert tag_match, f"no element with data-chart-id={chart_id!r}"
    tag = tag_match.group(0)
    spec_match = re.search(r"data-(?:vega-spec|threads-chart-spec)='(.*?)'", tag)
    assert spec_match, f"chart {chart_id!r} has no data-vega-spec/data-threads-chart-spec"
    return json.loads(html_module.unescape(spec_match.group(1)))


def _rows_of(spec: dict) -> list[dict]:
    return spec.get("data", {}).get("values", [])


def _encoding_owner(spec: dict) -> dict | None:
    """Mirrors `chart_expand.js::encodingOwner`: the object that actually
    carries this chart's `encoding` -- top level, `spec["spec"]` for a
    faceted small-multiples spec, or the first layer that has one
    (`peers_page.py::_line_chart_spec`, which has no top-level `encoding`
    at all)."""
    inner = spec.get("spec")
    if isinstance(inner, dict) and "encoding" in inner:
        return inner
    if "encoding" in spec:
        return spec
    for layer in spec.get("layer", []):
        if "encoding" in layer:
            return layer
    return None


def _spec_color_domain(spec: dict) -> list[str] | None:
    """The chart's own declared series order, straight from its spec's
    `encoding.color.sort` (preferred) or `encoding.color.scale.domain` --
    whichever `chart_spec.chart_meta_json`'s `series_order` is supposed to
    mirror (orchestrator review of PR #157)."""
    owner = _encoding_owner(spec)
    if not owner:
        return None
    color = owner.get("encoding", {}).get("color")
    if not color:
        return None
    if color.get("sort"):
        return color["sort"]
    return color.get("scale", {}).get("domain")


def _assert_charts_wired(html_text: str, *, expected_min: int = 1) -> list[tuple[str, dict]]:
    """Every chart container has a unique id + valid metadata, matched 1:1
    by an Expand button, its own `timeField`/`seriesField` actually appear
    in its embedded data, and (where the server declares one) its
    `seriesOrder` matches the spec's own color domain exactly -- the
    dialog's series checkboxes and its legend must never disagree."""
    entries = _chart_entries(html_text)
    assert len(entries) >= expected_min, html_text[:200]

    ids = [chart_id for chart_id, _ in entries]
    assert len(ids) == len(set(ids)), f"duplicate chart ids on page: {ids}"

    buttons = _EXPAND_BUTTON_RE.findall(html_text)
    assert len(buttons) == len(entries), (
        f"{len(buttons)} Expand button(s) for {len(entries)} chart(s)"
    )

    for chart_id, meta in entries:
        assert meta["id"] == chart_id
        assert meta["title"]
        spec = _spec_for(html_text, chart_id)
        rows = _rows_of(spec)
        if meta.get("timeField") and rows:
            assert any(meta["timeField"] in row for row in rows), (
                f"{chart_id}: timeField {meta['timeField']!r} not in any row"
            )
        if meta.get("seriesField") and rows:
            assert any(meta["seriesField"] in row for row in rows), (
                f"{chart_id}: seriesField {meta['seriesField']!r} not in any row"
            )
        if meta.get("seriesOrder"):
            domain = _spec_color_domain(spec)
            assert domain == meta["seriesOrder"], (
                f"{chart_id}: seriesOrder {meta['seriesOrder']!r} != "
                f"spec color domain {domain!r}"
            )
    return entries


# --- chart_spec.chart_meta_json (pure function) ------------------------------


def test_chart_meta_json_shape():
    raw = chart_spec.chart_meta_json(
        chart_id="example",
        title="Example chart",
        time_field="window_end",
        time_type="month",
        series_field="tier",
        series_order=["First-time submitters", "2nd-5th submission", "6th+ submission"],
        has_band=True,
        params=[{"name": "cutoff", "selector": "[data-x]"}],
    )
    assert json.loads(raw) == {
        "id": "example",
        "title": "Example chart",
        "timeField": "window_end",
        "timeType": "month",
        "seriesField": "tier",
        "seriesOrder": ["First-time submitters", "2nd-5th submission", "6th+ submission"],
        "hasBand": True,
        "params": [{"name": "cutoff", "selector": "[data-x]"}],
    }


def test_chart_meta_json_defaults():
    raw = chart_spec.chart_meta_json(chart_id="x", title="X")
    meta = json.loads(raw)
    assert meta["timeField"] is None
    assert meta["timeType"] == "month"
    assert meta["seriesField"] is None
    assert meta["seriesOrder"] == []
    assert meta["hasBand"] is False
    assert meta["params"] == []


# --- Home page: CHAOSS Starter cards (issue #136) ---------------------------


def test_home_page_chaoss_cards_wired(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = (out_dir / "index.html").read_text()
    entries = _assert_charts_wired(html_text)
    assert all(chart_id.startswith("chaoss-") for chart_id, _ in entries)
    for _, meta in entries:
        assert meta["timeField"] == "window_end"
        assert meta["timeType"] == "month"
        assert meta["seriesField"] is None


# --- Community page: plain metric cards + review-responsiveness + pr-backlog


def test_community_page_metric_cards_wired(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = (out_dir / "community" / "index.html").read_text()
    entries = _assert_charts_wired(html_text)
    by_id = dict(entries)
    assert "reviewer_hhi" in by_id
    assert by_id["reviewer_hhi"]["timeField"] == "window_end"
    assert by_id["reviewer_hhi"]["seriesField"] is None


def test_community_page_review_responsiveness_chart_wired(tmp_path):
    out_dir, _ = _build_site_with_review_responsiveness(tmp_path)
    html_text = (out_dir / "community" / "index.html").read_text()
    entries = _assert_charts_wired(html_text)
    by_id = dict(entries)
    meta = by_id["review-responsiveness-trailing12m"]
    assert meta["timeField"] == "window_end"
    assert meta["seriesField"] == "tier"
    assert meta["seriesOrder"] == [
        "First-time submitters",
        "2nd-5th submission",
        "6th+ submission",
    ]


def test_community_page_pr_backlog_charts_wired(tmp_path):
    out_dir, _ = _build_site_with_pr_backlog(tmp_path)
    html_text = (out_dir / "community" / "index.html").read_text()
    entries = _assert_charts_wired(html_text)
    by_id = dict(entries)
    for chart_id in ("pr-backlog-age", "pr-backlog-ticket"):
        assert by_id[chart_id]["timeField"] == "month"
        assert by_id[chart_id]["seriesField"] == "bucket"
    assert by_id["pr-backlog-age"]["seriesOrder"] == ["<30d", "30-90d", "90d-1y", "1-3y", ">3y"]
    assert by_id["pr-backlog-ticket"]["seriesOrder"] == [
        "Still open",
        "Fixed",
        "Closed (other)",
        "No ticket key",
    ]


# --- Conversations page: metric cards, tone-mix, yoy, message patterns -----


def test_conversations_page_charts_wired(tmp_path):
    out_dir, _ = _build_site_with_conversation_patterns(tmp_path)
    html_text = (out_dir / "conversations" / "index.html").read_text()
    entries = _assert_charts_wired(html_text)
    by_id = dict(entries)

    tone_meta = by_id["tone-mix-mailing_list"]
    assert tone_meta["timeField"] == "quarter"
    assert tone_meta["timeType"] == "quarter"
    assert tone_meta["seriesField"] == "tier_name"
    assert {p["name"] for p in tone_meta["params"]} == {"mode", "cutoff"}
    # -2 (closing/positive) .. 4 (attack), never alphabetical.
    assert tone_meta["seriesOrder"][0] == "Closing/positive"
    assert tone_meta["seriesOrder"][-1] == "Attack"
    assert len(tone_meta["seriesOrder"]) == 7

    yoy_meta = by_id["yoy-constructive"]
    assert yoy_meta["timeField"] == "year"
    assert yoy_meta["timeType"] == "year"
    assert {p["name"] for p in yoy_meta["params"]} == {"venue", "from", "to", "cutoff"}

    msgpat_meta = by_id["msgpat-mailing_list-constructive"]
    assert msgpat_meta["timeField"] == "year"
    assert msgpat_meta["hasBand"] is True


# --- Thread explorer charts (issue #122, #124) -------------------------------


def test_thread_explorer_charts_wired(tmp_path):
    out_dir, _ = _build_site_with_threads(tmp_path)
    html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
    entries = _assert_charts_wired(html_text)
    by_id = dict(entries)
    assert by_id["threads-outcome"]["seriesField"] == "outcome"
    assert {p["name"] for p in by_id["threads-outcome"]["params"]} == {
        "venue",
        "year_from",
        "year_to",
    }
    assert by_id["threads-outcome"]["seriesOrder"][0] == "none"
    assert by_id["threads-label-constructive"]["timeField"] == "year"


# --- Governance page: compliance-trend cards + ninja trend ------------------


def test_governance_page_trend_charts_wired(tmp_path):
    out_dir, _ = _build_site_with_governance(tmp_path)
    html_text = (out_dir / "governance" / "index.html").read_text()
    entries = _assert_charts_wired(html_text)
    assert all(chart_id.startswith("gov-trend-") for chart_id, _ in entries)
    for _, meta in entries:
        assert meta["timeField"] == "month"
        assert meta["seriesField"] is None


# --- Peers page (issue #145) -------------------------------------------------


def test_peers_page_charts_wired(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_peers_main_snapshot(data_dir)
    _write_peers_manifest(data_dir)
    _write_peers_snapshot(data_dir)
    generate(data_dir, PEERS_RUN_ID, out_dir, now=BUILD_TIME)

    html_text = (out_dir / "peers" / "index.html").read_text()
    entries = _assert_charts_wired(html_text)
    for _, meta in entries:
        assert meta["timeField"] == "month"
        assert meta["seriesField"] == "project"
        # Cassandra first (D30), never alphabetical ("Apache Cassandra"
        # would otherwise sort first anyway -- "Apache DataFusion" is the
        # tell: it must stay *last*, not alphabetically second).
        assert meta["seriesOrder"][0] == "Apache Cassandra"
        assert meta["seriesOrder"][-1] == "Apache DataFusion"


# --- No-JS: the Expand button is hidden without JavaScript ------------------


def test_expand_button_hidden_without_js(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = (out_dir / "community" / "index.html").read_text()
    # The hiding rule lives once in base.html, shared by every page.
    assert ".chart-expand-btn { display: none; }" in html_text
    assert "<noscript>" in html_text


# --- The dialog markup itself is present exactly once per page --------------


def test_expand_dialog_present_on_every_page(tmp_path):
    out_dir = _build_site(tmp_path)
    for page in ("index.html", "community/index.html", "conversations/index.html"):
        html_text = (out_dir / page).read_text()
        assert html_text.count('id="chart-expand-dialog"') == 1
        assert "static/chart_expand.js" in html_text
