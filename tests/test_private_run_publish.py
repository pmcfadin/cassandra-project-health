"""Tests for `project_health.private_run.publish` (issue #118, DECISIONS.md
D26): the sanitizer allowlist, its hard-fail leak/floor checks, and the
`snapshots/conversation_patterns/<run date>.json` writer.

All fixtures here are synthetic -- never the real private aggregates.json
(that file stays under `~/project-health-private/`, read-only, and is never
copied into this repo or a test fixture, per the task's own instructions).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.private_run.publish import (
    SanitizeError,
    sanitize_aggregates,
    write_conversation_patterns_snapshot,
)
from project_health.private_run.sensitivity import STRONG_GATING_LABELS

VENUES = ("mailing_list", "jira_comment")
CUTOFFS = ("0.5", "0.7", "0.9")
THREAD_CUTOFFS = ("0.5", "0.7")


def _rate_entry(per_1000: float = 5.0) -> dict:
    return {"per_1000": per_1000, "ci95": [per_1000 - 1.0, per_1000 + 1.0]}


def _sensitivity_entry(label: str) -> dict:
    return {
        "per_1000": 3.0,
        "threshold": 0.4,
        "dataset_name": f"{label}-dataset",
        "permissive": False,
    }


def _cell(*, insufficient: bool = False) -> dict:
    if insufficient:
        cutoff_rates = {
            label: {cutoff: None for cutoff in CUTOFFS} for label in MESSAGE_LEVEL_LABELS
        }
        prob_index = {label: None for label in MESSAGE_LEVEL_LABELS}
        sensitivity = {label: None for label in STRONG_GATING_LABELS}
        messages, authors = 5, 2
    else:
        cutoff_rates = {
            label: {cutoff: _rate_entry() for cutoff in CUTOFFS} for label in MESSAGE_LEVEL_LABELS
        }
        prob_index = {label: _rate_entry(2.0) for label in MESSAGE_LEVEL_LABELS}
        sensitivity = {label: _sensitivity_entry(label) for label in STRONG_GATING_LABELS}
        messages, authors = 50, 15
    return {
        "messages_classified": messages,
        "distinct_authors": authors,
        "threads_sampled": 10,
        "threads_population": 100,
        "insufficient_data": insufficient,
        "cutoff_rates_per_1000_messages": cutoff_rates,
        "probability_index_per_1000_messages": prob_index,
        "sensitivity_per_1000_messages": sensitivity,
    }


def _thread_rate_entry(*, insufficient: bool = False) -> dict:
    if insufficient:
        return {
            "qualifying_threads": 3,
            "distinct_participants": 2,
            "insufficient_data": True,
            "rate": None,
            "ci95": None,
        }
    return {
        "qualifying_threads": 40,
        "distinct_participants": 12,
        "insufficient_data": False,
        "rate": 0.2,
        "ci95": [0.1, 0.3],
    }


def _pile_on_entry(*, insufficient: bool = True) -> dict:
    if insufficient:
        return {
            "threads_total": 40,
            "distinct_targets": 2,
            "insufficient_data": True,
            "per_100_threads": None,
            "ci95": None,
        }
    return {
        "threads_total": 40,
        "distinct_targets": 8,
        "insufficient_data": False,
        "per_100_threads": 1.5,
        "ci95": [0.5, 2.5],
    }


def _thread_metrics_entry() -> dict:
    return {
        "threads_total": 40,
        "escalation_rate": _thread_rate_entry(),
        "constructive_resolution_rate": _thread_rate_entry(),
        "thread_abandonment_rate_post_friction": _thread_rate_entry(),
        "pile_on_rate": _pile_on_entry(),
    }


TONE_TIERS = ("-2", "-1", "0", "1", "2", "3", "4")
TONE_TIER_NAMES = {
    "-2": "Closing/positive",
    "-1": "Constructive/positive",
    "0": "Neutral",
    "1": "Substantive disagreement",
    "2": "Non-substantive friction",
    "3": "Hostile",
    "4": "Attack",
}


def _tone_mix_cell(*, insufficient: bool = False) -> dict:
    if insufficient:
        tiers = {tier: None for tier in TONE_TIERS}
        messages, authors = 5, 2
    else:
        # Shares deliberately sum to 1.0 across tiers, matching
        # `tone_mix.weighted_tier_shares`'s own invariant.
        shares = {
            "-2": 0.05,
            "-1": 0.15,
            "0": 0.50,
            "1": 0.15,
            "2": 0.10,
            "3": 0.03,
            "4": 0.02,
        }
        tiers = {
            tier: {
                "name": TONE_TIER_NAMES[tier],
                "share": share,
                "ci95": [max(0.0, share - 0.02), share + 0.02],
            }
            for tier, share in shares.items()
        }
        messages, authors = 50, 15
    return {
        "messages_classified": messages,
        "distinct_authors": authors,
        "threads_sampled": 10,
        "threads_population": 100,
        "insufficient_data": insufficient,
        "tiers": tiers,
    }


def _newcomer_entry(*, insufficient: bool = False) -> dict:
    if insufficient:
        return {
            "messages_directed_at_newcomers": 5,
            "distinct_newcomers": 2,
            "insufficient_data": True,
            "constructive_response_rate": None,
            "constructive_response_rate_ci95": None,
            "dismissive_hostile_response_rate": None,
            "dismissive_hostile_response_rate_ci95": None,
        }
    return {
        "messages_directed_at_newcomers": 40,
        "distinct_newcomers": 12,
        "insufficient_data": False,
        "constructive_response_rate": 0.5,
        "constructive_response_rate_ci95": [0.3, 0.7],
        "dismissive_hostile_response_rate": 0.1,
        "dismissive_hostile_response_rate_ci95": [0.02, 0.2],
    }


def _sample_aggregates(**overrides) -> dict:
    aggregates = {
        "generated_at": "2026-09-28T10:00:00+00:00",
        "seed": 42,
        "k": 30,
        "quarters": ["2024Q1", "2024Q2"],
        "venues": list(VENUES),
        "frame_definition": {v: f"thread = a {v} thread" for v in VENUES},
        "scope_note": "GitHub PR comments are out of scope for v1 (DECISIONS.md D7).",
        "sensitivity_thresholds": {
            label: {
                "threshold": 0.4,
                "f1": 0.8,
                "dataset_name": f"{label}-dataset",
                "permissive": False,
            }
            for label in STRONG_GATING_LABELS
        },
        "cutoffs": list(CUTOFFS),
        "headline_cutoff": "0.5",
        "probability_index_floor_per_1000_messages": {label: 1.0 for label in MESSAGE_LEVEL_LABELS},
        "floors": {"min_messages": 30, "min_distinct_authors": 10},
        "truncated_thread_counts": {v: {"2024Q1": 0, "2024Q2": 1} for v in VENUES},
        "cost_summary": {
            "status": "completed",
            "calls_made": 500,
            "cache_hits": 100,
            "input_tokens_used": 12345,
            "output_tokens_used": 6789,
            "estimated_cost_usd": 1.23,
            "elapsed_seconds": 42.0,
            "mean_latency_seconds_per_call": 0.08,
            "classifier_version": "1.0.0",
            "question_set_version": "1",
            "model_id_pinned": "jev-1.13.0",
        },
        "cost_ledger": {
            "runs_recorded": 3,
            "latest": {"estimated_cost_usd": 1.23},
            "cumulative_from_cache": {
                "distinct_messages_ever_classified": 9000,
                "input_tokens_used": 99999,
                "output_tokens_used": 55555,
                "estimated_cost_usd": 9.99,
            },
            "cumulative_from_ledger_runs": {
                "calls_made": 1500,
                "cache_hits": 300,
                "elapsed_seconds": 120.0,
                "estimated_cost_usd": 10.0,
            },
        },
        "partial_run": {
            "status": "completed",
            "messages_sampled": 100,
            "messages_classified": 100,
            "messages_truncated": 2,
            "messages_skipped": 1,
            "skip_reasons": {"max_tokens_exceeded": 1},
            "coverage_by_stratum": [
                {
                    "venue": "mailing_list",
                    "quarter": "2024Q1",
                    "messages_sampled": 50,
                    "messages_classified": 50,
                    "coverage": 1.0,
                }
            ],
        },
        "venue_totals": {
            v: {"messages_classified": 100, "threads_sampled": 20, "threads_population": 200}
            for v in VENUES
        },
        "trend_summary": {
            v: {
                "windows": {"2017_2019": _cell(), "2023_2025": _cell()},
                "headline_cutoff": "0.5",
                "ci_overlap_by_label": {label: True for label in MESSAGE_LEVEL_LABELS},
            }
            for v in VENUES
        },
        "cells_by_quarter": {
            v: {"2024Q1": _cell(), "2024Q2": _cell(insufficient=True)} for v in VENUES
        },
        "cells_by_year": {v: {"2024": _cell()} for v in VENUES},
        "thread_derive_cutoffs": list(THREAD_CUTOFFS),
        "thread_derive_headline_cutoff": "0.5",
        "newcomer_n": 3,
        "thread_metrics_by_year": {
            v: {"2024": {ck: _thread_metrics_entry() for ck in THREAD_CUTOFFS}} for v in VENUES
        },
        "newcomer_by_year": {v: {"2024": _newcomer_entry()} for v in VENUES},
        "thread_trend_summary": {
            v: {
                "windows": {
                    "2017_2019": {ck: _thread_metrics_entry() for ck in THREAD_CUTOFFS},
                    "2023_2025": {ck: _thread_metrics_entry() for ck in THREAD_CUTOFFS},
                }
            }
            for v in VENUES
        },
        "newcomer_trend_summary": {
            v: {"windows": {"2017_2019": _newcomer_entry(), "2023_2025": _newcomer_entry()}}
            for v in VENUES
        },
        "tone_mix_cutoffs": list(THREAD_CUTOFFS),
        "tone_mix_headline_cutoff": "0.5",
        "tone_mix_by_quarter": {
            v: {
                "2024Q1": {ck: _tone_mix_cell() for ck in THREAD_CUTOFFS},
                "2024Q2": {ck: _tone_mix_cell(insufficient=True) for ck in THREAD_CUTOFFS},
            }
            for v in VENUES
        },
        "tone_mix_by_year": {
            v: {"2024": {ck: _tone_mix_cell() for ck in THREAD_CUTOFFS}} for v in VENUES
        },
    }
    aggregates.update(overrides)
    return aggregates


# --- Allowlist behavior ----------------------------------------------------


def test_sanitize_keeps_only_allowlisted_top_keys():
    aggregates = _sample_aggregates()
    sanitized = sanitize_aggregates(aggregates)

    assert "cost_summary" not in sanitized
    assert "cost_ledger" not in sanitized
    assert sanitized["classifier_version"] == "1.0.0"
    assert sanitized["question_set_version"] == "1"
    assert sanitized["model_id_pinned"] == "jev-1.13.0"
    # Only total-USD cost figures survive (issue #118: "drop cost ledger
    # internals except total USD") -- calls made, cache hits, tokens,
    # latency and status are gone.
    assert sanitized["cost_total_usd"] == {"latest_run_usd": 1.23, "cumulative_usd": 10.0}

    assert sanitized["seed"] == 42
    assert sanitized["k"] == 30
    assert sanitized["quarters"] == ["2024Q1", "2024Q2"]
    assert sanitized["venues"] == list(VENUES)
    assert sanitized["floors"] == {"min_messages": 30, "min_distinct_authors": 10}
    assert sanitized["headline_cutoff"] == "0.5"


def test_sanitize_drops_unknown_top_level_keys():
    """A future runner.py field is dropped by default (allowlist, not a
    denylist) -- the safe failure direction for a new field this sanitizer
    hasn't been taught about yet."""
    aggregates = _sample_aggregates()
    aggregates["some_future_internal_field"] = {"note": "a harmless future field"}
    sanitized = sanitize_aggregates(aggregates)
    assert "some_future_internal_field" not in sanitized


