"""Tests for project_health.private_run.report (issue #110;
COMMUNITY-HEALTH.md §7.3/§7.4 -- aggregate-only, no text/names/ids)."""

from __future__ import annotations

from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.private_run.report import render_report_markdown

# A distinctive, synthetic thread id / message id / author string that must
# never appear in the rendered report -- exactly the kind of value this
# module is forbidden from ever seeing or emitting (report.py's own module
# docstring: "it never has access to message text, thread ids, message
# ids... in the first place").
_FORBIDDEN_SUBSTRINGS = (
    "SUPER-SECRET-THREAD-ID-0001",
    "<forbidden-message-id@example.com>",
    "forbidden.author@example.com",
    "this text must never leak into the report",
)


def _cell(messages=40, authors=12, insufficient=False):
    rates = {
        label: (None if insufficient else {"per_1000": 12.5, "ci95": [10.0, 15.0]})
        for label in sorted(MESSAGE_LEVEL_LABELS)
    }
    sensitivity = {
        label: (None if insufficient else 5.0)
        for label in ("personal_attack", "hostility", "sarcasm")
    }
    return {
        "messages_classified": messages,
        "distinct_authors": authors,
        "threads_sampled": 10,
        "threads_population": 100,
        "insufficient_data": insufficient,
        "rates_per_1000_messages": rates,
        "sensitivity_per_1000_messages": sensitivity,
    }


def _aggregates(**overrides):
    base = {
        "generated_at": "2026-09-28T00:00:00+00:00",
        "seed": 110,
        "k": 60,
        "quarters": ["2024Q1", "2025Q1"],
        "venues": ["mailing_list", "jira_comment"],
        "frame_definition": {
            "mailing_list": "thread = a dev@ Pony Mail thread; quarter = started_at",
            "jira_comment": "thread = one JIRA issue's comment stream; quarter = created_at",
        },
        "scope_note": "GitHub PR comments are out of scope for v1.",
        "sensitivity_thresholds": {"personal_attack": 0.15, "hostility": 0.4, "sarcasm": 0.6},
        "floors": {"min_messages": 30, "min_distinct_authors": 10},
        "cost_summary": {
            "status": "completed",
            "calls_made": 10,
            "cache_hits": 5,
            "input_tokens_used": 1000,
            "output_tokens_used": 200,
            "estimated_cost_usd": 0.001,
            "elapsed_seconds": 3.2,
            "mean_latency_seconds_per_call": 0.32,
            "classifier_version": "1.0.0",
            "question_set_version": "1",
            "model_id_pinned": "jev-1.13.0",
        },
        "venue_totals": {
            "mailing_list": {
                "messages_classified": 80,
                "threads_sampled": 20,
                "threads_population": 200,
            },
            "jira_comment": {
                "messages_classified": 60,
                "threads_sampled": 15,
                "threads_population": 150,
            },
        },
        "cells_by_quarter": {
            "mailing_list": {
                "2024Q1": _cell(),
                "2025Q1": _cell(messages=5, authors=2, insufficient=True),
            },
            "jira_comment": {"2024Q1": _cell(), "2025Q1": _cell()},
        },
        "cells_by_year": {
            "mailing_list": {
                "2024": _cell(),
                "2025": _cell(messages=5, authors=2, insufficient=True),
            },
            "jira_comment": {"2024": _cell(), "2025": _cell()},
        },
    }
    base.update(overrides)
    return base


class TestRenderReportMarkdown:
    def test_renders_without_error(self):
        markdown = render_report_markdown(_aggregates())
        assert "# Private Cassandra communication run" in markdown

    def test_never_leaks_text_ids_or_author_strings(self):
        # the aggregates dict itself never contains these -- this asserts
        # the renderer doesn't invent a way to leak something it was never
        # given, and stands in for a fixture that accidentally carried one.
        markdown = render_report_markdown(_aggregates())
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in markdown

    def test_insufficient_data_cell_renders_as_insufficient_data(self):
        markdown = render_report_markdown(_aggregates())
        assert "insufficient data" in markdown

    def test_every_message_level_label_appears_in_the_header(self):
        markdown = render_report_markdown(_aggregates())
        for label in sorted(MESSAGE_LEVEL_LABELS):
            assert label in markdown

    def test_sensitivity_section_present_when_thresholds_exist(self):
        markdown = render_report_markdown(_aggregates())
        assert "sensitivity" in markdown.lower()
        assert "0.15" in markdown  # personal_attack threshold

    def test_no_sensitivity_section_when_no_thresholds(self):
        aggregates = _aggregates(sensitivity_thresholds={})
        markdown = render_report_markdown(aggregates)
        assert "none found" in markdown

    def test_scope_note_mentions_github_pr_out_of_scope(self):
        markdown = render_report_markdown(_aggregates())
        assert "GitHub PR" in markdown
        assert "out of scope" in markdown

    def test_cost_summary_numbers_appear(self):
        markdown = render_report_markdown(_aggregates())
        assert "jev-1.13.0" in markdown
        assert "0.001" in markdown
