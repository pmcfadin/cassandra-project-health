"""Tests for the `/conversations/threads/` "Thread explorer" page (issue
#122, DECISIONS.md D27) -- `thread_explorer_page.py`'s context builder and
its full-site rendering, mirroring `test_site.py`'s own conversation-
patterns fixture pattern (real `publish.sanitize_threads`/
`write_threads_snapshot`, never a hand-rolled snapshot shape).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from project_health.private_run.publish import sanitize_aggregates, write_threads_snapshot
from project_health.site.generate import generate
from project_health.site.thread_explorer_page import (
    _outcome_display,
    _row_context,
    build_thread_explorer_context,
    thread_disagreement_url,
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