def test_sanitize_round_trips_message_level_cell_data():
    aggregates = _sample_aggregates()
    sanitized = sanitize_aggregates(aggregates)
    cell = sanitized["cells_by_year"]["mailing_list"]["2024"]
    assert cell["messages_classified"] == 50
    assert cell["distinct_authors"] == 15
    entry = cell["cutoff_rates_per_1000_messages"]["hostility"]["0.5"]
    assert entry == {"per_1000": 5.0, "ci95": [4.0, 6.0]}


def test_sanitize_is_json_serializable():
    aggregates = _sample_aggregates()
    sanitized = sanitize_aggregates(aggregates)
    json.dumps(sanitized)  # must not raise


def test_sanitize_missing_required_key_raises():
    aggregates = _sample_aggregates()
    del aggregates["trend_summary"]
    with pytest.raises(SanitizeError, match="trend_summary"):
        sanitize_aggregates(aggregates)


# --- Hard-fail leak detection ----------------------------------------------


def test_sanitize_hard_fails_on_email_in_frame_definition():
    aggregates = _sample_aggregates()
    aggregates["frame_definition"]["mailing_list"] = "thread started by jane@example.com"
    with pytest.raises(SanitizeError, match="email"):
        sanitize_aggregates(aggregates)


def test_sanitize_hard_fails_on_message_id_in_scope_note():
    aggregates = _sample_aggregates()
    aggregates["scope_note"] = "see <abc123@mail.gmail.com> for details"
    with pytest.raises(SanitizeError, match="Message-ID"):
        sanitize_aggregates(aggregates)


