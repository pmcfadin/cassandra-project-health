"""Tests for the "tone over time" chart context (issue #153:
`project_health.site.conversation_patterns_page`'s `_tone_mix_*` helpers
and `build_conversation_patterns_context`'s `tone_mix` key).
"""

from __future__ import annotations

import json
from pathlib import Path

from project_health.private_run.publish import sanitize_aggregates
from project_health.site.conversation_patterns_page import (
    build_conversation_patterns_context,
    _tone_mix_chart_spec,
    _tone_mix_context,
    _tone_mix_rows,
    _tone_mix_sparkline_spec,
)
from tests.test_private_run_publish import VENUES, _sample_aggregates


def _snapshot(**overrides) -> dict:
    return sanitize_aggregates(_sample_aggregates(**overrides))


class TestToneMixRows:
    def test_sufficient_quarter_produces_one_row_per_tier_per_cutoff(self):
        snapshot = _snapshot()
        rows, insufficient = _tone_mix_rows(snapshot, "mailing_list")
        # "2024Q1" is sufficient (base fixture), "2024Q2" is insufficient.
        q1_rows = [r for r in rows if r["quarter"] == "2024Q1"]
        assert q1_rows  # at least one row per tier per cutoff
        assert insufficient == ["2024Q2"]
        # Every row carries both share and per-1000 forms, plus n.
        row = q1_rows[0]
        assert row["per_1000"] == row["share"] * 1000.0
        assert "messages" in row and "authors" in row

    def test_rows_only_for_requested_venue(self):
        snapshot = _snapshot()
        rows, _ = _tone_mix_rows(snapshot, "mailing_list")
        assert all(r["venue"] == "mailing_list" for r in rows)

    def test_unknown_venue_returns_empty(self):
        snapshot = _snapshot()
        rows, insufficient = _tone_mix_rows(snapshot, "not_a_real_venue")
        assert rows == []
        assert insufficient == []


class TestToneMixChartSpec:
    def test_none_when_no_rows(self):
        assert _tone_mix_chart_spec([]) is None

    def test_spec_stacks_bottom_to_top_with_named_legend(self):
        snapshot = _snapshot()
        rows, _ = _tone_mix_rows(snapshot, "mailing_list")
        spec_json = _tone_mix_chart_spec(rows)
        assert spec_json is not None
        spec = json.loads(spec_json)
        assert spec["mark"]["type"] == "area"
        assert spec["encoding"]["order"]["field"] == "tier_order"
        assert spec["encoding"]["color"]["field"] == "tier_name"
        assert spec["encoding"]["color"]["sort"][0] == "Closing/positive"
        assert spec["encoding"]["color"]["sort"][-1] == "Attack"
        assert len(spec["encoding"]["color"]["scale"]["range"]) == 7
        tooltip_fields = [t["field"] for t in spec["encoding"]["tooltip"]]
        assert "share" in tooltip_fields
        assert "ci_lo" in tooltip_fields
        assert "ci_hi" in tooltip_fields
        assert "messages" in tooltip_fields
        # Both cutoffs are shipped in the data so JS can filter client-side.
        cutoffs_present = {row["cutoff"] for row in spec["data"]["values"]}
        assert cutoffs_present == {"0.5", "0.7"}


class TestToneMixSparklineSpec:
    def test_none_when_no_rows(self):
        assert _tone_mix_sparkline_spec([], "0.5") is None

    def test_sparkline_has_no_axis_or_legend(self):
        snapshot = _snapshot()
        rows, _ = _tone_mix_rows(snapshot, "mailing_list")
        spec_json = _tone_mix_sparkline_spec(rows, "0.5")
        assert spec_json is not None
        spec = json.loads(spec_json)
        assert spec["encoding"]["x"]["axis"] is None
        assert spec["encoding"]["y"]["axis"] is None
        assert spec["encoding"]["color"]["legend"] is None
        # Only the headline-cutoff rows are included.
        assert all(row["cutoff"] == "0.5" for row in spec["data"]["values"])


class TestToneMixContext:
    def test_context_has_one_chart_per_venue_and_sparklines(self):
        snapshot = _snapshot()
        venue_meta = [{"id": v, "label": v} for v in VENUES]
        context = _tone_mix_context(snapshot, list(VENUES), venue_meta)
        assert context is not None
        assert {c["venue"] for c in context["charts"]} == set(VENUES)
        assert {s["venue"] for s in context["sparklines"]} == set(VENUES)
        assert context["cutoffs"] == ["0.5", "0.7"]
        assert context["default_cutoff"] == "0.5"
        # 2024Q2 is insufficient_data in the base fixture for every venue.
        assert any(
            "2024Q2" in note["quarters"] for note in context["insufficient_notes"]
        )

    def test_none_when_no_data_clears_the_floor(self):
        snapshot = _snapshot(
            tone_mix_by_quarter={v: {} for v in VENUES},
        )
        venue_meta = [{"id": v, "label": v} for v in VENUES]
        assert _tone_mix_context(snapshot, list(VENUES), venue_meta) is None

    def test_wired_into_build_conversation_patterns_context(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        snapshot_dir = data_dir / "snapshots" / "conversation_patterns"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "2026-09-28.json").write_text(json.dumps(_snapshot()))

        context = build_conversation_patterns_context(data_dir)
        assert context["available"] is True
        assert context["tone_mix"] is not None
        assert context["tone_mix"]["charts"]
