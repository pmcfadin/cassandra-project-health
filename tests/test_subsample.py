"""Tests for `project_health.classify.subsample` (issue #90; DECISIONS.md D23).

Fully offline and self-contained: every test builds its own tiny synthetic
v0-shaped corpus JSONL rather than reading a real corpus file. All text here
is synthetic, never real Cassandra content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_health.classify.sample import load_enrichment_filters
from project_health.classify.subsample import (
    DEFAULT_ENRICHMENT_FILTERS_PATH,
    GAP_LABELS_WITH_ENRICHMENT_SIGNAL,
    GAP_LABELS_WITHOUT_ENRICHMENT_SIGNAL,
    PilotSubsampleResult,
    read_corpus_rows,
    run_pilot_subsample,
    select_subsample,
    shuffle_presentation_order,
)
from project_health.label.label_set import GAP_LABEL_IDS
from project_health.label.store import CorpusError

FILTER_MAP = load_enrichment_filters(DEFAULT_ENRICHMENT_FILTERS_PATH)


def _row(item_id: str, stratum: str, text: str = "synthetic message text here", **extra) -> dict:
    row = {
        "id": item_id,
        "stratum": stratum,
        "source": "mailing_list",
        "archive_url": f"https://example.invalid/{item_id}",
        "year": 2020,
        "text": text,
        "parent_text": None,
        "checksum": f"c-{item_id}",
    }
    row.update(extra)
    return row


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


class TestGapLabelPartition:
    def test_only_gatekeeping_and_status_authority_have_v0_prefilter_signal(self):
        assert set(GAP_LABELS_WITH_ENRICHMENT_SIGNAL) == {
            "gatekeeping",
            "status_authority_invocation",
        }

    def test_the_other_four_gap_labels_have_no_prefilter_signal(self):
        assert set(GAP_LABELS_WITHOUT_ENRICHMENT_SIGNAL) == {
            "evidence_based_argument",
            "compromise_offer",
            "acknowledgment",
            "resolution_marker",
        }

    def test_the_two_groups_partition_all_gap_labels(self):
        assert set(GAP_LABELS_WITH_ENRICHMENT_SIGNAL) | set(
            GAP_LABELS_WITHOUT_ENRICHMENT_SIGNAL
        ) == set(GAP_LABEL_IDS)


class TestReadCorpusRows:
    def test_reads_rows_preserving_every_field(self, tmp_path):
        rows = [_row("i1", "prevalence"), _row("i2", "enrichment")]
        path = _write_jsonl(tmp_path / "v0.jsonl", rows)
        loaded = read_corpus_rows(path)
        assert loaded == rows  # byte-identical field preservation, including "year"

    def test_empty_corpus_raises(self, tmp_path):
        path = tmp_path / "v0.jsonl"
        path.write_text("", encoding="utf-8")
        with pytest.raises(CorpusError):
            read_corpus_rows(path)

    def test_duplicate_id_raises(self, tmp_path):
        rows = [_row("i1", "prevalence"), _row("i1", "enrichment")]
        path = _write_jsonl(tmp_path / "v0.jsonl", rows)
        with pytest.raises(CorpusError):
            read_corpus_rows(path)

    def test_missing_field_raises(self, tmp_path):
        path = tmp_path / "v0.jsonl"
        path.write_text(json.dumps({"id": "i1", "stratum": "prevalence"}) + "\n", encoding="utf-8")
        with pytest.raises(CorpusError):
            read_corpus_rows(path)


class TestSelectSubsample:
    def test_preserves_prevalence_enrichment_proportions_approximately(self):
        prevalence = [_row(f"p{i}", "prevalence") for i in range(15)]
        enrichment = [_row(f"e{i}", "enrichment") for i in range(10)]
        rows = prevalence + enrichment
        selection = select_subsample(seed=1, rows=rows, enrichment_filter_map=FILTER_MAP, size=10)
        # v0 is 60% prevalence / 40% enrichment (15:10) -- v1 of 10 should be
        # 6 prevalence / 4 enrichment (largest-remainder allocation).
        assert selection.stats["selected_counts"]["prevalence"] == 6
        assert selection.stats["selected_counts"]["enrichment"] == 4
        assert selection.stats["achieved_size"] == 10

    def test_never_exceeds_available_rows(self):
        rows = [_row("p1", "prevalence"), _row("e1", "enrichment")]
        selection = select_subsample(seed=1, rows=rows, enrichment_filter_map=FILTER_MAP, size=80)
        assert selection.stats["achieved_size"] == 2
        assert set(selection.selected_ids) == {"p1", "e1"}

    def test_zero_or_negative_size_raises(self):
        rows = [_row("p1", "prevalence")]
        with pytest.raises(ValueError):
            select_subsample(seed=1, rows=rows, enrichment_filter_map=FILTER_MAP, size=0)

    def test_gap_hit_enrichment_items_are_prioritized_over_non_gap_ones(self):
        # 5 enrichment items hit gatekeeping (a gap label with prefilter
        # signal), 5 hit only personal_attack (not a gap label at all). With
        # a small enrichment budget, the gap-hit items must be favored.
        gap_hits = [
            _row(f"gk{i}", "enrichment", text="this was already decided, not up for debate")
            for i in range(5)
        ]
        non_gap_hits = [
            _row(f"pa{i}", "enrichment", text="you clearly have no idea how this works")
            for i in range(5)
        ]
        prevalence = [_row(f"p{i}", "prevalence") for i in range(5)]
        rows = prevalence + gap_hits + non_gap_hits
        # size=6: proportional alloc gives prevalence=2, enrichment=4 (5:10 ratio).
        selection = select_subsample(seed=1, rows=rows, enrichment_filter_map=FILTER_MAP, size=6)
        selected = set(selection.selected_ids)
        selected_gap = selected & {r["id"] for r in gap_hits}
        selected_non_gap = selected & {r["id"] for r in non_gap_hits}
        assert len(selected_gap) == 4  # all of the enrichment budget goes to gap hits first
        assert len(selected_non_gap) == 0
        assert selection.stats["gap_label_enrichment_signal"]["selected"]["gatekeeping"] == 4

    def test_manifest_stats_report_candidates_and_selected_counts_per_gap_label(self):
        gap_hits = [
            _row(f"gk{i}", "enrichment", text="this was already decided, not up for debate")
            for i in range(3)
        ]
        rows = [_row("p1", "prevalence")] + gap_hits
        selection = select_subsample(seed=1, rows=rows, enrichment_filter_map=FILTER_MAP, size=4)
        signal = selection.stats["gap_label_enrichment_signal"]
        assert signal["candidates_in_source"]["gatekeeping"] == 3
        assert signal["labels_with_v0_prefilter_signal"] == [
            "gatekeeping",
            "status_authority_invocation",
        ]
        assert set(signal["labels_with_no_v0_prefilter_signal"]) == {
            "evidence_based_argument",
            "compromise_offer",
            "acknowledgment",
            "resolution_marker",
        }

    def test_deterministic_for_the_same_seed(self):
        rows = [_row(f"p{i}", "prevalence") for i in range(20)] + [
            _row(f"e{i}", "enrichment") for i in range(20)
        ]
        first = select_subsample(seed=7, rows=rows, enrichment_filter_map=FILTER_MAP, size=10)
        second = select_subsample(seed=7, rows=rows, enrichment_filter_map=FILTER_MAP, size=10)
        assert first.selected_ids == second.selected_ids


class TestShufflePresentationOrder:
    def test_deterministic_and_a_permutation_of_the_input(self):
        rows = [_row(f"i{i}", "prevalence") for i in range(10)]
        shuffled1 = shuffle_presentation_order(1, rows)
        shuffled2 = shuffle_presentation_order(1, rows)
        assert [r["id"] for r in shuffled1] == [r["id"] for r in shuffled2]
        assert {r["id"] for r in shuffled1} == {r["id"] for r in rows}

    def test_different_seeds_can_give_different_orders(self):
        rows = [_row(f"i{i}", "prevalence") for i in range(20)]
        order_a = [r["id"] for r in shuffle_presentation_order(1, rows)]
        order_b = [r["id"] for r in shuffle_presentation_order(2, rows)]
        assert order_a != order_b


class TestRunPilotSubsampleEndToEnd:
    def _write_source(self, tmp_path: Path) -> Path:
        prevalence = [_row(f"p{i}", "prevalence") for i in range(6)]
        enrichment = [
            _row(f"e{i}", "enrichment", text="this was already decided, not up for debate")
            for i in range(4)
        ]
        return _write_jsonl(tmp_path / "v0.jsonl", prevalence + enrichment)

    def test_writes_a_v1_corpus_and_counts_only_manifest(self, tmp_path):
        source = self._write_source(tmp_path)
        corpus_out = tmp_path / "out" / "v1.jsonl"
        manifest_out = tmp_path / "out" / "v1_manifest.json"

        result = run_pilot_subsample(
            source_corpus_path=source,
            seed=90,
            size=5,
            corpus_output_path=corpus_out,
            manifest_output_path=manifest_out,
        )

        assert isinstance(result, PilotSubsampleResult)
        assert corpus_out.is_file()
        assert manifest_out.is_file()
        assert len(result.row_ids) == 5

        written_rows = [json.loads(line) for line in corpus_out.read_text().splitlines()]
        assert len(written_rows) == 5
        # Every field from v0 is preserved byte-for-byte.
        source_rows_by_id = {r["id"]: r for r in read_corpus_rows(source)}
        for row in written_rows:
            assert row == source_rows_by_id[row["id"]]

        manifest = json.loads(manifest_out.read_text())
        assert manifest["corpus_version"] == "v1"
        assert manifest["source_corpus_version"] == "v0"
        assert manifest["requested_size"] == 5
        assert manifest["achieved_size"] == 5
        assert manifest["seed"] == 90
        assert "corpus_checksum_sha256" in manifest
        assert "source_corpus_checksum_sha256" in manifest
        # Counts-only: no message text anywhere in the manifest, and the
        # top-level schema is exactly the documented counts/metadata fields
        # (no per-item id list was ever added).
        manifest_blob = json.dumps(manifest)
        for row in written_rows:
            assert row["text"] not in manifest_blob
        assert set(manifest) == {
            "corpus_version",
            "source_corpus_version",
            "source_corpus_path",
            "source_corpus_checksum_sha256",
            "seed",
            "generated_at",
            "requested_size",
            "achieved_size",
            "counts_by_stratum_source",
            "selection",
            "corpus_checksum_sha256",
        }

    def test_deterministic_across_runs(self, tmp_path):
        source = self._write_source(tmp_path)
        r1 = run_pilot_subsample(
            source_corpus_path=source,
            seed=90,
            size=5,
            corpus_output_path=tmp_path / "a" / "v1.jsonl",
            manifest_output_path=tmp_path / "a" / "v1_manifest.json",
        )
        r2 = run_pilot_subsample(
            source_corpus_path=source,
            seed=90,
            size=5,
            corpus_output_path=tmp_path / "b" / "v1.jsonl",
            manifest_output_path=tmp_path / "b" / "v1_manifest.json",
        )
        assert r1.row_ids == r2.row_ids
        assert r1.manifest["corpus_checksum_sha256"] == r2.manifest["corpus_checksum_sha256"]

    def test_empty_source_raises(self, tmp_path):
        source = tmp_path / "v0.jsonl"
        source.write_text("", encoding="utf-8")
        with pytest.raises(CorpusError):
            run_pilot_subsample(
                source_corpus_path=source,
                seed=90,
                size=5,
                corpus_output_path=tmp_path / "out" / "v1.jsonl",
                manifest_output_path=tmp_path / "out" / "v1_manifest.json",
            )
