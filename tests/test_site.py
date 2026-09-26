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
from project_health.site.metrics_meta import GOVERNANCE_METRICS, M0_METRICS, PAGES

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
    details_json: str | None = None,
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
        "details_json": details_json,
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
    governance: dict | None = None,
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
    if governance is not None:
        manifest["governance"] = governance
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


# --- Governance per-commit compliance fixtures (issue #37, D14/D15) --------


def _commit_compliance_row(**overrides) -> dict:
    row = {
        "sha": "1111111111111111111111111111111111aaaa",
        "branch": "trunk",
        "commit_date": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        "author": "Jane Author",
        "committer": "Jane Author",
        "is_merge": False,
        "reviewers": [],
        "jira_keys": ["CASSANDRA-90001"],
        "policy_version": 1,
        "check_id": "reviewer-present",
        "result": "fail",
        "evidence": (
            "CASSANDRA-N key referenced; no reviewer found in commit trailer or JIRA "
            "reviewer field(s); no exemption matched; no review wording present"
        ),
        "evidence_url": None,
    }
    row.update(overrides)
    return row


def _commit_fact_row(**overrides) -> dict:
    row = {
        "sha": "1111111111111111111111111111111111aaaa",
        "branch": "trunk",
        "commit_date": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        "changes_txt_touched": True,
        "news_txt_touched": False,
        "test_touched": True,
    }
    row.update(overrides)
    return row


def _governance_metric_row(
    check_id: str, window_start: date, window_end: date, counts: dict
) -> dict:
    from project_health.governance.metrics import metric_id_for_check

    scored = counts.get("pass", 0) + counts.get("fail", 0) + counts.get("unknown", 0)
    total = scored + counts.get("exempt", 0) + counts.get("not_in_force", 0)
    return {
        "metric_id": metric_id_for_check(check_id),
        "definition_version": "1.0",
        "window_start": window_start,
        "window_end": window_end,
        "value": (counts.get("pass", 0) / scored) if scored else None,
        "n": scored,
        "flag": "ok" if scored else "insufficient_data",
        "run_id": RUN_ID,
        "computed_at": datetime(2026, 9, 25, 6, 30, tzinfo=UTC),
        "details_json": json.dumps(
            {
                "check_id": check_id,
                "pass": counts.get("pass", 0),
                "fail": counts.get("fail", 0),
                "unknown": counts.get("unknown", 0),
                "exempt": counts.get("exempt", 0),
                "not_in_force": counts.get("not_in_force", 0),
                "total_including_exempt_and_not_in_force": total,
            }
        ),
    }


def _write_governance_snapshot(
    data_dir: Path,
    run_id: str,
    *,
    compliance_rows: list[dict] | None = None,
    fact_rows: list[dict] | None = None,
    metric_rows: list[dict] | None = None,
) -> None:
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    if compliance_rows is not None:
        table = validate(
            "commit_compliance",
            pa.Table.from_pylist(compliance_rows, schema=get_schema("commit_compliance")),
        )
        pq.write_table(table, snapshot_dir / "governance_commit_compliance.parquet")
    if fact_rows is not None:
        table = validate(
            "commit_fact", pa.Table.from_pylist(fact_rows, schema=get_schema("commit_fact"))
        )
        pq.write_table(table, snapshot_dir / "governance_commit_fact.parquet")
    if metric_rows is not None:
        table = _metric_value_table(metric_rows)
        pq.write_table(table, snapshot_dir / "governance_metric_value.parquet")


def _default_governance_compliance_rows() -> list[dict]:
    """Two commits covering fail/pass/unknown/exempt/n-a (D15's every
    result state, plus a check simply absent from one commit)."""
    return [
        _commit_compliance_row(
            check_id="reviewer-present",
            result="fail",
        ),
        _commit_compliance_row(
            check_id="jira-ticket-referenced",
            result="pass",
            evidence="issue key(s) found: CASSANDRA-90001",
        ),
        _commit_compliance_row(
            check_id="pre-commit-ci-evidence",
            result="unknown",
            evidence="no JIRA-comment CI evidence found on CASSANDRA-90001",
        ),
        _commit_compliance_row(
            check_id="code-style-checkstyle",
            result="pass",
            evidence="check-run(s) ant-check-jdk11 all succeeded",
            evidence_url="https://github.com/apache/cassandra/runs/1",
        ),
        _commit_compliance_row(
            sha="2222222222222222222222222222222222bbbb",
            commit_date=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            author="Ninja Author",
            committer="Ninja Author",
            reviewers=[],
            jira_keys=[],
            check_id="reviewer-present",
            result="exempt",
            evidence="matched exemption: ninja",
        ),
        _commit_compliance_row(
            sha="2222222222222222222222222222222222bbbb",
            commit_date=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            author="Ninja Author",
            committer="Ninja Author",
            reviewers=[],
            jira_keys=[],
            check_id="jira-ticket-referenced",
            result="exempt",
            evidence="matched exemption: ninja",
        ),
    ]


def _default_governance_metric_rows() -> list[dict]:
    counts = {"pass": 5, "fail": 1, "unknown": 2, "exempt": 1, "not_in_force": 0}
    return [
        _governance_metric_row(check_id, date(2026, 7, 1), date(2026, 7, 31), counts)
        for check_id in (
            "reviewer-present",
            "jira-ticket-referenced",
            "pre-commit-ci-evidence",
            "code-style-checkstyle",
        )
    ]


def _build_site_with_governance(
    tmp_path: Path,
    *,
    compliance_rows: list[dict] | None = None,
    fact_rows: list[dict] | None = None,
    metric_rows: list[dict] | None = None,
    **manifest_kwargs,
) -> Path:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_governance_snapshot(
        data_dir,
        RUN_ID,
        compliance_rows=(
            _default_governance_compliance_rows() if compliance_rows is None else compliance_rows
        ),
        fact_rows=[_commit_fact_row()] if fact_rows is None else fact_rows,
        metric_rows=(_default_governance_metric_rows() if metric_rows is None else metric_rows),
    )
    manifest_kwargs.setdefault("completed_at", BUILD_TIME - timedelta(hours=1))
    _write_manifest(data_dir, RUN_ID, **manifest_kwargs)
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    return out_dir, data_dir


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


