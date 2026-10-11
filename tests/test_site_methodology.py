"""Tests for the `/methodology/` and `/corrections/` pages (issue #160).

Self-contained fixture pattern mirroring `test_site_sections.py` rather than
importing `test_site.py`'s private helpers (that file's own stated
convention). Covers this issue's own acceptance criteria: metric-catalogue
completeness with working anchors, internal links/anchors resolving on both
pages, the four existing correction templates plus the new general one all
present, the footer linking to both pages from every page, and the
verdict-vocabulary ban extended to these two new pages.
"""

from __future__ import annotations

import html as html_module
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from project_health.schema import validate
from project_health.site.generate import generate
from project_health.site.metrics_meta import (
    GOVERNANCE_METRICS,
    M0_METRICS,
    PR_BACKLOG_METRICS,
    metric_catalogue,
)

RUN_ID = "2026-10-10-meth001"
CODE_SHA = "meth0000meth0000meth0000meth0000meth000"
BUILD_TIME = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

REPO_ROOT = Path(__file__).resolve().parents[1]


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
        "computed_at": datetime(2026, 10, 10, 6, 30, tzinfo=UTC),
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


def _write_governance_snapshot(data_dir: Path, run_id: str) -> None:
    """Minimal governance evidence so `governance.has_data` is true and the
    per-check cards (this issue's catalogue anchors for `GOVERNANCE_METRICS`)
    actually render -- mirrors `test_site_sections.py::
    _write_governance_metric_snapshot`."""
    from project_health.governance.checks import result_state
    from project_health.schema import get_schema

    def _fact_row(metric_id: str) -> dict:
        return {
            "metric_id": metric_id,
            "definition_version": "1.0",
            "window_start": date(2026, 7, 1),
            "window_end": date(2026, 7, 31),
            "value": 0.6,
            "n": 8,
            "flag": "ok",
            "run_id": run_id,
            "computed_at": datetime(2026, 10, 10, 6, 30, tzinfo=UTC),
            "details_json": None,
        }

    fact_rows = [_fact_row(metric_id) for metric_id in GOVERNANCE_METRICS]
    table = _metric_value_table(fact_rows)
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, snapshot_dir / "governance_metric_value.parquet")

    commit_fact_rows = [
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
        "commit_fact", pa.Table.from_pylist(commit_fact_rows, schema=get_schema("commit_fact"))
    )
    pq.write_table(fact_table, snapshot_dir / "governance_commit_fact.parquet")

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


def _write_manifest(data_dir: Path, run_id: str, *, completed_at: datetime) -> None:
    import json

    manifest = {
        "run_id": run_id,
        "trigger": "schedule",
        "started_at": "2026-10-10T06:17:00Z",
        "completed_at": completed_at.isoformat(),
        "pipeline_code_sha": CODE_SHA,
        "sources": {
            "git": {"status": "ok", "watermark": "sha:deadbeef", "records_collected": 42},
            "jira": {"status": "ok", "watermark": "2026-10-10T04:00:00Z", "records_collected": 118},
        },
        "metrics_computed": list(M0_METRICS),
        "metrics_skipped_insufficient_data": [],
        "data_branch_commit": "d4e5f6",
        "site_deploy_status": "ok",
    }
    manifests_dir = data_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{run_id}.json").write_text(json.dumps(manifest))


def _build_site(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_governance_snapshot(data_dir, RUN_ID)
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    return out_dir


def _page_html(out_dir: Path, page: str) -> str:
    return (out_dir / page / "index.html").read_text()


def _visible_text(html_text: str) -> str:
    return html_module.unescape(re.sub(r"<[^>]+>", "", html_text))


def _ids_in(html_text: str) -> set[str]:
    return set(re.findall(r'\sid="([^"]+)"', html_text))


def _hrefs_in(html_text: str) -> list[str]:
    return re.findall(r'<a\s[^>]*href="([^"]+)"', html_text)


# --- Pages generated, footer wired ------------------------------------------


def test_methodology_and_corrections_pages_are_generated(tmp_path):
    out_dir = _build_site(tmp_path)
    assert (out_dir / "methodology" / "index.html").is_file()
    assert (out_dir / "corrections" / "index.html").is_file()


def test_footer_links_to_methodology_and_corrections_on_every_page(tmp_path):
    out_dir = _build_site(tmp_path)
    prefix_by_page = {
        "": "./",
        "community/": "../",
        "conversations/": "../",
        "governance/": "../",
        "methodology/": "../",
        "corrections/": "../",
    }
    for page, prefix in prefix_by_page.items():
        html_text = _page_html(out_dir, page)
        footer_start = html_text.index('<footer class="site-footer">')
        footer_html = html_text[footer_start:]
        assert f'href="{prefix}methodology/"' in footer_html, page
        assert f'href="{prefix}corrections/"' in footer_html, page


def test_nav_is_unchanged_methodology_and_corrections_not_added(tmp_path):
    """Issue #160's own constraint: "nav unchanged otherwise" -- the primary
    nav bar still lists exactly Home/Community/Conversations/Governance
    (plus Peers once available), never Methodology/Corrections."""
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "methodology/")
    nav_start = html_text.index('<nav class="site-nav"')
    nav_end = html_text.index("</nav>", nav_start)
    nav_html = html_text[nav_start:nav_end]
    assert ">Methodology<" not in nav_html
    assert ">Corrections<" not in nav_html


# --- Metric catalogue completeness (issue #160's own required test) --------


