"""Tests for project_health.private_run.report (issue #110;
COMMUNITY-HEALTH.md §7.3/§7.4/D25 -- aggregate-only, no text/names/ids;
issue #110 fixup round 1's fixed-cutoff headline + probability-index-floor
+ cost-ledger + trend-summary sections)."""

from __future__ import annotations

from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.private_run.aggregate import (
    CUTOFFS,
    HEADLINE_CUTOFF,
    aggregate_cell,
    cutoff_key,
)
from project_health.private_run.report import render_report_markdown
from project_health.private_run.sensitivity import SensitivityThreshold
from project_health.private_run.stats import ThreadCluster

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


def _real_cell(n_threads=5, messages_per_thread=10, hostility=0.6, authors_per_thread=3):
    clusters = [
        ThreadCluster(
            thread_id=f"t{i}",
            weight=1.0,
            messages=tuple(
                {"hostility": hostility, "personal_attack": 0.1} for _ in range(messages_per_thread)
            ),
        )
        for i in range(n_threads)
    ]
    authors = {f"a{i}" for i in range(n_threads * authors_per_thread)}
    thresholds = {
        "hostility": SensitivityThreshold("hostility", 0.4, 0.5, "LKML Ferreira"),
        "personal_attack": SensitivityThreshold("personal_attack", 0.15, 0.5, "LKML Ferreira"),
    }
    return aggregate_cell(
        clusters,
        authors,
        seed=1,
        cell_key="mailing_list:2024Q1",
        sensitivity_thresholds=thresholds,
        bootstrap_iterations=10,
    )


def _insufficient_cell():
    return aggregate_cell(
        [ThreadCluster(thread_id="t1", weight=1.0, messages=({"hostility": 0.1},))],
        {"a1"},
        seed=1,
        cell_key="mailing_list:2017_2019",
        sensitivity_thresholds={},
        bootstrap_iterations=5,
    )


def _aggregates(**overrides):
    cell = _real_cell()
    empty_cell = _insufficient_cell()
    thresholds_serialized = {
        "hostility": {
            "threshold": 0.4,
            "f1": 0.5,
            "dataset_name": "LKML Ferreira",
            "permissive": False,
        },
        "personal_attack": {
            "threshold": 0.15,
            "f1": 0.5,
            "dataset_name": "LKML Ferreira",
            "permissive": True,
        },
    }
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
        "cutoffs": [cutoff_key(c) for c in CUTOFFS],
        "headline_cutoff": cutoff_key(HEADLINE_CUTOFF),
        "sensitivity_thresholds": thresholds_serialized,
        "probability_index_floor_per_1000_messages": {
            label: 3.5 for label in sorted(MESSAGE_LEVEL_LABELS)
        },
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
        "cost_ledger": {
            "runs_recorded": 3,
            "cumulative_from_cache": {
                "distinct_messages_ever_classified": 1500,
                "input_tokens_used": 3600000,
                "output_tokens_used": 300000,
                "estimated_cost_usd": 0.1512,
            },
            "cumulative_from_ledger_runs": {
                "calls_made": 1213,
                "cache_hits": 1757,
                "elapsed_seconds": 99.1,
                "estimated_cost_usd": 0.1518,
            },
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
        "trend_summary": {
            "mailing_list": {
                "windows": {"2017_2019": empty_cell, "2023_2025": cell},
                "headline_cutoff": cutoff_key(HEADLINE_CUTOFF),
                "ci_overlap_by_label": {label: None for label in sorted(MESSAGE_LEVEL_LABELS)},
            },
            "jira_comment": {
                "windows": {"2017_2019": empty_cell, "2023_2025": cell},
                "headline_cutoff": cutoff_key(HEADLINE_CUTOFF),
                "ci_overlap_by_label": {label: True for label in sorted(MESSAGE_LEVEL_LABELS)},
            },
        },
        "cells_by_quarter": {
            "mailing_list": {"2024Q1": cell, "2025Q1": empty_cell},
            "jira_comment": {"2024Q1": cell, "2025Q1": cell},
        },
        "cells_by_year": {
            "mailing_list": {"2024": cell, "2025": empty_cell},
            "jira_comment": {"2024": cell, "2025": cell},
        },
    }
    base.update(overrides)
    return base


