"""Tests for the `/conversations/threads/` "Thread explorer" page (issue
#122, DECISIONS.md D27) -- `thread_explorer_page.py`'s context builder and
its full-site rendering, mirroring `test_site.py`'s own conversation-
patterns fixture pattern (real `publish.sanitize_threads`/
`write_threads_snapshot`, never a hand-rolled snapshot shape).
"""

from __future__ import annotations

import html as html_module
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from project_health.private_run.publish import sanitize_aggregates, write_threads_snapshot
from project_health.site.generate import generate
from project_health.site.thread_explorer_page import (
    CHART_OUTCOME_CATEGORIES,
    _chart_outcome_category,
    _outcome_display,
    _row_context,
    build_thread_explorer_context,
    label_year_share_rows,
    thread_disagreement_url,
    year_outcome_rows,
)

from tests.test_private_run_publish import _sample_aggregates
from tests.test_site import (
    BUILD_TIME,
    RUN_ID,
    _default_rows,
    _visible_text,
    _write_manifest,
    _write_snapshot,
)

THREADS_RUN_DATE = date(2026, 9, 29)


def _sample_thread_rows() -> list[dict]:
    return [
        {
            "venue": "mailing_list",
            "thread_key": "abc123",
            "url": "https://lists.apache.org/thread/abc123",
            "subject": "[DISCUSS] A public thread",
            "started_at": "2026-01-15T00:00:00+00:00",
            "quarter": "2026Q1",
            "n_messages": 5,
            "n_distinct_participants": 3,
            "outcome": {
                "escalation": True,
                "deescalation": False,
                "constructive_resolution": False,
                "abandonment_after_friction": True,
                "pile_on": False,
            },
            "peak_intensity_tier": 3,
            "label_counts": {"technical_disagreement": 2, "hostility": 1},
        },
        {
            "venue": "jira_comment",
            "thread_key": "CASSANDRA-1",
            "url": "https://issues.apache.org/jira/browse/CASSANDRA-1",
            "subject": "Fix the thing",
            "started_at": "2025-06-01T00:00:00+00:00",
            "quarter": "2025Q2",
            "n_messages": 2,
            "n_distinct_participants": 2,
            "outcome": {
                "escalation": False,
                "deescalation": False,
                "constructive_resolution": True,
                "abandonment_after_friction": False,
                "pile_on": False,
            },
            "peak_intensity_tier": 1,
            "label_counts": {},
        },
    ]


def _write_threads_snapshot(data_dir: Path, rows: list[dict] | None = None) -> None:
    write_threads_snapshot(
        rows if rows is not None else _sample_thread_rows(), data_dir, run_date=THREADS_RUN_DATE
    )


def _write_conversation_patterns_snapshot(data_dir: Path) -> None:
    sanitized = sanitize_aggregates(_sample_aggregates())
    snapshot_dir = data_dir / "snapshots" / "conversation_patterns"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "2026-09-28.json").write_text(json.dumps(sanitized))


def _build_site_with_threads(
    tmp_path: Path, *, rows: list[dict] | None = None
) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    # The Conversation patterns section/card is what carries the D27 links
    # to the Thread explorer (both are gated on `conversation_patterns.
    # available`), so a full "links render" test needs both snapshots
    # published, same as a real run would produce.
    _write_conversation_patterns_snapshot(data_dir)
    _write_threads_snapshot(data_dir, rows)
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME)
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    return out_dir, data_dir


