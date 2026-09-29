"""Tests for project_health.private_run.sensitivity (issue #110).

`test_real_public_v1_md_yields_the_three_d23_strong_labels` is the one
integration-style check that reads the actual in-repo `docs/benchmark/
public-v1.md` (public, already-committed markdown -- no privacy concern);
every other test uses a small synthetic markdown fixture built inline.
"""

from __future__ import annotations

from pathlib import Path

from project_health.private_run.sensitivity import (
    DEFAULT_PUBLIC_BENCHMARK_PATH,
    STRONG_GATING_LABELS,
    load_gating_thresholds,
    parse_gating_thresholds,
)

_HEADER = (
    "| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | "
    "Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | Notes |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
)


def _row(label: str, strength: str, gate_role: str, threshold: float, f1: float) -> str:
    return (
        f"| `{label}` | {strength} | {gate_role} | {threshold} | 0.500 [0.400, 0.600] | "
        f"0.500 [0.400, 0.600] | {f1} [0.400, 0.600] | 10/100 = 0.100 | 100/1000 = 0.100 | note |\n"
    )


class TestParseGatingThresholds:
    def test_picks_the_threshold_from_the_only_qualifying_row(self):
        markdown = _HEADER + _row("hostility", "strong", "gating", 0.4, 0.5)
        assert parse_gating_thresholds(markdown) == {"hostility": 0.4}

    def test_ignores_partial_strength(self):
        markdown = _HEADER + _row("dismissiveness", "partial", "informative", 0.2, 0.5)
        assert parse_gating_thresholds(markdown) == {}

    def test_ignores_strong_but_informative_role(self):
        # e.g. Wikipedia Personal Attacks: strong mapping, but alpha below
        # the usable floor, so D23 keeps it informative-only, never gating.
        markdown = _HEADER + _row("personal_attack", "strong", "informative", 0.45, 0.862)
        assert parse_gating_thresholds(markdown) == {}

    def test_ignores_labels_outside_strong_gating_labels(self):
        markdown = _HEADER + _row("acknowledgment", "strong", "gating", 0.1, 0.5)
        assert parse_gating_thresholds(markdown) == {}

    def test_picks_the_higher_f1_row_when_two_datasets_qualify(self):
        markdown = (
            _HEADER
            + _row("sarcasm", "strong", "gating", 0.5, 0.286)
            + _row("sarcasm", "strong", "gating", 0.6, 0.296)
        )
        assert parse_gating_thresholds(markdown) == {"sarcasm": 0.6}

    def test_ties_break_toward_the_lower_threshold(self):
        markdown = (
            _HEADER
            + _row("sarcasm", "strong", "gating", 0.7, 0.3)
            + _row("sarcasm", "strong", "gating", 0.3, 0.3)
        )
        assert parse_gating_thresholds(markdown) == {"sarcasm": 0.3}

    def test_multiple_labels_resolved_independently(self):
        markdown = (
            _HEADER
            + _row("personal_attack", "strong", "gating", 0.15, 0.479)
            + _row("hostility", "strong", "gating", 0.4, 0.507)
            + _row("sarcasm", "strong", "gating", 0.6, 0.296)
        )
        assert parse_gating_thresholds(markdown) == {
            "personal_attack": 0.15,
            "hostility": 0.4,
            "sarcasm": 0.6,
        }

    def test_non_table_lines_are_ignored(self):
        markdown = "# heading\n\nSome prose about `hostility` that is not a table row.\n" + _HEADER
        assert parse_gating_thresholds(markdown) == {}

    def test_strong_gating_labels_is_exactly_d23s_three(self):
        assert STRONG_GATING_LABELS == frozenset({"personal_attack", "hostility", "sarcasm"})


class TestLoadGatingThresholds:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path):
        assert load_gating_thresholds(tmp_path / "nope.md") == {}

    def test_loads_from_a_real_file(self, tmp_path: Path):
        path = tmp_path / "public-v1.md"
        path.write_text(_HEADER + _row("hostility", "strong", "gating", 0.4, 0.5), encoding="utf-8")
        assert load_gating_thresholds(path) == {"hostility": 0.4}

    def test_default_path_points_at_the_real_docs_file(self):
        assert DEFAULT_PUBLIC_BENCHMARK_PATH.name == "public-v1.md"

    def test_real_public_v1_md_yields_the_three_d23_strong_labels(self):
        if not DEFAULT_PUBLIC_BENCHMARK_PATH.is_file():
            return  # nothing to check if the benchmark hasn't been generated yet
        thresholds = load_gating_thresholds()
        assert set(thresholds) == STRONG_GATING_LABELS
        for threshold in thresholds.values():
            assert 0.0 <= threshold <= 1.0
