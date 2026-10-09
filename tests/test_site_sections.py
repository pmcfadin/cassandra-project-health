"""Tests for the collapsible, CHAOSS-topic-grouped page sections (issue
#144: "Layout: collapsible sections with summary rows, grouped by CHAOSS
practitioner-guide topics"). Covers the issue's own acceptance criteria:
section membership/orphans, anchors, and summary values matching card
values -- mirroring `test_site_thread_explorer.py`'s own self-contained
fixture pattern rather than importing `test_site.py`'s private helpers.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from project_health.schema import validate
from project_health.site.generate import generate
from project_health.site.metrics_meta import (
    COMMUNITY_METRIC_SECTION,
    COMMUNITY_SECTIONS,
    GOVERNANCE_METRICS,
    GOVERNANCE_SECTIONS,
    M0_METRICS,
    PR_BACKLOG_METRICS,
    _validate_community_sections,
)

RUN_ID = "2026-10-09-sect001"
CODE_SHA = "sect000001sect000001sect000001sect00001"
BUILD_TIME = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _metric_value_row(
    metric_id: str,
    window_start: date,
    window_end: date,
    value: float | None,
    n: int,
    flag: str,
    *,
    run_id: str = RUN_ID,
) -> dict:
    return {
        "metric_id": metric_id,
        "definition_version": "1.0",
        "window_start": window_start,
        "window_end": window_end,
        "value": value,
        "n": n,
        "flag": flag,
        "run_id": run_id,
        "computed_at": datetime(2026, 10, 9, 6, 30, tzinfo=UTC),
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
            _metric_value_row(metric_id, date(2026, 7, 1), date(2026, 7, 31), 10.0, 12, "ok")
        )
        rows.append(
            _metric_value_row(metric_id, date(2026, 8, 1), date(2026, 8, 31), 14.0, 15, "ok")
        )
    return rows


def _write_snapshot(data_dir: Path, run_id: str, rows: list[dict]) -> None:
    table = _metric_value_table(rows)
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, snapshot_dir / "metrics.parquet")


def _write_manifest(data_dir: Path, run_id: str, *, completed_at: datetime) -> None:
    manifest = {
        "run_id": run_id,
        "trigger": "schedule",
        "started_at": "2026-10-09T06:17:00Z",
        "completed_at": completed_at.isoformat(),
        "pipeline_code_sha": CODE_SHA,
        "sources": {
            "git": {"status": "ok", "watermark": "sha:deadbeef", "records_collected": 42},
            "jira": {"status": "ok", "watermark": "2026-10-09T04:00:00Z", "records_collected": 118},
        },
        "metrics_computed": list(M0_METRICS),
        "metrics_skipped_insufficient_data": [],
        "data_branch_commit": "d4e5f6",
        "site_deploy_status": "ok",
    }
    manifests_dir = data_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{run_id}.json").write_text(json.dumps(manifest))


def _build_site(tmp_path: Path, rows: list[dict] | None = None) -> Path:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, rows if rows is not None else _default_rows())
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    return out_dir


def _page_html(out_dir: Path, page: str) -> str:
    return (out_dir / page / "index.html").read_text()


def _visible_text(html_text: str) -> str:
    import html as html_module

    return html_module.unescape(re.sub(r"<[^>]+>", "", html_text))


# --- Community: section membership / no orphan, no duplicate (issue #144's
# own acceptance criterion) -------------------------------------------------


def test_community_metric_section_covers_every_community_metric_exactly_once():
    community_metric_ids = {
        meta.metric_id
        for meta in (*M0_METRICS.values(), *PR_BACKLOG_METRICS.values())
        if meta.page == "community"
    }
    mapped_ids = list(COMMUNITY_METRIC_SECTION)
    # No duplicate keys (a dict can't literally have one, but guards against
    # a future refactor that builds this map programmatically).
    assert len(mapped_ids) == len(set(mapped_ids))
    # No orphan: every community-page metric is mapped.
    assert community_metric_ids == set(mapped_ids)
    # No stray entry for a metric that isn't actually on the community page.
    section_ids = {meta.section_id for meta in COMMUNITY_SECTIONS}
    assert set(COMMUNITY_METRIC_SECTION.values()) <= section_ids


def test_community_metric_section_validator_catches_an_orphaned_metric():
    """The same fail-fast validator `metrics_meta` runs at import time
    (`_validate_community_sections`) must actually detect a missing
    mapping -- exercised directly here against a monkeypatched map so this
    test fails loudly if that function is ever gutted into a no-op."""
    import project_health.site.metrics_meta as metrics_meta_module

    original = dict(metrics_meta_module.COMMUNITY_METRIC_SECTION)
    try:
        metrics_meta_module.COMMUNITY_METRIC_SECTION.pop("active_contributors_monthly")
        with pytest.raises(ValueError, match="no COMMUNITY_METRIC_SECTION entry"):
            _validate_community_sections()
    finally:
        metrics_meta_module.COMMUNITY_METRIC_SECTION.clear()
        metrics_meta_module.COMMUNITY_METRIC_SECTION.update(original)


def test_community_metric_section_validator_catches_a_stray_entry():
    import project_health.site.metrics_meta as metrics_meta_module

    original = dict(metrics_meta_module.COMMUNITY_METRIC_SECTION)
    try:
        metrics_meta_module.COMMUNITY_METRIC_SECTION["not_a_real_metric_id"] = "responsiveness"
        with pytest.raises(ValueError, match="non-community metric"):
            _validate_community_sections()
    finally:
        metrics_meta_module.COMMUNITY_METRIC_SECTION.clear()
        metrics_meta_module.COMMUNITY_METRIC_SECTION.update(original)


def test_community_section_headline_metric_ids_belong_to_their_own_section():
    for meta in COMMUNITY_SECTIONS:
        for metric_id in meta.headline_metric_ids:
            assert COMMUNITY_METRIC_SECTION[metric_id] == meta.section_id


# --- Community: section anchors (collapsed by default, hash-openable) -----


def test_community_page_renders_one_details_section_per_community_section(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "community")

    for meta in COMMUNITY_SECTIONS:
        section_id = re.escape(meta.section_id)
        pattern = rf'<details class="page-section [^"]*" id="{section_id}"(?!\s+open)'
        assert re.search(pattern, html_text), f"{meta.section_id!r} missing or open by default"
        assert html_text.count(f'id="{meta.section_id}"') == 1, meta.section_id
        assert meta.title in html_text


def test_community_jump_nav_links_to_every_section(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "community")
    nav_start = html_text.index('<nav class="page-section-nav"')
    nav_end = html_text.index("</nav>", nav_start)
    nav_html = html_text[nav_start:nav_end]
    for meta in COMMUNITY_SECTIONS:
        assert f'href="#{meta.section_id}"' in nav_html


def test_community_chaoss_links_present_for_every_section(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "community")
    for meta in COMMUNITY_SECTIONS:
        assert meta.chaoss_url is not None
        assert f'href="{meta.chaoss_url}"' in html_text


# --- Community: summary-row values match the expanded card's own value ----


def test_community_section_summary_value_matches_card_value(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "community")

    # active_contributors_monthly is a headline metric of the
    # "contributor-sustainability" section; `_default_rows` gives it a
    # latest (Aug 2026) formatted value of "14".
    assert html_text.count(">14</span>") >= 2
    assert "Aug 2026" in html_text
    # The summary row's own markup carries the same value class as the
    # headline metric shell, immediately followed by the month label.
    idx = html_text.index("section-headline-metric-name\">Active Contributors<")
    nearby = html_text[idx : idx + 300]
    assert ">14</span>" in nearby
    assert "Aug 2026" in nearby


def test_community_section_summary_shows_insufficient_data_honestly(tmp_path):
    """A headline metric with no `flag == 'ok'` point renders "insufficient
    data" in its section's summary row -- never a fabricated number."""
    rows = [
        row for row in _default_rows() if row["metric_id"] != "pmc_joins_quarterly"
    ]
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = _page_html(out_dir, "community")

    idx = html_text.index('id="leadership"')
    section_html = html_text[idx : idx + 2000]
    assert "section-headline-metric-value--insufficient" in section_html
    assert "insufficient data" in section_html