def test_metric_catalogue_lists_every_published_metric_exactly_once():
    all_metric_ids = {*M0_METRICS, *GOVERNANCE_METRICS, *PR_BACKLOG_METRICS}
    catalogue = metric_catalogue()
    catalogue_ids = [entry.metric_id for entry in catalogue]
    # No duplicates.
    assert len(catalogue_ids) == len(set(catalogue_ids))
    # No orphan, no stray.
    assert set(catalogue_ids) == all_metric_ids
    # Every entry carries a name, a one-line definition, a tier, and a window.
    for entry in catalogue:
        assert entry.name
        assert entry.definition
        assert entry.tier
        assert entry.window
        assert entry.anchor_href


def test_metric_catalogue_every_anchor_resolves_on_the_rendered_site(tmp_path):
    """Issue #160's required test: "catalogue completeness (every published
    metric_id appears once with a working anchor)" -- actually renders the
    site and follows each catalogue row's anchor to confirm the target id
    exists on the target page."""
    out_dir = _build_site(tmp_path)
    methodology_html = _page_html(out_dir, "methodology/")

    # Every metric_id appears exactly once in the catalogue table.
    for metric_id in (*M0_METRICS, *GOVERNANCE_METRICS, *PR_BACKLOG_METRICS):
        assert methodology_html.count(f'id="catalogue-{metric_id}"') == 1, metric_id

    page_html_by_dir = {
        "community": _page_html(out_dir, "community/"),
        "conversations": _page_html(out_dir, "conversations/"),
        "governance": _page_html(out_dir, "governance/"),
    }
    for entry in metric_catalogue():
        page_dir, _, fragment = entry.anchor_href.partition("#")
        page_dir = page_dir.rstrip("/")
        assert page_dir in page_html_by_dir, (entry.metric_id, page_dir)
        target_ids = _ids_in(page_html_by_dir[page_dir])
        assert fragment in target_ids, (entry.metric_id, fragment)


# --- Internal link/anchor resolution on both pages --------------------------


def _assert_internal_links_resolve(out_dir: Path, from_page: str) -> None:
    html_text = _page_html(out_dir, from_page)
    own_ids = _ids_in(html_text)
    for href in _hrefs_in(html_text):
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            continue  # external link, not checked here
        if href.startswith("#"):
            fragment = href[1:]
            assert fragment in own_ids, (from_page, href)
            continue
        target_dir = (out_dir / from_page / parts.path).resolve()
        assert str(target_dir).startswith(str(out_dir.resolve())), (from_page, href)
        candidate = target_dir / "index.html" if target_dir.is_dir() else target_dir
        if not candidate.is_file():
            candidate = (out_dir / from_page).joinpath(parts.path).resolve()
        assert candidate.is_file(), (from_page, href, candidate)
        if parts.fragment:
            target_ids = _ids_in(candidate.read_text())
            assert parts.fragment in target_ids, (from_page, href)


def test_methodology_internal_links_and_anchors_resolve(tmp_path):
    out_dir = _build_site(tmp_path)
    _assert_internal_links_resolve(out_dir, "methodology/")


def test_corrections_internal_links_and_anchors_resolve(tmp_path):
    out_dir = _build_site(tmp_path)
    _assert_internal_links_resolve(out_dir, "corrections/")


# --- Verdict vocabulary ban extended to both new pages ----------------------


def test_methodology_and_corrections_never_render_verdict_vocabulary(tmp_path):
    out_dir = _build_site(tmp_path)
    banned = (
        "fail", "failing", "pass rate", "exempt", "not in force", "verdict",
        "threshold", "healthy", "unhealthy", "improving", "declining",
        "health score", "stable", "composite",
    )
    for page in ("methodology/", "corrections/"):
        text = _visible_text(_page_html(out_dir, page)).lower()
        for word in banned:
            assert re.search(rf"\b{re.escape(word)}\b", text) is None, (page, word)


# --- Correction templates present -------------------------------------------


def test_all_five_correction_issue_templates_exist_and_are_valid():
    template_dir = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
    expected = (
        "identity-correction.yml",
        "governance-correction.yml",
        "thread-score-disagreement.yml",
        "conversation-patterns-feedback.yml",
        "data-correction.yml",
    )
    for filename in expected:
        path = template_dir / filename
        assert path.is_file(), filename
        parsed = yaml.safe_load(path.read_text())
        assert parsed["name"]
        assert parsed["body"]


def test_corrections_page_links_all_five_templates(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "corrections/")
    for template in (
        "identity-correction.yml",
        "governance-correction.yml",
        "thread-score-disagreement.yml",
        "conversation-patterns-feedback.yml",
        "data-correction.yml",
    ):
        assert f"template={template}" in html_text, template


def test_corrections_page_states_no_committed_response_time(tmp_path):
    out_dir = _build_site(tmp_path)
    text = _visible_text(_page_html(out_dir, "corrections/"))
    assert "no committed response time" in text.lower()


# --- Facts are sourced from code/config, not hard-coded ---------------------


def test_methodology_page_states_pinned_model_k_and_seed_from_code(tmp_path):
    from project_health.classify.questions import EXPECTED_MODEL
    from project_health.private_run.sample import DEFAULT_K, DEFAULT_SEED

    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "methodology/")
    assert EXPECTED_MODEL in html_text
    assert f"K={DEFAULT_K}" in html_text
    assert f"seed={DEFAULT_SEED}" in html_text