def test_sanitize_hard_fails_on_hashlike_dataset_name():
    aggregates = _sample_aggregates()
    label = next(iter(STRONG_GATING_LABELS))
    aggregates["sensitivity_thresholds"][label]["dataset_name"] = (
        "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"  # 32 hex chars -- author_key shaped
    )
    with pytest.raises(SanitizeError, match="hash"):
        sanitize_aggregates(aggregates)


def test_sanitize_hard_fails_on_jira_comment_reference():
    aggregates = _sample_aggregates()
    aggregates["scope_note"] = "flagged by CASSANDRA-12345 comment 7"
    with pytest.raises(SanitizeError, match="JIRA comment"):
        sanitize_aggregates(aggregates)


def test_sanitize_does_not_false_positive_on_venue_at_sign():
    """`frame_definition` prose legitimately contains "dev@" (a mailing
    list name, not an email address) -- must not trip the email check."""
    aggregates = _sample_aggregates()
    aggregates["frame_definition"]["mailing_list"] = "thread = a dev@ Pony Mail thread"
    sanitize_aggregates(aggregates)  # must not raise


# --- §5.1/§5.2 floor invariant ----------------------------------------------


def test_sanitize_hard_fails_when_insufficient_cell_carries_a_value():
    aggregates = _sample_aggregates()
    bad_cell = _cell(insufficient=True)
    label = next(iter(MESSAGE_LEVEL_LABELS))
    bad_cell["cutoff_rates_per_1000_messages"][label]["0.5"] = _rate_entry()
    aggregates["cells_by_year"]["mailing_list"]["2024"] = bad_cell
    with pytest.raises(SanitizeError, match="floor"):
        sanitize_aggregates(aggregates)


