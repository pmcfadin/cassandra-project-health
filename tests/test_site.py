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

from project_health import storage
from project_health.schema import get_schema, validate
from project_health.site.generate import generate
from project_health.site.metrics_meta import M0_METRICS, PAGES

SITE_PAGES = ["", "community/", "conversations/", "governance/"]

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


def _scorecard_row(**overrides) -> dict:
    row = {
        "check_id": "check-1",
        "repo": "github.com/apache/cassandra",
        "scorecard_date": date(2026, 9, 21),
        "scorecard_version": "v5.5.1-0.20260908181711-f92023a3f778",
        "overall_score": 4.6,
        "check_name": "Maintained",
        "check_score": 10.0,
        "check_reason": "30 commit(s) and 0 issue activity found in the last 90 days",
        "check_details_summary": None,
        "source_snapshot_id": "run-1:security",
        "collected_at": datetime(2026, 9, 25, 6, 0, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _advisory_row(**overrides) -> dict:
    row = {
        "advisory_id": "adv-1",
        "cve_id": "CVE-2025-26467",
        "published_date": date(2025, 8, 25),
        "last_modified_date": None,
        "severity": "HIGH",
        "cvss_score": 8.8,
        "cvss_version": "3.1",
        "summary": "A vulnerability in Apache Cassandra.",
        "affected_versions": "3.0.0–3.0.31",
        "fixed_versions": "3.0.31",
        "advisory_url": "https://nvd.nist.gov/vuln/detail/CVE-2025-26467",
        "source": "nvd",
        "source_snapshot_id": "run-1:security",
        "collected_at": datetime(2026, 9, 25, 6, 0, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _write_security_partitions(
    data_dir: Path,
    run_id: str,
    *,
    scorecard_rows: list[dict] | None = None,
    advisory_rows: list[dict] | None = None,
) -> None:
    if scorecard_rows:
        table = validate(
            "scorecard_check",
            pa.Table.from_pylist(scorecard_rows, schema=get_schema("scorecard_check")),
        )
        storage.write_partition(
            data_dir, "security", "scorecard_check", date(2026, 9, 25), run_id, table
        )
    if advisory_rows:
        table = validate(
            "security_advisory",
            pa.Table.from_pylist(advisory_rows, schema=get_schema("security_advisory")),
        )
        storage.write_partition(
            data_dir, "security", "security_advisory", date(2026, 9, 25), run_id, table
        )


def _page_html(out_dir: Path, page: str) -> str:
    """Read a rendered page's HTML. `page` is `""` for home or one of
    `"community/"`, `"conversations/"`, `"governance/"` for a subpage
    (D13: every page other than home lives at `<page>/index.html`)."""
    return (out_dir / page / "index.html").read_text()


def _community_html(out_dir: Path) -> str:
    return _page_html(out_dir, "community/")


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


# --- Multi-page site (D13, issue #34) ---------------------------------------


def test_all_four_pages_are_generated(tmp_path):
    out_dir = _build_site(tmp_path)
    for page in SITE_PAGES:
        assert (out_dir / page / "index.html").is_file(), page


def test_nav_present_on_every_page_with_current_page_marked(tmp_path):
    out_dir = _build_site(tmp_path)
    page_to_label = {
        "": "Home",
        "community/": "Community",
        "conversations/": "Conversations",
        "governance/": "Governance",
    }
    for page, label in page_to_label.items():
        html_text = _page_html(out_dir, page)
        assert '<nav class="site-nav"' in html_text
        # All four nav labels are present as links on every page...
        for other_label in page_to_label.values():
            assert f">{other_label}</a>" in html_text
        # ...but only the current page's link is marked aria-current, and
        # it's the anchor whose text is this page's own nav label.
        assert html_text.count('aria-current="page"') == 1
        nav_start = html_text.index('<nav class="site-nav"')
        nav_end = html_text.index("</nav>", nav_start)
        nav_html = html_text[nav_start:nav_end]
        assert f'aria-current="page">{label}</a>' in nav_html


def test_home_summary_card_shows_community_headline_metrics(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "")

    assert 'href="./community/"' in html_text
    assert 'href="./conversations/"' in html_text
    assert 'href="./governance/"' in html_text
    # A headline metric's value and month are shown on the home page.
    assert "Aug 2026" in html_text


def test_home_summary_card_is_honest_when_a_page_has_no_metrics(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "")

    assert PAGES["conversations"].empty_message in html_text
    assert PAGES["governance"].empty_message in html_text


def test_conversations_page_explains_whats_coming(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "conversations/")

    assert "No conversation metrics yet" in html_text
    assert "Phase 2a" in html_text
    assert "Phase 2b" in html_text
    assert "interaction health, not raw sentiment" in html_text.replace("\n", " ")
    assert "COMMUNITY-HEALTH.md" in html_text


def test_governance_page_is_a_placeholder_linking_to_decisions(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert "not published yet" in html_text
    assert "DECISIONS.md" in html_text
    assert "D14" in html_text
    assert "D15" in html_text


# --- Security section (OpenSSF Scorecard + advisories, issue #55) -----------


def test_governance_page_security_section_is_honest_when_no_data_yet(tmp_path):
    """No `raw/security/*` partitions exist -- the section must say so
    rather than rendering an empty table."""
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert "Security" in html_text
    assert "No OpenSSF Scorecard data collected yet." in html_text
    assert "No CVE/advisory data collected yet." in html_text


def test_governance_page_renders_scorecard_per_check_never_score_alone(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    _write_security_partitions(
        data_dir,
        RUN_ID,
        scorecard_rows=[
            _scorecard_row(
                check_id="c1",
                check_name="Maintained",
                check_score=10.0,
                check_reason="30 commit(s) and 0 issue activity found in the last 90 days",
            ),
            _scorecard_row(
                check_id="c2",
                check_name="Code-Review",
                check_score=0.0,
                check_reason="Found 0/30 approved changesets -- score normalized to 0",
            ),
        ],
    )
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    html_text = _page_html(out_dir, "governance/")

    # Every check shows its own reason -- the aggregate is labeled as never
    # read alone (RESEARCH.md §6.2), and Code-Review's known blind spot gets
    # its "why" note (GOVERNANCE.md §11.1).
    assert "4.6/10" in html_text
    assert "never as one number" in html_text
    assert "Found 0/30 approved changesets" in html_text
    assert "commit-message trailers" in html_text or "commit-trailer" in html_text
    assert "30 commit(s) and 0 issue activity" in html_text
    assert "GitHub Issues" in html_text  # Maintained's "why" note


def test_governance_page_leads_with_per_check_table_never_a_headline_score(tmp_path):
    """Coordinator review (issue #55): the aggregate must never render as a
    large/headline number -- only as a small, secondary line under the
    per-check table. This asserts the per-check table appears in the markup
    before the aggregate line, and that the aggregate is never inside a
    heading or any element carrying the (removed) big-number styling class."""
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    _write_security_partitions(
        data_dir,
        RUN_ID,
        scorecard_rows=[_scorecard_row(check_name="Maintained", check_score=10.0)],
    )
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    html_text = _page_html(out_dir, "governance/")

    # The per-check table's header row comes before the aggregate line.
    table_index = html_text.index("<th scope=\"col\">Check</th>")
    aggregate_index = html_text.index("Scorecard's own aggregate:")
    assert table_index < aggregate_index

    # The aggregate score is never inside any heading tag (h1-h6).
    assert not re.search(r"<h[1-6][^>]*>[^<]*4\.6[^<]*</h[1-6]>", html_text)
    # The now-removed big-number styling class is never used anywhere.
    assert "security-score" not in html_text
    # The aggregate line itself renders with the small/secondary styling.
    assert 'class="security-meta">Scorecard\'s own aggregate: 4.6/10' in html_text


def test_governance_page_renders_advisory_table_and_per_year_counts(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    _write_security_partitions(
        data_dir,
        RUN_ID,
        advisory_rows=[
            _advisory_row(cve_id="CVE-2025-26467", published_date=date(2025, 8, 25)),
            _advisory_row(
                advisory_id="adv-2",
                cve_id="CVE-2026-27314",
                published_date=date(2026, 4, 7),
                severity="HIGH",
                cvss_score=8.8,
                fixed_versions="5.0.7",
            ),
        ],
    )
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    html_text = _page_html(out_dir, "governance/")

    assert "CVE-2025-26467" in html_text
    assert "CVE-2026-27314" in html_text
    assert "2025:" in html_text
    assert "2026:" in html_text
    assert "5.0.7" in html_text


def test_governance_page_dedupes_advisories_by_cve_across_runs(tmp_path):
    """A CVE re-collected in a later run (refreshed NVD metadata) shows once,
    with the newer summary -- not twice."""
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    _write_security_partitions(
        data_dir,
        "run-1",
        advisory_rows=[
            _advisory_row(
                summary="original fetch",
                collected_at=datetime(2026, 9, 1, tzinfo=UTC),
                source_snapshot_id="run-1:security",
            )
        ],
    )
    _write_security_partitions(
        data_dir,
        RUN_ID,
        advisory_rows=[
            _advisory_row(
                advisory_id="adv-refetched",
                summary="re-fetched, newer metadata",
                collected_at=datetime(2026, 9, 25, tzinfo=UTC),
                source_snapshot_id="run-2:security",
            )
        ],
    )
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    html_text = _page_html(out_dir, "governance/")

    # The cve_id appears twice per row (link text + href URL) -- one row,
    # not two, means it appears exactly twice, not four times.
    assert html_text.count("CVE-2025-26467") == 2
    assert "re-fetched, newer metadata" not in html_text  # summary isn't shown in the table
    # but the row itself should reflect the later fetch's affected/fixed data
    assert "3.0.31" in html_text


def test_governance_page_shows_scorecard_run_count_across_runs(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    _write_security_partitions(
        data_dir,
        "run-1",
        scorecard_rows=[_scorecard_row(source_snapshot_id="run-1:security", overall_score=4.6)],
    )
    _write_security_partitions(
        data_dir,
        RUN_ID,
        scorecard_rows=[
            _scorecard_row(
                check_id="c2",
                source_snapshot_id="run-2:security",
                overall_score=4.8,
                collected_at=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
            )
        ],
    )
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    html_text = _page_html(out_dir, "governance/")

    assert "2 Scorecard runs collected so far" in html_text


def test_community_page_has_the_metric_cards_and_charts(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _community_html(out_dir)

    for meta in M0_METRICS.values():
        assert meta.name in html_text
    assert 'data-vega-spec=' in html_text


def test_relative_data_and_static_links_from_a_subpage(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _community_html(out_dir)

    assert '../static/style.css' in html_text
    assert '../static/app.js' in html_text
    metric_id = next(iter(M0_METRICS))
    assert f'../data/{metric_id}.json' in html_text
    assert f'../data/{metric_id}.csv' in html_text


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


def test_reported_definition_version_is_the_max_not_the_latest_window(tmp_path):
    """Issue #27 / ARCHITECTURE.md §4.4: old-version rows are never
    overwritten -- a snapshot can hold both "1.0" and "1.1" rows for the
    same metric. The reported `definition_version` must be the numeric
    maximum among them, regardless of which row's `window_start` sorts
    last. Here the "1.0" row is deliberately given the LATEST window_start
    so a naive window_start-sort pick would wrongly report "1.0"."""
    metric_id = "new_contributors_monthly"
    rows = [
        _metric_value_row(
            metric_id,
            date(2026, 6, 1),
            date(2026, 6, 30),
            3.0,
            3,
            "ok",
            definition_version="1.1",
        ),
        # Deliberately the LATEST window_start, but the OLDER version.
        _metric_value_row(
            metric_id,
            date(2026, 7, 1),
            date(2026, 7, 31),
            10.0,
            12,
            "ok",
            definition_version="1.0",
        ),
    ]
    for other_id in M0_METRICS:
        if other_id == metric_id:
            continue
        rows.append(_metric_value_row(other_id, date(2026, 7, 1), date(2026, 7, 31), 1.0, 6, "ok"))

    out_dir = _build_site(tmp_path, rows=rows)
    payload = json.loads((out_dir / "data" / f"{metric_id}.json").read_text())

    assert payload["definition_version"] == "1.1"


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
    html_text = _community_html(out_dir)
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

    # href="/..." or src="/..." (a single leading slash, not "//" which is
    # protocol-relative and not part of this HTML anyway) would break the
    # site once it's served under the /cassandra-project-health/ subpath —
    # on every page, not just home (D13, issue #34).
    for page in SITE_PAGES:
        html_text = _page_html(out_dir, page)
        assert not re.search(r'(?:href|src)="/(?!/)', html_text), page


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


def test_all_m0_metrics_declare_the_community_page():
    """D13/issue #34: which page a metric renders on is declared once, in
    `MetricMeta.page`. All six M0 metrics are community (code/contributor)
    metrics."""
    for meta in M0_METRICS.values():
        assert meta.page == "community"
        assert meta.page in PAGES


def test_group_by_page_rejects_a_metric_with_an_unknown_page(tmp_path):
    """A metric whose `page` isn't one of `PAGES`' keys is a metadata bug
    (metrics_meta.py declares an unknown page) and must fail loudly, not
    silently vanish from every page."""
    from project_health.site.generate import MetricSeries, _group_by_page
    from project_health.site.metrics_meta import MetricMeta

    bad_meta = MetricMeta(
        metric_id="bogus_metric",
        name="Bogus",
        dimension="nowhere",
        tier="experimental",
        direction_of_good="none",
        value_kind="count",
        page="nonexistent_page",
    )
    series_by_id = {
        "bogus_metric": MetricSeries(meta=bad_meta, definition_version=None, points=[]),
    }
    with pytest.raises(ValueError, match="unknown page"):
        _group_by_page(series_by_id)


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
    html_text = _community_html(out_dir)

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
    html_text = _community_html(out_dir)

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
    html_text = _community_html(out_dir)
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
    html_text = _community_html(out_dir)
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
    html_text = _community_html(out_dir)

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