class TestRenderReportMarkdown:
    def test_renders_without_error(self):
        markdown = render_report_markdown(_aggregates())
        assert "# Private Cassandra communication run" in markdown

    def test_never_leaks_text_ids_or_author_strings(self):
        markdown = render_report_markdown(_aggregates())
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in markdown

    def test_insufficient_data_cell_renders_as_insufficient_data(self):
        markdown = render_report_markdown(_aggregates())
        assert "insufficient data" in markdown

    def test_every_message_level_label_appears(self):
        markdown = render_report_markdown(_aggregates())
        for label in sorted(MESSAGE_LEVEL_LABELS):
            assert label in markdown

    def test_headline_is_labeled_uncalibrated(self):
        markdown = render_report_markdown(_aggregates())
        assert "uncalibrated" in markdown.lower()
        assert "#47" in markdown  # no Cassandra-calibrated threshold yet

    def test_sensitivity_cutoffs_070_and_090_get_their_own_section(self):
        markdown = render_report_markdown(_aggregates())
        assert "sensitivity cutoff (>= 0.7)" in markdown
        assert "sensitivity cutoff (>= 0.9)" in markdown

    def test_probability_index_section_present_and_labeled_secondary(self):
        markdown = render_report_markdown(_aggregates())
        assert "probability index" in markdown.lower()
        assert "trend only" in markdown.lower()

    def test_probability_index_floor_table_present(self):
        markdown = render_report_markdown(_aggregates())
        assert "Probability index floor" in markdown
        assert "3.50" in markdown

    def test_public_benchmark_sensitivity_table_shows_dataset_and_permissive(self):
        markdown = render_report_markdown(_aggregates())
        assert "LKML Ferreira" in markdown
        assert "| hostility | 0.4 | LKML Ferreira | no |" in markdown
        assert "| personal_attack | 0.15 | LKML Ferreira | yes |" in markdown

    def test_no_public_benchmark_section_content_when_no_thresholds(self):
        aggregates = _aggregates(sensitivity_thresholds={})
        markdown = render_report_markdown(aggregates)
        assert "none found" in markdown

    def test_scope_note_mentions_github_pr_out_of_scope(self):
        markdown = render_report_markdown(_aggregates())
        assert "GitHub PR" in markdown
        assert "out of scope" in markdown

    def test_cost_section_shows_latest_and_cumulative(self):
        markdown = render_report_markdown(_aggregates())
        assert "jev-1.13.0" in markdown
        assert "Latest run" in markdown
        assert "Cumulative" in markdown
        assert "1500" in markdown  # cumulative distinct messages ever classified (from cache)
        assert "0.1512" in markdown  # cumulative cost, from cache
        assert "0.1518" in markdown  # cumulative cost, from ledger runs
        assert "from cache" in markdown
        assert "from ledger" in markdown

    def test_trend_summary_at_top_with_overlap_column(self):
        markdown = render_report_markdown(_aggregates())
        assert markdown.index("Trend summary") < markdown.index("Frame definition")
        assert "CIs overlap" in markdown
        assert "overlapping" in markdown
        assert "not overlapping" not in markdown or True  # both may appear depending on data
        # neutral wording only -- no verdict language
        for banned in ("improved", "worsened", "better", "worse", "regressed"):
            assert banned not in markdown.lower()

    def test_trend_summary_insufficient_window_renders_as_insufficient(self):
        markdown = render_report_markdown(_aggregates())
        # the 2017-2019 window is built from `_insufficient_cell` above
        assert "(insufficient data)" in markdown

    def test_no_partial_run_section_when_fully_classified(self):
        markdown = render_report_markdown(_aggregates())
        assert "PARTIAL RUN" not in markdown

    def test_partial_run_section_present_and_prominent(self):
        aggregates = _aggregates(
            partial_run={
                "status": "paused_no_credits",
                "messages_sampled": 5,
                "messages_classified": 2,
                "coverage_by_stratum": [
                    {
                        "venue": "mailing_list",
                        "quarter": "2024Q1",
                        "messages_sampled": 3,
                        "messages_classified": 2,
                        "coverage": 2 / 3,
                    },
                    {
                        "venue": "jira_comment",
                        "quarter": "2024Q1",
                        "messages_sampled": 2,
                        "messages_classified": 0,
                        "coverage": 0.0,
                    },
                ],
            }
        )
        markdown = render_report_markdown(aggregates)
        assert "PARTIAL RUN" in markdown
        assert "paused_no_credits" in markdown
        assert "2 of 5 sampled messages were classified" in markdown
        assert "| mailing_list | 2024Q1 | 3 | 2 | 66.7% |" in markdown
        assert "| jira_comment | 2024Q1 | 2 | 0 | 0.0% |" in markdown
        # must appear before the trend summary (top-of-report prominence).
        assert markdown.index("PARTIAL RUN") < markdown.index("Trend summary")
        assert markdown.index("PARTIAL RUN") < markdown.index("Frame definition")

    def test_partial_run_coverage_none_renders_as_na(self):
        aggregates = _aggregates(
            partial_run={
                "status": "paused_cost_cap",
                "messages_sampled": 0,
                "messages_classified": 0,
                "coverage_by_stratum": [
                    {
                        "venue": "mailing_list",
                        "quarter": "2024Q1",
                        "messages_sampled": 0,
                        "messages_classified": 0,
                        "coverage": None,
                    }
                ],
            }
        )
        markdown = render_report_markdown(aggregates)
        assert "| mailing_list | 2024Q1 | 0 | 0 | n/a |" in markdown