def _visible_text(html_text: str) -> str:
    """Rendered page text with tags stripped and entities decoded -- for
    asserting on a headline's visible wording (issue #69) without also
    matching against the `<span>` markup used to color-code its
    pass/unknown/fail parts."""
    return html_module.unescape(re.sub(r"<[^>]+>", "", html_text))


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

    # Conversations now always has two registered metrics (issue #35), and
    # Governance's GOVERNANCE_METRICS (issue #36) are wired into series
    # building (issue #37) -- both summary cards render real headline
    # metrics (Governance's honestly flagged "insufficient data" since this
    # run has no governance snapshot) instead of falling back to the
    # page-level empty message, which would incorrectly imply nothing is
    # registered there at all.
    assert PAGES["conversations"].empty_message not in html_text
    assert PAGES["governance"].empty_message not in html_text
    assert "insufficient data" in html_text


def test_home_summary_card_shows_conversations_headline_metrics(tmp_path):
    """Issue #35: the home page's Conversations summary card shows the two
    dev@ metrics' headline values now that they're registered, the same way
    it already does for Community."""
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "")

    assert "Time to First Reply" in html_text
    assert "Unanswered Thread Rate" in html_text


def test_conversations_page_shows_devlist_metric_cards(tmp_path):
    """Issue #35: the Conversations page renders the two dev@ metrics as
    cards (replacing the old empty state) once they're registered."""
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "conversations/")

    assert "Time to First Reply — dev@" in html_text
    assert "Unanswered Thread Rate — dev@" in html_text
    assert "Aug 2026" in html_text


def test_conversations_page_explains_whats_coming(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "conversations/")

    assert "Phase 2a" in html_text
    assert "Phase 2b" in html_text
    assert "interaction health, not raw sentiment" in html_text.replace("\n", " ")
    assert "COMMUNITY-HEALTH.md" in html_text


def test_conversations_page_is_honest_when_metrics_have_no_data_yet(tmp_path):
    """A snapshot with rows for every *other* M0 metric but none for the two
    dev@ metrics (e.g. a run before Pony Mail has collected anything) still
    renders their cards -- as "insufficient data", the same convention
    `/community/` already uses for a metric with no data yet -- rather than
    a card silently going missing."""
    rows = [
        row
        for row in _default_rows()
        if row["metric_id"] not in ("time_to_first_reply_devlist", "unanswered_thread_rate_devlist")
    ]
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = _page_html(out_dir, "conversations/")

    assert "Time to First Reply — dev@" in html_text
    assert "Unanswered Thread Rate — dev@" in html_text
    assert html_text.count("insufficient data") >= 2