def _build_site_without_threads(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_snapshot(data_dir, RUN_ID, _default_rows())
    _write_manifest(data_dir, RUN_ID, completed_at=BUILD_TIME)
    generate(data_dir, RUN_ID, out_dir, now=BUILD_TIME)
    return out_dir, data_dir


class TestBuildThreadExplorerContext:
    def test_honest_empty_state_without_snapshot(self, tmp_path):
        ctx = build_thread_explorer_context(
            tmp_path / "data", tmp_path / "out", base_prefix="../../"
        )
        assert ctx["available"] is False

    def test_available_with_rows_from_snapshot(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_threads_snapshot(data_dir)
        ctx = build_thread_explorer_context(data_dir, tmp_path / "out", base_prefix="../../")
        assert ctx["available"] is True
        assert ctx["row_count"] == 2
        assert ctx["default_sort_field"] == "date"
        assert ctx["default_sort_dir"] == "desc"

    def test_writes_rows_json_for_client_fetch(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_threads_snapshot(data_dir)
        out_dir = tmp_path / "out"
        build_thread_explorer_context(data_dir, out_dir, base_prefix="../../")
        rows_path = out_dir / "data" / "conversation-threads.json"
        assert rows_path.is_file()
        payload = json.loads(rows_path.read_text(encoding="utf-8"))
        assert payload["row_count"] == 2
        venues = {r["venue"] for r in payload["rows"]}
        assert venues == {"mailing_list", "jira_comment"}

    def test_reads_latest_of_multiple_threads_snapshots(self, tmp_path):
        data_dir = tmp_path / "data"
        write_threads_snapshot(_sample_thread_rows(), data_dir, run_date=date(2026, 1, 1))
        write_threads_snapshot(
            [_sample_thread_rows()[0]], data_dir, run_date=date(2026, 9, 29)
        )
        ctx = build_thread_explorer_context(data_dir, tmp_path / "out", base_prefix="../../")
        assert ctx["row_count"] == 1


class TestOutcomeDisplay:
    def test_escalation_and_deescalation_both_shown(self):
        # Issue #122 fixup: a thread that both escalated and later
        # de-escalated must show both, not just "escalated".
        outcome = {
            "escalation": True,
            "deescalation": True,
            "constructive_resolution": False,
            "abandonment_after_friction": False,
            "pile_on": False,
        }
        assert _outcome_display(outcome) == "escalated, then de-escalated"

    def test_resolved_only(self):
        outcome = {
            "escalation": False,
            "deescalation": False,
            "constructive_resolution": True,
            "abandonment_after_friction": False,
            "pile_on": False,
        }
        assert _outcome_display(outcome) == "resolved"

    def test_none_when_no_flags(self):
        outcome = {
            "escalation": False,
            "deescalation": False,
            "constructive_resolution": False,
            "abandonment_after_friction": False,
            "pile_on": False,
        }
        assert _outcome_display(outcome) == "none"

    def test_escalation_then_resolution(self):
        outcome = {
            "escalation": True,
            "deescalation": False,
            "constructive_resolution": True,
            "abandonment_after_friction": False,
            "pile_on": False,
        }
        assert _outcome_display(outcome) == "escalated, then resolved"

    def test_row_context_exposes_individual_outcome_flags(self):
        row = _sample_thread_rows()[0]  # escalation=True, abandonment_after_friction=True
        ctx = _row_context(row)
        assert ctx["outcome_display"] == "escalated, then abandoned after friction"
        assert ctx["outcome_flags"] == {
            "resolved": False,
            "escalated": True,
            "de-escalated": False,
            "abandoned after friction": True,
        }


class TestThreadDisagreementUrl:
    def test_prefills_thread_url_and_scores(self):
        row = _sample_thread_rows()[0]
        url = thread_disagreement_url(row)
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert params["template"] == ["thread-score-disagreement.yml"]
        assert params["thread_url"] == [row["url"]]
        scores = params["scores"][0]
        # `_outcome_display` composes every true flag in narrative order
        # (issue #122 fixup) -- this fixture row has both escalation=True
        # and abandonment_after_friction=True, so both appear.
        assert "escalated, then abandoned after friction" in scores
        assert "technical_disagreement=2" in scores
        assert "hostility=1" in scores

    def test_pile_on_noted_in_scores(self):
        row = _sample_thread_rows()[0]
        row = {**row, "outcome": {**row["outcome"], "pile_on": True}}
        params = parse_qs(urlparse(thread_disagreement_url(row)).query)
        assert "pile-on" in params["scores"][0]


class TestThreadExplorerPageRendering:
    def test_no_snapshot_no_page(self, tmp_path):
        out_dir, _ = _build_site_without_threads(tmp_path)
        assert not (out_dir / "conversations" / "threads" / "index.html").exists()
        conversations_html = (out_dir / "conversations" / "index.html").read_text()
        community_html = (out_dir / "community" / "index.html").read_text()
        assert "conversations/threads/" not in conversations_html
        assert "conversations/threads/" not in community_html

    def test_page_renders_from_synthetic_snapshot(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        page_path = out_dir / "conversations" / "threads" / "index.html"
        assert page_path.is_file()
        html_text = page_path.read_text()
        assert "Thread explorer" in html_text
        assert 'data-threads-table' in html_text

        rows_path = out_dir / "data" / "conversation-threads.json"
        payload = json.loads(rows_path.read_text(encoding="utf-8"))
        subjects = {r["subject"] for r in payload["rows"]}
        assert "[DISCUSS] A public thread" in subjects
        assert "Fix the thing" in subjects
        urls = {r["url"] for r in payload["rows"]}
        assert "https://lists.apache.org/thread/abc123" in urls
        assert "https://issues.apache.org/jira/browse/CASSANDRA-1" in urls

    def test_default_sort_is_date_descending(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        assert 'data-default-sort="date"' in html_text
        assert 'data-default-dir="desc"' in html_text
        date_th = 'data-threads-sort="date" tabindex="0" role="button" aria-sort="descending"'
        assert date_th in html_text

    def test_disagree_link_present_per_row(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        rows_path = out_dir / "data" / "conversation-threads.json"
        payload = json.loads(rows_path.read_text(encoding="utf-8"))
        for row in payload["rows"]:
            assert "issues/new" in row["disagree_url"]
            assert "thread_url=" in row["disagree_url"]

    def test_links_from_conversation_patterns_section_and_community_card(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        conversations_html = (out_dir / "conversations" / "index.html").read_text()
        community_html = (out_dir / "community" / "index.html").read_text()
        assert 'href="threads/"' in conversations_html
        assert "conversations/threads/" in community_html
        assert "Explore individual threads" in community_html

    def test_explore_threads_cta_prominent_in_two_places_on_conversations_page(self, tmp_path):
        # Issue #124: a prominent "Explore individual threads ->" button in
        # the Conversation patterns section header *and* after the summary
        # -- not just the small inline mention inside the intro paragraph
        # that issue #122 already shipped.
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "index.html").read_text()
        occurrences = html_text.count("Explore individual threads")
        assert occurrences >= 2
        assert html_text.count('class="btn-cta" href="threads/"') >= 2
        heading_index = html_text.index('id="conversation-patterns-heading"')
        summary_heading_index = html_text.index('id="conv-summary-heading"')
        first_cta_index = html_text.index("Explore individual threads")
        # The first CTA sits between the section heading and the summary.
        assert heading_index < first_cta_index < summary_heading_index

    def test_charts_and_trends_link_at_top_of_explorer(self, tmp_path):
        # Issue #124: "'Charts and trends <-' link at the top of the
        # explorer."
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        assert "Charts and trends" in html_text
        assert html_text.index("Charts and trends") < html_text.index(
            'id="thread-explorer-heading"'
        )

    def test_no_verdict_vocabulary(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        text = _visible_text(html_text).lower()
        for banned in (
            "fail",
            "failing",
            "pass rate",
            "exempt",
            "not in force",
            "verdict",
            "compliance",
            "healthy",
            "unhealthy",
            "worst",
        ):
            assert re.search(rf"\b{re.escape(banned)}\b", text) is None, banned

    def test_no_absolute_root_urls(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        assert 'href="/' not in html_text
        assert 'src="/' not in html_text


# --- Chart data derivation (issue #124) --------------------------------


def _synthetic_chart_rows() -> list[dict]:
    """Synthetic thread rows in `_row_context`'s own output shape (just
    the fields the chart derivation functions actually read) -- covers
    every outcome category across two years and two venues, so a test can
    filter this fixture down (e.g. to one venue) before calling a
    derivation function and see the aggregate change accordingly."""
    return [
        {
            "venue": "mailing_list",
            "year": "2021",
            "outcome_flags": {
                "resolved": False,
                "escalated": True,
                "de-escalated": True,
                "abandoned after friction": False,
            },
            "label_counts": {"hostility": 2, "technical_disagreement": 1},
        },
        {
            "venue": "mailing_list",
            "year": "2021",
            "outcome_flags": {
                "resolved": True,
                "escalated": False,
                "de-escalated": False,
                "abandoned after friction": False,
            },
            "label_counts": {"acknowledgment": 1},
        },
        {
            "venue": "jira_comment",
            "year": "2021",
            "outcome_flags": {
                "resolved": False,
                "escalated": False,
                "de-escalated": False,
                "abandoned after friction": False,
            },
            "label_counts": {},
        },
        {
            "venue": "mailing_list",
            "year": "2022",
            "outcome_flags": {
                "resolved": False,
                "escalated": True,
                "de-escalated": False,
                "abandoned after friction": False,
            },
            "label_counts": {"hostility": 1},
        },
        {
            "venue": "jira_comment",
            "year": "2022",
            "outcome_flags": {
                "resolved": False,
                "escalated": False,
                "de-escalated": False,
                "abandoned after friction": True,
            },
            "label_counts": {"gatekeeping": 1},
        },
    ]


class TestChartOutcomeCategory:
    def test_escalated_and_deescalated_is_composite_category(self):
        assert (
            _chart_outcome_category({"escalated": True, "de-escalated": True})
            == "escalated, then de-escalated"
        )

    def test_escalated_only(self):
        assert _chart_outcome_category({"escalated": True}) == "escalated"

    def test_resolved_only(self):
        assert _chart_outcome_category({"resolved": True}) == "resolved"

    def test_abandoned_only(self):
        assert (
            _chart_outcome_category({"abandoned after friction": True})
            == "abandoned after friction"
        )

    def test_no_flags_is_none(self):
        assert _chart_outcome_category({}) == "none"

    def test_escalation_takes_priority_over_resolution(self):
        # A thread can carry both an escalation and a resolution flag at
        # once (issue #122's own `_outcome_display` fixup) -- the chart
        # still needs one mutually-exclusive category per thread.
        assert _chart_outcome_category({"escalated": True, "resolved": True}) == "escalated"


class TestYearOutcomeRows:
    def test_by_year_outcome_stacks_from_synthetic_rows(self):
        rows = _synthetic_chart_rows()
        result = year_outcome_rows(rows)
        as_dict = {(r["year"], r["outcome"]): r["count"] for r in result}
        assert as_dict == {
            ("2021", "escalated, then de-escalated"): 1,
            ("2021", "resolved"): 1,
            ("2021", "none"): 1,
            ("2022", "escalated"): 1,
            ("2022", "abandoned after friction"): 1,
        }

    def test_output_order_is_year_then_fixed_category_order_not_alphabetical(self):
        rows = _synthetic_chart_rows()
        result = year_outcome_rows(rows)
        years_seen = [r["year"] for r in result]
        assert years_seen == sorted(years_seen)
        for year in set(years_seen):
            categories_for_year = [r["outcome"] for r in result if r["year"] == year]
            assert categories_for_year == sorted(
                categories_for_year, key=CHART_OUTCOME_CATEGORIES.index
            )

    def test_rows_missing_year_are_skipped(self):
        rows = [{"year": "", "outcome_flags": {"resolved": True}, "label_counts": {}}]
        assert year_outcome_rows(rows) == []

    def test_empty_input_returns_empty_list(self):
        assert year_outcome_rows([]) == []


class TestLabelYearShareRows:
    def test_per_label_share_from_synthetic_rows(self):
        rows = _synthetic_chart_rows()
        result = label_year_share_rows(rows, ("hostility",))
        by_year = {r["year"]: r for r in result}
        # 2021: 3 total rows (mailing_list x2, jira_comment x1), 1 flagged
        # for hostility.
        assert by_year["2021"]["count"] == 1
        assert by_year["2021"]["total"] == 3
        assert by_year["2021"]["share"] == pytest.approx(1 / 3)
        # 2022: 2 total rows, 1 flagged for hostility.
        assert by_year["2022"]["count"] == 1
        assert by_year["2022"]["total"] == 2
        assert by_year["2022"]["share"] == pytest.approx(0.5)

    def test_share_with_filter_applied(self):
        # Issue #124 acceptance: "per-label share with filter applied" --
        # filtering to `mailing_list` first (same "filter, then aggregate"
        # contract `thread_explorer.js` uses for the table's own filters)
        # changes both the numerator and the denominator.
        rows = [r for r in _synthetic_chart_rows() if r["venue"] == "mailing_list"]
        result = label_year_share_rows(rows, ("hostility",))
        by_year = {r["year"]: r for r in result}
        assert by_year["2021"]["total"] == 2  # jira_comment row excluded
        assert by_year["2021"]["count"] == 1
        assert by_year["2021"]["share"] == pytest.approx(0.5)
        assert by_year["2022"]["total"] == 1
        assert by_year["2022"]["count"] == 1
        assert by_year["2022"]["share"] == pytest.approx(1.0)

    def test_label_with_zero_count_still_present_with_zero_share(self):
        rows = _synthetic_chart_rows()
        result = label_year_share_rows(rows, ("dismissiveness",))
        assert all(r["count"] == 0 and r["share"] == 0.0 for r in result)
        assert {r["year"] for r in result} == {"2021", "2022"}

    def test_multiple_labels_each_get_every_year(self):
        rows = _synthetic_chart_rows()
        result = label_year_share_rows(rows, ("hostility", "acknowledgment"))
        labels_seen = {r["label"] for r in result}
        assert labels_seen == {"hostility", "acknowledgment"}
        assert len(result) == 4  # 2 labels x 2 years

    def test_empty_input_returns_empty_list(self):
        assert label_year_share_rows([], ("hostility",)) == []


class TestThreadExplorerChartsRendering:
    def test_charts_section_renders_with_both_chart_divs(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        assert "data-threads-charts" in html_text
        assert 'data-threads-chart="outcome"' in html_text
        assert 'data-threads-chart="label-constructive"' in html_text
        assert 'data-threads-chart="label-negative"' in html_text
        assert "data-threads-outcome-toggle" in html_text

    def test_charts_render_above_the_table(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        assert html_text.index("data-threads-charts") < html_text.index("data-threads-table")

    def test_sample_size_footnote_present(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        assert "Counts are of sampled threads (K=60 per venue-quarter)" in html_text
        assert "Conversation patterns" in html_text.split("Counts are of sampled")[1][:200]

    def test_outcome_chart_spec_is_valid_json_with_fixed_category_domain(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        match = re.search(
            r'data-threads-chart="outcome"[^>]*data-threads-chart-spec=\'(.*?)\'',
            html_text,
            re.S,
        )
        assert match is not None
        spec = json.loads(html_module.unescape(match.group(1)))
        assert spec["mark"]["type"] == "bar"
        assert spec["encoding"]["color"]["scale"]["domain"] == list(CHART_OUTCOME_CATEGORIES)
        years = {row["year"] for row in spec["data"]["values"]}
        assert years == {"2025", "2026"}  # from `_sample_thread_rows()`

    def test_label_chart_specs_use_fixed_group_order_not_alphabetical(self, tmp_path):
        out_dir, _ = _build_site_with_threads(tmp_path)
        html_text = (out_dir / "conversations" / "threads" / "index.html").read_text()
        match = re.search(
            r'data-threads-chart="label-constructive"[^>]*data-threads-chart-spec=\'(.*?)\'',
            html_text,
            re.S,
        )
        assert match is not None
        spec = json.loads(html_module.unescape(match.group(1)))
        # Issue #133: small multiples -- one mini-chart per label, faceted,
        # independent y-scale per label, one flat color per panel.
        assert spec["facet"]["field"] == "label_display"
        assert spec["facet"]["sort"] == [
            "Acknowledgment",
            "Compromise offer",
            "Constructive counterargument",
            "Evidence based argument",
            "Resolution marker",
            "Technical disagreement",
        ]
        assert spec["resolve"]["scale"]["y"] == "independent"
        assert spec["spec"]["mark"]["type"] == "bar"
        assert "color" not in spec["spec"]["encoding"]

    def test_no_charts_section_when_no_threads_snapshot(self, tmp_path):
        out_dir, _ = _build_site_without_threads(tmp_path)
        assert not (out_dir / "conversations" / "threads" / "index.html").exists()