def test_sanitize_hard_fails_when_insufficient_thread_rate_carries_a_value():
    aggregates = _sample_aggregates()
    entry = _thread_metrics_entry()
    entry["escalation_rate"] = {
        "qualifying_threads": 3,
        "distinct_participants": 2,
        "insufficient_data": True,
        "rate": 0.5,  # should be None when insufficient_data is True
        "ci95": [0.4, 0.6],
    }
    aggregates["thread_metrics_by_year"]["mailing_list"]["2024"]["0.5"] = entry
    with pytest.raises(SanitizeError, match="floor"):
        sanitize_aggregates(aggregates)


def test_sanitize_hard_fails_when_insufficient_newcomer_row_carries_a_value():
    aggregates = _sample_aggregates()
    entry = _newcomer_entry(insufficient=True)
    entry["constructive_response_rate"] = 0.9
    aggregates["newcomer_by_year"]["mailing_list"]["2024"] = entry
    with pytest.raises(SanitizeError, match="floor"):
        sanitize_aggregates(aggregates)


def test_sanitize_allows_insufficient_data_cell_with_no_values():
    """The normal, expected shape (issue #114's own pile-on rarity, for
    instance): insufficient_data True with every rate/ci95 None must sanitize
    cleanly, never raise."""
    aggregates = _sample_aggregates()  # cells_by_quarter's 2024Q2 is insufficient by design
    sanitized = sanitize_aggregates(aggregates)
    cell = sanitized["cells_by_quarter"]["mailing_list"]["2024Q2"]
    assert cell["insufficient_data"] is True
    for by_cutoff in cell["cutoff_rates_per_1000_messages"].values():
        assert all(v is None for v in by_cutoff.values())