def test_governance_page_is_a_placeholder_linking_to_decisions(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert "not published yet" in html_text
    assert "DECISIONS.md" in html_text
    assert "D14" in html_text
    assert "D15" in html_text


# --- Governance per-commit compliance (issue #37, D14/D15) ------------------


def test_governance_page_shows_policy_version_and_approval_linked(tmp_path):
    out_dir, _ = _build_site_with_governance(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert "Policy v1" in html_text
    assert "pmcfadin" in html_text
    assert "2026-09-25" in html_text
    policy_href = (
        'href="https://github.com/pmcfadin/cassandra-project-health/'
        'blob/main/governance-policy.yaml"'
    )
    assert policy_href in html_text
    assert "Unknown is not a fail" in html_text


def test_governance_page_lists_current_fails_with_evidence_and_correction_link(tmp_path):
    out_dir, _ = _build_site_with_governance(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert "Currently failing (1)" in html_text
    assert "no reviewer found in commit trailer" in html_text
    assert "1111111111" in html_text  # short sha of the failing commit
    commit_url = "https://github.com/apache/cassandra/commit/1111111111111111111111111111111111aaaa"
    assert commit_url in html_text
    assert "Request a correction" in html_text
    assert "issues/new?" in html_text
    assert "governance-correction.yml" in html_text
    assert "sha=1111111111111111111111111111111111aaaa" in html_text
    assert "check_id=reviewer-present" in html_text


def test_governance_page_never_labels_unknown_as_fail(tmp_path):
    out_dir, _ = _build_site_with_governance(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    # The one genuine fail (reviewer-present on commit 1) is the only row in
    # the fails view -- the pre-commit-ci-evidence `unknown` result on that
    # same commit must never appear there.
    assert "Currently failing (1)" in html_text
    fails_start = html_text.index('id="fails-heading"')
    fails_end = html_text.index("</section>", fails_start)
    fails_html = html_text[fails_start:fails_end]
    assert "pre-commit-ci-evidence" not in fails_html
    assert "no JIRA-comment CI evidence found" not in fails_html


def test_governance_page_backfill_partial_status_shown_honestly(tmp_path):
    out_dir, _ = _build_site_with_governance(
        tmp_path,
        governance={
            "status": "partial",
            "commits_scored": 2,
            "compliance_rows": 6,
            "policy_version": 1,
            "ci_evidence": {"checked": 3, "pending": 7, "calls_made": 3},
            "check_runs": {"checked": 1, "pending": 4, "calls_made": 1},
        },
    )
    html_text = _page_html(out_dir, "governance/")

    assert "partial" in html_text
    assert "7 CI-evidence lookup(s)" in html_text
    assert "4 checkstyle-check lookup(s)" in html_text
    assert "picked up automatically on a later run" in html_text


def test_governance_page_backfill_ok_status_shown(tmp_path):
    out_dir, _ = _build_site_with_governance(
        tmp_path,
        governance={
            "status": "ok",
            "commits_scored": 2,
            "compliance_rows": 6,
            "policy_version": 1,
            "ci_evidence": {"checked": 1, "pending": 0, "calls_made": 1},
            "check_runs": {"checked": 1, "pending": 0, "calls_made": 1},
        },
    )
    html_text = _page_html(out_dir, "governance/")

    assert "Backfill complete for this run" in html_text


def test_governance_page_writes_commit_json_and_csv_downloads(tmp_path):
    out_dir, _ = _build_site_with_governance(tmp_path)

    recent_path = out_dir / "data" / "governance-commits-recent.json"
    full_json_path = out_dir / "data" / "governance-commits-full.json"
    full_csv_path = out_dir / "data" / "governance-commits-full.csv"
    assert recent_path.is_file()
    assert full_json_path.is_file()
    assert full_csv_path.is_file()

    payload = json.loads(full_json_path.read_text())
    assert payload["policy_version"] == 1
    assert payload["row_count"] == 2
    by_sha = {row["sha"]: row for row in payload["rows"]}
    failing = by_sha["1111111111111111111111111111111111aaaa"]
    assert failing["checks"]["reviewer-present"]["result"] == "fail"
    assert failing["jira_urls"] == ["https://issues.apache.org/jira/browse/CASSANDRA-90001"]
    assert failing["commit_url"] == (
        "https://github.com/apache/cassandra/commit/1111111111111111111111111111111111aaaa"
    )
    assert failing["correction_url"].startswith(
        "https://github.com/pmcfadin/cassandra-project-health/issues/new?"
    )
    # commit 2 never got a pre-commit-ci-evidence/code-style-checkstyle row
    # in the fixture -- absence must stay absent (n/a), never turn into a
    # fabricated "unknown".
    ninja_commit = by_sha["2222222222222222222222222222222222bbbb"]
    assert "pre-commit-ci-evidence" not in ninja_commit["checks"]

    csv_text = full_csv_path.read_text()
    assert "reviewer-present_result" in csv_text.splitlines()[0]
    assert "1111111111111111111111111111111111aaaa" in csv_text


def test_governance_page_filters_present_with_month_branch_check_result_options(tmp_path):
    out_dir, _ = _build_site_with_governance(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert 'data-gov-filter="month"' in html_text
    assert 'data-gov-filter="branch"' in html_text
    assert 'data-gov-filter="check"' in html_text
    assert 'data-gov-filter="result"' in html_text
    assert '<option value="2026-08">2026-08</option>' in html_text
    assert '<option value="trunk">trunk</option>' in html_text
    assert '<option value="reviewer-present">reviewer-present</option>' in html_text
    assert '<option value="fail">fail</option>' in html_text
    assert "governance.js" in html_text
    assert "load full history" in html_text


def test_governance_page_shows_compliance_trend_charts_and_ninja_trend(tmp_path):
    out_dir, _ = _build_site_with_governance(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert "Compliance trends" in html_text
    assert "Ninja exemptions" in html_text
    assert "descriptive only" in html_text
    spec = _extract_vega_spec(html_text, "Compliance trend for reviewer-present")
    states = {v["state"] for v in spec["data"]["values"]}
    assert states == {"pass", "fail", "unknown", "exempt"}


def test_governance_page_headline_shows_pass_unknown_fail_shares(tmp_path):
    """Issue #69: every governance headline card shows the pass/unknown/fail
    shares together (not just the bare pass rate), computed over the exact
    same scored (pass+fail+unknown) denominator as the pass rate itself --
    the fixture's counts (pass=5, fail=1, unknown=2, scored=8) give
    62.5%/25.0%/12.5%."""
    out_dir, _ = _build_site_with_governance(tmp_path)
    text = _visible_text(_page_html(out_dir, "governance/"))

    combined = "62.5% pass · 25.0% unknown · 12.5% fail"
    assert text.count(combined) == 4  # one per scored check card
    assert "62.5%" in text  # the bare pass-rate value is unchanged


def test_governance_page_headline_tags_backfill_pending_when_partial_and_unknown_high(tmp_path):
    """A 'backfill pending' tag appears when the manifest's governance
    status is `partial` *and* the check's unknown share is >= 25% (issue
    #69) -- the fixture's unknown share is exactly 25%, the threshold."""
    out_dir, _ = _build_site_with_governance(
        tmp_path,
        governance={
            "status": "partial",
            "commits_scored": 2,
            "compliance_rows": 6,
            "policy_version": 1,
            "ci_evidence": {"checked": 3, "pending": 7, "calls_made": 3},
            "check_runs": {"checked": 1, "pending": 4, "calls_made": 1},
        },
    )
    html_text = _page_html(out_dir, "governance/")

    assert html_text.count("backfill pending") == 4  # one per scored check card


def test_governance_page_headline_omits_backfill_pending_tag_when_status_ok(tmp_path):
    """The same high (25%) unknown share never earns the tag when this
    run's backfill status is `ok`, not `partial` (issue #69)."""
    out_dir, _ = _build_site_with_governance(
        tmp_path,
        governance={
            "status": "ok",
            "commits_scored": 2,
            "compliance_rows": 6,
            "policy_version": 1,
            "ci_evidence": {"checked": 1, "pending": 0, "calls_made": 1},
            "check_runs": {"checked": 1, "pending": 0, "calls_made": 1},
        },
    )
    html_text = _page_html(out_dir, "governance/")

    assert "backfill pending" not in html_text


def test_governance_page_headline_omits_backfill_pending_tag_below_unknown_threshold(tmp_path):
    """A `partial` backfill status alone doesn't earn the tag -- the
    check's own unknown share must be at least 25% (issue #69); here it's
    10% (1 of 10 scored)."""
    counts = {"pass": 9, "fail": 0, "unknown": 1, "exempt": 0, "not_in_force": 0}
    metric_rows = [
        _governance_metric_row(check_id, date(2026, 7, 1), date(2026, 7, 31), counts)
        for check_id in (
            "reviewer-present",
            "jira-ticket-referenced",
            "pre-commit-ci-evidence",
            "code-style-checkstyle",
        )
    ]
    out_dir, _ = _build_site_with_governance(
        tmp_path,
        metric_rows=metric_rows,
        governance={
            "status": "partial",
            "commits_scored": 2,
            "compliance_rows": 6,
            "policy_version": 1,
            "ci_evidence": {"checked": 3, "pending": 7, "calls_made": 3},
            "check_runs": {"checked": 1, "pending": 4, "calls_made": 1},
        },
    )
    text = _visible_text(_page_html(out_dir, "governance/"))

    assert "backfill pending" not in text
    assert text.count("90.0% pass · 10.0% unknown · 0.0% fail") == 4


def test_governance_page_has_no_data_state_when_engine_never_ran(tmp_path):
    """A run without the governance engine (older run, or source not
    configured) still renders honestly -- no crash, no fabricated rows."""
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert "not published yet" in html_text
    assert "Currently failing" not in html_text


def test_manifest_parses_governance_status_and_pending_counts(tmp_path):
    from project_health.site.manifest import load_manifest

    data_dir = tmp_path / "data"
    _write_manifest(
        data_dir,
        RUN_ID,
        completed_at=BUILD_TIME,
        governance={
            "status": "partial",
            "commits_scored": 10,
            "compliance_rows": 30,
            "policy_version": 1,
            "ci_evidence": {"checked": 2, "pending": 5, "calls_made": 2},
            "check_runs": {"checked": 1, "pending": 3, "calls_made": 1},
        },
    )
    manifest = load_manifest(data_dir, RUN_ID)

    assert manifest.governance is not None
    assert manifest.governance.status == "partial"
    assert manifest.governance.ci_evidence.pending == 5
    assert manifest.governance.check_runs.pending == 3


def test_manifest_governance_is_none_when_absent(tmp_path):
    from project_health.site.manifest import load_manifest

    data_dir = tmp_path / "data"
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME)
    manifest = load_manifest(data_dir, RUN_ID)

    assert manifest.governance is None


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
        if meta.page != "community":
            continue
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


# --- Per-card staleness badge (issue #86, ARCHITECTURE.md §7.3) ------------
#
# Distinct from the header pill above: every card/metric that *depends* on
# a failed/stale source shows its own badge naming that source, its last
# good refresh date, and when collection failed -- not just a once-per-page
# header pill a reader has to cross-reference against `MetricMeta.sources`
# themselves.


def _failed_jira_sources() -> dict:
    return {
        "git": {"status": "ok", "watermark": "sha:aaa", "records_collected": 10},
        "jira": {
            "status": "failed",
            "reason": "Unterminated string starting at: line 1 column 219263",
            "last_good_snapshot": "2026-09-24T074929Z-371daf4",
        },
    }


def test_community_card_for_a_jira_sourced_metric_shows_staleness_badge(tmp_path):
    out_dir = _build_site(
        tmp_path, completed_at=BUILD_TIME - timedelta(hours=1), sources=_failed_jira_sources()
    )
    html_text = _page_html(out_dir, "community/")
    # median_resolution_latency_jira declares sources=("jira",) in metrics_meta.py.
    assert "JIRA data last refreshed 2026-09-24" in html_text
    assert "collection failed on" in html_text


def test_community_card_for_a_git_only_metric_omits_jira_staleness_badge(tmp_path):
    out_dir = _build_site(
        tmp_path, completed_at=BUILD_TIME - timedelta(hours=1), sources=_failed_jira_sources()
    )
    html_text = _page_html(out_dir, "community/")
    # active_contributors_monthly declares sources=("git",) only -- git is
    # 'ok' in this manifest, so this metric's own card must not show a
    # staleness badge even though the page as a whole has one failed source.
    dom = html_text
    card_start = dom.index("Active Contributors")
    card_html = dom[card_start : card_start + 800]
    assert "JIRA data last refreshed" not in card_html


def test_unique_reviewers_card_has_no_badge_when_only_github_failed(tmp_path):
    """review_event only ever gets 'commit_trailer' (git) or 'jira_field'
    (jira) rows -- never a github one -- so unique_reviewers_monthly's card
    must show no staleness badge at all when only 'github' has failed. This
    is the exact bug the orchestrator's review caught (a GitHub badge
    wrongly shown on this card)."""
    out_dir = _build_site(
        tmp_path,
        completed_at=BUILD_TIME - timedelta(hours=1),
        sources={
            "git": {"status": "ok", "watermark": "sha:aaa", "records_collected": 10},
            "jira": {"status": "ok", "watermark": "2026-09-25T00:00:00Z", "records_collected": 3},
            "github": {
                "status": "failed",
                "reason": "Unterminated string starting at: line 1 column 219263",
                "last_good_snapshot": "2026-09-24T074929Z-371daf4",
            },
        },
    )
    html_text = _page_html(out_dir, "community/")
    card_start = html_text.index("Unique Reviewers")
    card_html = html_text[card_start : card_start + 800]
    assert "card-staleness" not in card_html
    assert "GitHub data last refreshed" not in card_html


def test_home_summary_card_shows_staleness_badge_when_a_page_source_failed(tmp_path):
    out_dir = _build_site(
        tmp_path, completed_at=BUILD_TIME - timedelta(hours=1), sources=_failed_jira_sources()
    )
    html_text = (out_dir / "index.html").read_text()
    assert "JIRA data last refreshed 2026-09-24" in html_text


def test_staleness_badge_not_shown_for_partial_status(tmp_path):
    """'partial' is budgeted backfill-in-progress, which already has its own
    'backfill pending' badge -- issue #86 acceptance criterion: never a
    staleness badge for it."""
    out_dir = _build_site(
        tmp_path,
        completed_at=BUILD_TIME - timedelta(hours=1),
        sources={
            "git": {"status": "ok", "watermark": "sha:aaa", "records_collected": 10},
            "jira": {
                "status": "partial",
                "watermark": "2026-09-25T00:00:00Z",
                "records_collected": 3,
            },
        },
    )
    html_text = _page_html(out_dir, "community/")
    assert "JIRA data last refreshed" not in html_text
    assert "collection failed on" not in html_text


def test_governance_cards_show_staleness_badge_when_git_failed(tmp_path):
    """Every governance check scores governance's own git commit walk,
    which reuses the same local clone the 'git' source's clone_or_fetch
    step maintains (metrics_meta.py's GOVERNANCE_METRICS comment) -- so a
    'git' failure must badge every governance card, including
    code-style-checkstyle even though its own evidence collector
    (GitHubChecksCollector) has no manifest.sources entry of its own."""
    out_dir, _ = _build_site_with_governance(
        tmp_path,
        sources={
            "git": {
                "status": "failed",
                "reason": "git fetch failed",
                "last_good_snapshot": "2026-09-24T074929Z-371daf4",
            },
            "jira": {"status": "ok", "watermark": "2026-09-25T00:00:00Z", "records_collected": 3},
        },
    )
    html_text = _page_html(out_dir, "governance/")
    assert "Git data last refreshed 2026-09-24" in html_text


def test_governance_checkstyle_card_omits_github_staleness_badge(tmp_path):
    """A 'github' failure must NOT badge code-style-checkstyle: that check's
    own evidence collector (GitHubChecksCollector) is distinct from the PR
    collector 'github' status tracks, and has no manifest.sources entry of
    its own (metrics_meta.py's GOVERNANCE_METRICS comment) -- a wrong
    mapping here was exactly the orchestrator-review bug this fixup fixes."""
    out_dir, _ = _build_site_with_governance(
        tmp_path,
        sources={
            "git": {"status": "ok", "watermark": "sha:aaa", "records_collected": 10},
            "jira": {"status": "ok", "watermark": "2026-09-25T00:00:00Z", "records_collected": 3},
            "github": {
                "status": "failed",
                "reason": "Unterminated string starting at: line 1 column 219263",
                "last_good_snapshot": "2026-09-24T074929Z-371daf4",
            },
        },
    )
    html_text = _page_html(out_dir, "governance/")
    assert "GitHub data last refreshed" not in html_text


def test_leaderboard_section_shows_staleness_badge_when_git_failed(tmp_path):
    out_dir = _build_site(
        tmp_path,
        completed_at=BUILD_TIME - timedelta(hours=1),
        sources={
            "git": {
                "status": "failed",
                "reason": "git fetch failed",
                "last_good_snapshot": "2026-09-24T074929Z-371daf4",
            },
            "jira": {"status": "ok", "watermark": "2026-09-25T00:00:00Z", "records_collected": 3},
        },
    )
    html_text = _page_html(out_dir, "community/")
    leaderboard_start = html_text.index('id="leaderboard-heading"')
    leaderboard_html = html_text[leaderboard_start:]
    assert "Git data last refreshed 2026-09-24" in leaderboard_html


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


def test_all_m0_metrics_declare_a_known_page():
    """D13/issue #34: which page a metric renders on is declared once, in
    `MetricMeta.page`. The original six M0 metrics (plus issue #53's three)
    are community (code/contributor) metrics; issue #35 added the first two
    conversations (dev@ mailing-list) metrics."""
    expected_pages = {
        "time_to_first_reply_devlist": "conversations",
        "unanswered_thread_rate_devlist": "conversations",
    }
    for metric_id, meta in M0_METRICS.items():
        assert meta.page == expected_pages.get(metric_id, "community")
        assert meta.page in PAGES


def test_every_registered_metric_declares_a_nonempty_source_mapping():
    """issue #86 fixup (orchestrator review): every metric_id registered in
    `metrics.registry.METRIC_IDS` -- the actual set the pipeline computes
    and writes `metric_value` rows for -- must have a `MetricMeta` entry
    here with a non-empty `sources` tuple, so its card can always render a
    staleness badge when one of its real dependencies fails. A registered
    metric silently missing from `M0_METRICS` entirely (as
    `pmc_joins_quarterly` was before this fixup) gets no card on the site at
    all -- this test catches that class of bug too, not just an empty
    `sources` tuple on an already-present entry."""
    from project_health.metrics.registry import METRIC_IDS

    missing = set(METRIC_IDS) - set(M0_METRICS)
    assert not missing, f"registered metric(s) with no MetricMeta at all: {missing}"
    for metric_id in METRIC_IDS:
        assert M0_METRICS[metric_id].sources, f"{metric_id} declares no sources"


def test_every_governance_check_declares_a_nonempty_source_mapping():
    from project_health.governance.registry import _CHECK_IDS
    from project_health.governance.metrics import metric_id_for_check

    expected_metric_ids = {metric_id_for_check(check_id) for check_id in _CHECK_IDS}
    assert expected_metric_ids == set(GOVERNANCE_METRICS)
    for metric_id, meta in GOVERNANCE_METRICS.items():
        assert meta.sources, f"{metric_id} declares no sources"


def test_metric_source_mappings_match_the_actual_engine_queries_and_collectors():
    """issue #86 fixup (orchestrator review): these mappings must be derived
    from `metrics/engine.py`'s SQL and the collectors that actually populate
    each table, never from a metric's name or prose description alone --
    the original mapping wrongly credited `unique_reviewers_monthly`/
    `reviewer_hhi` with a 'github' dependency `review_event` never has, and
    missed the `github_commit_authors`/`github_profile` dependency every
    organizational-diversity metric's `affiliation_period` join actually
    has. See metrics_meta.py's own inline comments for the full per-metric
    citation into engine.py/pipeline.py/collectors/*.py this test guards."""
    # review_event only ever gets 'commit_trailer' (git) or 'jira_field'
    # (jira) rows (collectors/git.py, collectors/jira.py) -- never a GitHub
    # row -- so neither reviewer metric below has a 'github' dependency.
    assert M0_METRICS["unique_reviewers_monthly"].sources == ("git", "jira")
    # reviewer_hhi's displayed value/chart is commit_trailer-only
    # (metrics/engine.py::_reviewer_hhi's own docstring) -- jira_field_hhi
    # is a details_json-only cross-check, never the rendered number.
    assert M0_METRICS["reviewer_hhi"].sources == ("git",)

    # Every organizational-diversity metric joins affiliation_period, whose
    # github_company priority level reads github_profile (keyed by logins
    # github_commit_authors links), on top of the git commits being resolved.
    for metric_id in (
        "elephant_factor",
        "organizational_hhi",
        "single_org_share",
        "unknown_affiliation_rate",
    ):
        assert M0_METRICS[metric_id].sources == ("git", "github_commit_authors", "github_profile")

    # pmc_joins_quarterly reads roster_entry only -- no git/jira at all.
    assert M0_METRICS["pmc_joins_quarterly"].sources == ("asf_roster",)

    # Governance: every check scores governance's own git commit walk (git),
    # but only reviewer-present also reads the real jira source's
    # review_event table -- jira-ticket-referenced is pure commit-message
    # regex, and the CI-evidence/checkstyle evidence collectors
    # (JiraCommentsCollector/GitHubChecksCollector) have no manifest.sources
    # entry of their own to attribute a badge to.
    assert GOVERNANCE_METRICS["governance_reviewer_present_pass_rate"].sources == ("git", "jira")
    assert GOVERNANCE_METRICS["governance_jira_ticket_referenced_pass_rate"].sources == ("git",)
    assert GOVERNANCE_METRICS["governance_pre_commit_ci_evidence_pass_rate"].sources == ("git",)
    assert GOVERNANCE_METRICS["governance_code_style_checkstyle_pass_rate"].sources == ("git",)

    # Leaderboard: commits/reviews are git-only (reviews credit
    # commit_trailer only, same as reviewer_hhi), jira_issues_resolved is
    # jira, and every list's organization column is the same
    # affiliation_period-derived github_commit_authors/github_profile
    # dependency the org metrics have -- never 'github' (PRs): leaderboard.py
    # never reads pr/pr_review at all.
    from project_health.site.metrics_meta import LEADERBOARD_SOURCES, SECURITY_SOURCES

    assert LEADERBOARD_SOURCES == ("git", "jira", "github_commit_authors", "github_profile")
    assert SECURITY_SOURCES == ("security",)


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


def test_community_card_shows_backfill_in_progress_note(tmp_path):
    """Issue #79: a card whose most recent window's `details_json.
    backfill_in_progress` is true shows the same badge--backfill-pending
    note governance.html already uses (issue #69)."""
    rows = [
        _metric_value_row(
            "time_to_first_response_jira",
            date(2026, 8, 1),
            date(2026, 8, 31),
            None,
            0,
            "insufficient_data",
            details_json=json.dumps({"backfill_in_progress": True}),
        ),
    ]
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = _community_html(out_dir)

    idx = html_text.index("Time to First Response (JIRA)")
    card_html = html_text[idx : idx + 800]
    assert 'class="badge badge--backfill-pending"' in card_html
    assert "backfill pending" in card_html


def test_community_card_omits_backfill_note_when_not_flagged(tmp_path):
    rows = [
        _metric_value_row(
            "time_to_first_response_jira",
            date(2026, 8, 1),
            date(2026, 8, 31),
            3.5,
            10,
            "ok",
        ),
    ]
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = _community_html(out_dir)

    idx = html_text.index("Time to First Response (JIRA)")
    card_html = html_text[idx : idx + 800]
    assert "badge--backfill-pending" not in card_html


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
    # (Issue #28: the chart is now a two-layer spec -- a full-opacity line
    # layer plus a point layer whose opacity de-emphasises low-n/
    # insufficient_data points -- so `clip` lives on each layer's own mark.)
    assert len(spec["layer"]) == 2
    for layer in spec["layer"]:
        assert layer["mark"]["clip"] is True


# --- Chart window default + low-n de-emphasis (issue #28) ------------------


def _monthly_rows(
    metric_id: str,
    count: int,
    *,
    start: date = date(2010, 1, 1),
    n: int = 20,
    flag: str = "ok",
    value: float | None = 5.0,
) -> list[dict]:
    """`count` consecutive monthly `metric_value` rows for `metric_id`,
    starting at `start`'s month -- used to build a series long enough to
    exercise the default 36-month chart window against its full history."""
    from project_health.metrics.windows import add_months, month_end

    rows = []
    for i in range(count):
        window_start = add_months(start, i)
        rows.append(
            _metric_value_row(metric_id, window_start, month_end(window_start), value, n, flag)
        )
    return rows


def test_long_series_defaults_to_recent_window_with_full_history_in_usermeta(tmp_path):
    """Issue #28: 17 years of monthly points renders densely by default --
    a chart now opens on just its last 36 months, with the complete
    (padded) domain still embedded in the spec's own `usermeta` for
    `static/app.js`'s "Full history" toggle to swap in client-side, with no
    second network request."""
    metric_id = "median_resolution_latency_jira"
    rows = _monthly_rows(metric_id, 48, start=date(2022, 1, 1), n=20, flag="ok", value=14.0)
    for other_id in M0_METRICS:
        if other_id == metric_id:
            continue
        rows.append(
            _metric_value_row(other_id, date(2025, 12, 1), date(2025, 12, 31), 1.0, 20, "ok")
        )

    out_dir = _build_site(tmp_path, rows=rows)
    html_text = _community_html(out_dir)
    spec = _extract_vega_spec(html_text, "History chart for Median JIRA Resolution Latency")

    window = spec["usermeta"]["chartWindow"]
    recent_domain = window["domain"]["recent"]
    full_domain = window["domain"]["full"]

    # The chart's actual rendered domain is the recent one, by default.
    assert spec["encoding"]["x"]["scale"]["domain"] == recent_domain
    # ... but it's materially narrower than the full 48-month history, which
    # is still there (untruncated) for the toggle.
    assert recent_domain[0] > full_domain[0]
    assert recent_domain[1] == full_domain[1]  # both end at the same latest month


def test_short_series_recent_window_equals_full_history(tmp_path):
    """A series shorter than the default 36-month window is never
    artificially truncated -- the toggle exists but has nothing to add."""
    out_dir = _build_site(tmp_path)  # _default_rows(): 3 months of history
    html_text = _community_html(out_dir)
    spec = _extract_vega_spec(html_text, "History chart for Reviewer Concentration (HHI)")

    window = spec["usermeta"]["chartWindow"]
    assert window["domain"]["recent"] == window["domain"]["full"]
    assert spec["encoding"]["x"]["scale"]["domain"] == window["domain"]["full"]


def test_chart_tooltip_includes_n_and_flag(tmp_path):
    """Issue #28: every chart's tooltip shows the underlying sample size
    and flag, not just the formatted value, so a reader can tell a
    low-sample-size or insufficient-data point apart from a well-supported
    one without leaving the page."""
    out_dir = _build_site(tmp_path)
    html_text = _community_html(out_dir)
    spec = _extract_vega_spec(html_text, "History chart for Reviewer Concentration (HHI)")

    tooltip_by_field = {t["field"]: t for t in spec["encoding"]["tooltip"]}
    assert tooltip_by_field["n"]["title"] == "n"
    assert tooltip_by_field["n"]["type"] == "quantitative"
    assert tooltip_by_field["flag"]["title"] == "Flag"
    assert tooltip_by_field["flag"]["type"] == "nominal"


def test_low_n_and_insufficient_data_points_are_flagged_for_de_emphasis(tmp_path):
    """Issue #28: an 'ok' point below the display floor, and an
    insufficient_data point, both get `low_n: true` in the chart's plotted
    values -- a presentation-only hint (never a change to n/value/flag
    themselves) that the point layer's opacity is conditioned on."""
    metric_id = "median_resolution_latency_jira"
    rows = [
        _metric_value_row(metric_id, date(2026, 6, 1), date(2026, 6, 30), 12.0, 20, "ok"),
        _metric_value_row(metric_id, date(2026, 7, 1), date(2026, 7, 31), 1200.0, 6, "ok"),
        _metric_value_row(
            metric_id, date(2026, 8, 1), date(2026, 8, 20), None, 1, "insufficient_data"
        ),
    ]
    for other_id in M0_METRICS:
        if other_id == metric_id:
            continue
        rows.append(_metric_value_row(other_id, date(2026, 8, 1), date(2026, 8, 20), 1.0, 20, "ok"))

    out_dir = _build_site(tmp_path, rows=rows)
    html_text = _community_html(out_dir)
    spec = _extract_vega_spec(html_text, "History chart for Median JIRA Resolution Latency")

    values_by_n = {v["n"]: v for v in spec["data"]["values"]}
    assert values_by_n[20]["low_n"] is False
    # A low-n (but still `ok`) point: never changes value/flag.
    assert values_by_n[6]["low_n"] is True
    assert values_by_n[6]["value"] == 1200.0
    assert values_by_n[6]["flag"] == "ok"
    # An insufficient_data point: always de-emphasised, value stays null.
    assert values_by_n[1]["low_n"] is True
    assert values_by_n[1]["value"] is None
    assert values_by_n[1]["flag"] == "insufficient_data"

    point_layer = spec["layer"][1]
    assert point_layer["mark"]["type"] == "point"
    assert point_layer["encoding"]["opacity"]["condition"]["test"] == "datum.low_n"
    # The line layer itself never fades -- only the point layer is
    # conditioned on `low_n`, so the trend stays fully legible.
    line_layer = spec["layer"][0]
    assert "opacity" not in line_layer.get("encoding", {})


def test_low_n_de_emphasis_never_applies_to_a_plain_count_metric(tmp_path):
    """METRICS.md §0.6 (owner decision, issue #27): a plain headcount is
    the complete, meaningful number at any `n` -- "a month with 3 new
    contributors is real signal", never an unstable estimate the way a
    rate/HHI/median with the same small `n` would be. A count metric's
    points must never be flagged `low_n` just because the count itself is
    small (verified against the real site: `new_contributors_monthly`
    regularly reports single-digit months)."""
    metric_id = "new_contributors_monthly"
    rows = [
        _metric_value_row(metric_id, date(2026, 6, 1), date(2026, 6, 30), 3.0, 3, "ok"),
        _metric_value_row(metric_id, date(2026, 7, 1), date(2026, 7, 31), 0.0, 0, "ok"),
    ]
    for other_id in M0_METRICS:
        if other_id == metric_id:
            continue
        rows.append(
            _metric_value_row(other_id, date(2026, 7, 1), date(2026, 7, 31), 1.0, 20, "ok")
        )

    out_dir = _build_site(tmp_path, rows=rows)
    html_text = _community_html(out_dir)
    spec = _extract_vega_spec(html_text, "History chart for New Contributors")

    for point in spec["data"]["values"]:
        assert point["low_n"] is False


def test_old_outlier_month_does_not_flatten_the_default_view_y_axis(tmp_path):
    """The actual bug issue #28 reports: an old, low-n outlier month (a
    median latency spike to ~1,200 days from a couple of closed issues)
    must not set the default view's y-scale once it's scrolled out of the
    visible (recent) x-window -- restricting the x-domain alone doesn't do
    this on its own; the y-domain must be recomputed from only the
    in-window points too (`chart_spec.recent_value_domain`)."""
    metric_id = "median_resolution_latency_jira"
    rows = _monthly_rows(metric_id, 40, start=date(2022, 1, 1), n=20, flag="ok", value=14.0)
    # An old outlier, well outside the last 36 months, that would otherwise
    # dominate a shared y-scale.
    rows[0] = _metric_value_row(metric_id, date(2022, 1, 1), date(2022, 1, 31), 1200.0, 6, "ok")
    for other_id in M0_METRICS:
        if other_id == metric_id:
            continue
        rows.append(_metric_value_row(other_id, date(2025, 4, 1), date(2025, 4, 30), 1.0, 20, "ok"))

    out_dir = _build_site(tmp_path, rows=rows)
    html_text = _community_html(out_dir)
    spec = _extract_vega_spec(html_text, "History chart for Median JIRA Resolution Latency")

    y_domain = spec["encoding"]["y"]["scale"]["domain"]
    assert y_domain[0] == 0
    assert y_domain[1] < 1200.0  # the old outlier never sets the default view's scale

    # The full-history y-domain override is explicitly `None` -- toggling
    # to "Full history" (`static/app.js`) removes the override entirely,
    # falling back to Vega-Lite's own auto-scale over the whole series, so
    # the outlier is honestly visible there.
    y_window = spec["usermeta"]["chartWindow"]["yDomain"]
    assert y_window["recent"] == y_domain
    assert y_window["full"] is None


def test_community_page_has_chart_window_toggle_and_low_n_legend_note(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _community_html(out_dir)

    assert "data-chart-window-toggle" in html_text
    assert "Full history" in html_text
    assert "low-n" in html_text
    assert "insufficient data" in html_text.lower()


def test_conversations_page_has_chart_window_toggle(tmp_path):
    out_dir = _build_site(tmp_path)
    html_text = _page_html(out_dir, "conversations/")
    assert "data-chart-window-toggle" in html_text


def test_governance_trend_charts_are_windowed_and_show_n_and_flag(tmp_path):
    """Issue #28 explicitly calls out governance's multi-series compliance
    trend charts: the same recent-window default, n/flag tooltip and low-n
    de-emphasis apply there too, without breaking the per-check
    pass/fail/unknown/exempt color-coded lines (issue #36/#69)."""
    out_dir, _ = _build_site_with_governance(tmp_path)
    html_text = _page_html(out_dir, "governance/")

    assert "data-chart-window-toggle" in html_text
    spec = _extract_vega_spec(html_text, "Compliance trend for reviewer-present")

    assert "usermeta" in spec
    assert len(spec["layer"]) == 2

    tooltip_by_field = {t["field"]: t for t in spec["encoding"]["tooltip"]}
    assert tooltip_by_field["n"]["title"] == "n"
    assert tooltip_by_field["flag"]["title"] == "Flag"

    values = spec["data"]["values"]
    states = {v["state"] for v in values}
    assert states == {"pass", "fail", "unknown", "exempt"}
    # The fixture's scored n (pass=5, fail=1, unknown=2 -> n=8) is below the
    # display floor, so every state's record for that month is low_n.
    assert all(v["n"] == 8 and v["low_n"] is True for v in values)

    # Multi-series color-by-result-state still works (issue #36/#69).
    assert spec["encoding"]["color"]["field"] == "state"


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


# --- Orchestrator review fixups (issue #57): composite scale note, stale-
# point "backfill pending" flag, sub-day duration formatting -------------


def _write_scoring_snapshot(
    data_dir: Path,
    run_id: str,
    *,
    composite: float | None = 56.4,
    has_declining_dimension: bool = True,
) -> None:
    """A minimal, real `composite_score.parquet` + `dimension_status.parquet`
    pair (D20, issue #57) -- just enough for `scoring_page.py` to report
    `has_data=True` so the home page's composite section renders."""
    computed_at = datetime(2026, 9, 25, 6, 30, tzinfo=UTC)
    dimensions = [
        "contributor sustainability",
        "reviewer capacity",
        "responsiveness",
        "organizational diversity",
        "release cadence",
    ]
    breakdown = [
        {
            "dimension": dim,
            "weight": 0.2,
            "renormalized_weight": 0.2,
            "score": 55.0,
            "status": (
                "declining"
                if (dim == "reviewer capacity" and has_declining_dimension)
                else "stable"
            ),
            "included": True,
            "key_metrics_scored": 1,
            "key_metrics_total": 1,
        }
        for dim in dimensions
    ]
    composite_row = {
        "window_end": date(2026, 8, 31),
        "composite": composite,
        "dimensions_included": 5,
        "dimensions_total": 5,
        "has_declining_dimension": has_declining_dimension,
        "dimensions_json": json.dumps(breakdown),
        "scoring_version": "1.0.0",
        "run_id": run_id,
        "computed_at": computed_at,
    }
    dimension_rows = [
        {
            "dimension": entry["dimension"],
            "window_end": date(2026, 8, 31),
            "status": entry["status"],
            "driven_by": "reviewer_hhi" if entry["status"] == "declining" else None,
            "score": entry["score"],
            "key_metrics_scored": entry["key_metrics_scored"],
            "key_metrics_total": entry["key_metrics_total"],
            "scoring_version": "1.0.0",
            "run_id": run_id,
            "computed_at": computed_at,
        }
        for entry in breakdown
    ]
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    composite_table = validate(
        "composite_score",
        pa.Table.from_pylist([composite_row], schema=get_schema("composite_score")),
    )
    dimension_table = validate(
        "dimension_status",
        pa.Table.from_pylist(dimension_rows, schema=get_schema("dimension_status")),
    )
    pq.write_table(composite_table, snapshot_dir / "composite_score.parquet")
    pq.write_table(dimension_table, snapshot_dir / "dimension_status.parquet")


def test_composite_scale_note_shown_on_home_page(tmp_path):
    """Orchestrator review of issue #57 fix 1: the composite must never be
    shown without a plain-language explanation of its self-baselined scale,
    both for the composite itself and for each dimension's own score."""
    data_dir = tmp_path / "data"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME - timedelta(hours=1))
    _write_scoring_snapshot(data_dir, RUN_ID)
    out_dir = tmp_path / "out"
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    html_text = (out_dir / "index.html").read_text()

    assert "typical for Cassandra" in html_text
    assert "not comparable" in html_text.lower()
    assert "LFX Insights" in html_text
    # The same scale note explicitly says it covers each dimension's score.
    assert "each dimension" in html_text.lower() or "dimension's score" in html_text.lower()


def _rows_without(metric_id: str) -> list[dict]:
    """`_default_rows()` minus every row for `metric_id` -- used so a test's
    own custom point for that metric is unambiguously "the latest" (never
    tied against, or shadowed by, the default fixture's own fresh Jul/Aug
    2026 rows for every M0 metric)."""
    return [row for row in _default_rows() if row["metric_id"] != metric_id]


def test_summary_card_flags_backfill_pending_for_a_stale_point(tmp_path):
    """Orchestrator review fix 2: a home summary card must not present a
    stale point as current. `time_to_first_reply_devlist`'s only point here
    is 13 months before the run's last completed month (Aug 2026) -- well
    past the 2-month staleness window -- with no `backfill_in_progress` flag
    of its own, exercising the general "window_end is old" heuristic."""
    rows = _rows_without("time_to_first_reply_devlist")
    rows.append(
        _metric_value_row(
            "time_to_first_reply_devlist", date(2025, 7, 1), date(2025, 7, 31), 0.03, 12, "ok"
        )
    )
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = (out_dir / "index.html").read_text()
    assert "backfill pending" in html_text


def test_summary_card_flags_backfill_pending_when_details_json_says_so(tmp_path):
    """The dev@ metrics' own honest `details_json.backfill_in_progress` flag
    (metrics/engine.py) is respected even for a recent-looking point."""
    rows = _rows_without("time_to_first_reply_devlist")
    row = _metric_value_row(
        "time_to_first_reply_devlist", date(2026, 8, 1), date(2026, 8, 31), 0.03, 12, "ok"
    )
    row["details_json"] = json.dumps({"backfill_in_progress": True})
    rows.append(row)
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = (out_dir / "index.html").read_text()
    assert "backfill pending" in html_text


def test_summary_card_omits_backfill_flag_for_a_fresh_point(tmp_path):
    """Negative case: every default-rows point is within 0 months of the
    run's last completed month, and none set `backfill_in_progress` --
    the badge must not appear anywhere on a normal, fresh run's home page."""
    out_dir = _build_site(tmp_path)
    html_text = (out_dir / "index.html").read_text()
    assert "backfill pending" not in html_text


def test_subpage_card_flags_backfill_pending_too(tmp_path):
    """Fix 2 applies wherever a card shows a metric's latest value, not just
    the home page's own summary cards -- the Conversations subpage card for
    the same metric must show the same flag."""
    rows = _rows_without("time_to_first_reply_devlist")
    rows.append(
        _metric_value_row(
            "time_to_first_reply_devlist", date(2025, 7, 1), date(2025, 7, 31), 0.03, 12, "ok"
        )
    )
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = (out_dir / "conversations" / "index.html").read_text()
    assert "backfill pending" in html_text


def test_format_days_shows_days_hours_and_minutes_by_magnitude():
    """Orchestrator review fix 3: a sub-day duration must not render as a
    misleading "0.0 days" -- switch to hours, then minutes, as the value
    shrinks."""
    from project_health.site.metrics_meta import format_days

    assert format_days(14.2) == "14.2 days"
    assert format_days(1.0) == "1.0 days"
    assert format_days(0.5) == "12.0 h"  # 12 hours
    assert format_days(2 / 24) == "2.0 h"
    assert format_days(0.03) == "43 min"  # ~0.72h, under 1 hour -> minutes
    assert format_days(0.0) == "0 min"


def test_card_shows_duration_in_hours_not_a_misleading_zero_days(tmp_path):
    """A real sub-day median (0.03 days, issue #57 orchestrator review) must
    never render as "0.0 days" on the Conversations card or the home
    summary card."""
    rows = _rows_without("time_to_first_reply_devlist")
    rows.append(
        _metric_value_row(
            "time_to_first_reply_devlist", date(2026, 8, 1), date(2026, 8, 31), 0.03, 12, "ok"
        )
    )
    out_dir = _build_site(tmp_path, rows=rows)
    conversations_html = (out_dir / "conversations" / "index.html").read_text()
    home_html = (out_dir / "index.html").read_text()
    assert "0.0 days" not in conversations_html
    assert "43 min" in conversations_html
    assert "0.0 days" not in home_html
    assert "43 min" in home_html


def test_chart_tooltip_uses_precomputed_display_string_for_days_metrics(tmp_path):
    """The chart tooltip for a "days" metric must show the same unit-aware
    string as the card, not a raw quantitative day-value formatted with a
    static d3-format spec (which can't switch units)."""
    rows = _rows_without("time_to_first_reply_devlist")
    rows.append(
        _metric_value_row(
            "time_to_first_reply_devlist", date(2026, 8, 1), date(2026, 8, 31), 0.03, 12, "ok"
        )
    )
    out_dir = _build_site(tmp_path, rows=rows)
    html_text = (out_dir / "conversations" / "index.html").read_text()
    specs = re.findall(r"data-vega-spec='(.*?)'", html_text)
    devlist_spec = next(
        (json.loads(html_module.unescape(s)) for s in specs if "value_display" in s), None
    )
    assert devlist_spec is not None, "expected a chart spec whose data.values carry value_display"
    tooltip_fields = {t["field"] for t in devlist_spec["encoding"]["tooltip"]}
    assert "value_display" in tooltip_fields
    assert "43 min" in json.dumps(devlist_spec["data"]["values"])