# --- Governance sections ----------------------------------------------------


def _fact_metric_row(
    metric_id: str, window_start: date, window_end: date, *, hit: int, n: int
) -> dict:
    return {
        "metric_id": metric_id,
        "definition_version": "1.0",
        "window_start": window_start,
        "window_end": window_end,
        "value": (hit / n) if n else None,
        "n": n,
        "flag": "ok" if n else "insufficient_data",
        "run_id": RUN_ID,
        "computed_at": datetime(2026, 10, 9, 6, 30, tzinfo=UTC),
        "details_json": json.dumps({"n_total": n}),
    }


def _write_governance_metric_snapshot(data_dir: Path, run_id: str) -> None:
    from project_health.governance.checks import result_state
    from project_health.schema import get_schema

    rows = [
        _fact_metric_row(metric_id, date(2026, 7, 1), date(2026, 7, 31), hit=5, n=8)
        for metric_id in GOVERNANCE_METRICS
    ]
    table = _metric_value_table(rows)
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, snapshot_dir / "governance_metric_value.parquet")

    commit_rows = [
        {
            "sha": "1111111111111111111111111111111111aaaa",
            "branch": "trunk",
            "commit_date": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            "changes_txt_touched": True,
            "news_txt_touched": False,
            "test_touched": True,
        }
    ]
    fact_table = validate(
        "commit_fact", pa.Table.from_pylist(commit_rows, schema=get_schema("commit_fact"))
    )
    pq.write_table(fact_table, snapshot_dir / "governance_commit_fact.parquet")

    # `governance.has_data` (governance_page.py) gates on this table alone
    # (`bool(compliance_rows)`) -- without at least one row the page renders
    # its honest empty state instead of the three collapsible sections.
    compliance_row = {
        "sha": "1111111111111111111111111111111111aaaa",
        "branch": "trunk",
        "commit_date": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        "author": "Jane Author",
        "committer": "Jane Author",
        "subject": "Fix a bug",
        "is_merge": False,
        "reviewers": [],
        "jira_keys": ["CASSANDRA-90001"],
        "policy_version": 1,
        "check_id": "reviewer-present",
        "result": "fail",
        "evidence": "no reviewer found",
        "evidence_url": None,
        "evidence_kind": None,
        "evidence_label": None,
        "evidence_at": None,
        "lead_time_seconds": None,
        "reason": None,
        "reviewer_detail": [],
    }
    compliance_row["state"] = result_state(compliance_row["result"])
    compliance_table = validate(
        "commit_compliance",
        pa.Table.from_pylist([compliance_row], schema=get_schema("commit_compliance")),
    )
    pq.write_table(compliance_table, snapshot_dir / "governance_commit_compliance.parquet")