# --- Tone mix (issue #153) --------------------------------------------------


def test_sanitize_round_trips_tone_mix_tiers():
    aggregates = _sample_aggregates()
    sanitized = sanitize_aggregates(aggregates)
    cell = sanitized["tone_mix_by_year"]["mailing_list"]["2024"]["0.5"]
    assert cell["insufficient_data"] is False
    tier = cell["tiers"]["0"]
    assert tier["name"] == "Neutral"
    assert tier["share"] == 0.50
    assert tier["ci95"] == [0.48, 0.52]
    # Every tier's share sums to 1.0 -- the stacking invariant.
    assert sum(entry["share"] for entry in cell["tiers"].values()) == pytest.approx(1.0)


def test_sanitize_tone_mix_insufficient_cell_has_no_values():
    aggregates = _sample_aggregates()
    sanitized = sanitize_aggregates(aggregates)
    cell = sanitized["tone_mix_by_quarter"]["mailing_list"]["2024Q2"]["0.5"]
    assert cell["insufficient_data"] is True
    assert all(v is None for v in cell["tiers"].values())


def test_sanitize_hard_fails_when_insufficient_tone_mix_tier_carries_a_value():
    aggregates = _sample_aggregates()
    bad_cell = _tone_mix_cell(insufficient=True)
    bad_cell["tiers"]["0"] = {"name": "Neutral", "share": 0.5, "ci95": [0.4, 0.6]}
    aggregates["tone_mix_by_year"]["mailing_list"]["2024"]["0.5"] = bad_cell
    with pytest.raises(SanitizeError, match="floor"):
        sanitize_aggregates(aggregates)


def test_sanitize_missing_tone_mix_key_raises():
    aggregates = _sample_aggregates()
    del aggregates["tone_mix_by_quarter"]
    with pytest.raises(SanitizeError, match="tone_mix_by_quarter"):
        sanitize_aggregates(aggregates)


# --- Snapshot writer ---------------------------------------------------------


def test_write_conversation_patterns_snapshot_writes_dated_file(tmp_path: Path):
    aggregates = _sample_aggregates()
    data_dir = tmp_path / "data"
    out_path = write_conversation_patterns_snapshot(aggregates, data_dir)

    assert out_path == data_dir / "snapshots" / "conversation_patterns" / "2026-09-28.json"
    assert out_path.is_file()
    written = json.loads(out_path.read_text())
    assert written["classifier_version"] == "1.0.0"


def test_write_conversation_patterns_snapshot_respects_run_date_override(tmp_path: Path):
    from datetime import date

    aggregates = _sample_aggregates()
    out_path = write_conversation_patterns_snapshot(
        aggregates, tmp_path / "data", run_date=date(2026, 1, 1)
    )
    assert out_path.name == "2026-01-01.json"


def test_write_conversation_patterns_snapshot_writes_nothing_on_sanitize_error(tmp_path: Path):
    aggregates = _sample_aggregates()
    aggregates["scope_note"] = "contact admin@example.com for details"
    data_dir = tmp_path / "data"
    with pytest.raises(SanitizeError):
        write_conversation_patterns_snapshot(aggregates, data_dir)
    assert not (data_dir / "snapshots" / "conversation_patterns").exists()