def _build_site_with_governance(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_governance_metric_snapshot(data_dir, RUN_ID)
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    return out_dir


def test_governance_sections_anchors_and_default_open_state(tmp_path):
    out_dir = _build_site_with_governance(tmp_path)
    html_text = _page_html(out_dir, "governance")

    for meta in GOVERNANCE_SECTIONS:
        assert html_text.count(f'id="{meta.section_id}"') == 1, meta.section_id
        assert meta.title in html_text

    # "Facts summary" and "References" default collapsed; "Commit history"
    # -- the page's main content -- defaults open (issue #144's own
    # grouping spec).
    for section_id in ("facts-summary", "references"):
        pattern = rf'<details class="page-section [^"]*" id="{section_id}"(?!\s+open)>'
        assert re.search(pattern, html_text), section_id
    assert re.search(r'<details class="page-section [^"]*" id="commit-history" open>', html_text)


def test_governance_facts_summary_shows_headline_shares(tmp_path):
    out_dir = _build_site_with_governance(tmp_path)
    html_text = _page_html(out_dir, "governance")
    idx = html_text.index('id="facts-summary"')
    section_html = html_text[idx : idx + 3000]
    # 5/8 -> 62.5%, rendered to one decimal by MetricMeta.format_value.
    assert "62.5%" in section_html


# --- Conversations sections --------------------------------------------------


def test_conversations_section_ids_present_in_new_order(tmp_path):
    """Issue #144's owner-approved order: Summary -> Newcomer treatment ->
    How disagreements go -> Message patterns (YoY + facets) -> Method &
    limits -- exercised with the full conversation-patterns fixture pulled
    in from `test_site.py`'s own helper to avoid re-deriving its shape."""
    from tests.test_site import _build_site_with_conversation_patterns

    out_dir, _ = _build_site_with_conversation_patterns(tmp_path)
    html_text = _page_html(out_dir, "conversations")

    section_ids = [
        "conv-summary",
        "conv-newcomer",
        "conv-disagreement",
        "conv-message-patterns",
        "conv-method",
    ]
    for section_id in section_ids:
        needle = f'<details class="page-section " id="{section_id}">'
        assert html_text.count(needle) == 1, section_id
    indices = [html_text.index(f'id="{s}"') for s in section_ids]
    assert indices == sorted(indices)


def test_conversations_preliminary_banner_outside_sections(tmp_path):
    """The preliminary banner stays outside/above every collapsible section
    (issue #144), never inside a `<details class="page-section">` body."""
    from tests.test_site import _build_site_with_conversation_patterns

    out_dir, _ = _build_site_with_conversation_patterns(tmp_path)
    html_text = _page_html(out_dir, "conversations")

    banner_index = html_text.index('banner--preliminary')
    first_section_index = html_text.index('<details class="page-section')
    assert banner_index < first_section_index


# --- Verdict-vocabulary / neutral-wording guard (issue #144's constraint:
# "summaries are numbers, not sentences of interpretation") -----------------


def test_section_chrome_never_renders_verdict_vocabulary(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "community")
    text = _visible_text(html_text).lower()

    for banned in ("fail", "failing", "pass rate", "exempt", "verdict", "healthy", "unhealthy"):
        assert re.search(rf"\b{re.escape(banned)}\b", text) is None, banned


def test_releases_section_links_release_frequency_metric_not_a_guide():
    from project_health.site.metrics_meta import COMMUNITY_SECTIONS

    releases = next(s for s in COMMUNITY_SECTIONS if s.section_id == "releases")
    assert releases.chaoss_url.endswith("/kb/metric-release-frequency/")
    assert releases.chaoss_label == "CHAOSS: Release Frequency"
    guides = [s for s in COMMUNITY_SECTIONS if s.section_id != "releases"]
    assert all(s.chaoss_label == "CHAOSS practitioner guide" for s in guides)
