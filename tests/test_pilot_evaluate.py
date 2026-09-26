"""Tests for `project_health.pilot.evaluate` (issue #47).

Fully offline: no network, no model call -- classification records are
constructed directly (or written as synthetic JSONL) rather than produced by
a live Jev call. Covers ground-truth aggregation, the deterministic §6.4
gate rule, per-label evaluation math, tone agreement, rater time, and both
the one-rater ("no agreement statistic exists") and two-rater
(Krippendorff's alpha) cases end to end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_health.classify.classifier import ClassificationRecord, Label, ToneIntensity, Usage
from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.label.store import CorpusItem
from project_health.pilot import stats
from project_health.pilot.evaluate import (
    aggregate_human_mark,
    aggregate_human_tone,
    classify_gate,
    evaluate_agreement,
    evaluate_label,
    evaluate_pilot,
    evaluate_tone,
    load_classification_records,
    load_jev_summary,
    load_rater_labels,
)

FIXED_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _record(
    message_id: str, probability: float, tone_probs: dict[str, float] | None = None
) -> ClassificationRecord:
    labels = {label_id: Label(probability=probability) for label_id in MESSAGE_LEVEL_LABELS}
    tone = None
    if tone_probs is not None:
        tone = ToneIntensity(score=1.0, confidence=0.7, probabilities=tone_probs)
    return ClassificationRecord(
        message_id=message_id,
        thread_id=message_id,
        source="mailing_list",
        classifier_version="1.0.0",
        question_set_version="1",
        model_id="jev-1.13.0",
        input_hash="a" * 64,
        classified_at=FIXED_NOW,
        usage=Usage(input_tokens=10, output_tokens=5),
        labels=labels,
        tone_intensity=tone,
    )


def _corpus_item(item_id: str, stratum: str) -> CorpusItem:
    return CorpusItem(
        id=item_id,
        stratum=stratum,
        source="mailing_list",
        archive_url="https://example.invalid/x",
        text="synthetic text",
        parent_text=None,
        checksum="c",
    )


def _label_record(
    item_id: str, rater: str, mark: str, tone: int = 2, seconds: float = 30.0
) -> dict:
    return {
        "item_id": item_id,
        "rater": rater,
        "labels": {label_id: mark for label_id in MESSAGE_LEVEL_LABELS},
        "tone": tone,
        "note": None,
        "seconds": seconds,
        "saved_at": "2026-01-01T00:00:00Z",
        "corpus_checksum": "c",
        "label_set_version": 1,
        "question_set_version": 1,
    }


# --- aggregate_human_mark ----------------------------------------------------------------


class TestAggregateHumanMark:
    @pytest.mark.parametrize(
        "marks,expected",
        [
            (["yes"], "yes"),
            (["no", "no"], "no"),
            (["yes", "no"], "unsure"),  # real tie/disagreement -> excluded
            (["unsure"], "unsure"),
            (["unsure", "yes"], "yes"),  # unsure dropped, remainder agrees
            (["unsure", "yes", "no"], "unsure"),  # remainder disagrees
            ([], "unsure"),
        ],
    )
    def test_cases(self, marks, expected):
        assert aggregate_human_mark(marks) == expected


class TestAggregateHumanTone:
    def test_single_rater(self):
        assert aggregate_human_tone([2]) == 2

    def test_rounds_mean(self):
        assert aggregate_human_tone([1, 3]) == 2

    def test_bankers_rounding_on_exact_half(self):
        # round(2.5) == 2 in Python (banker's rounding) -- documented behavior.
        assert aggregate_human_tone([2, 3]) == 2


# --- classify_gate -------------------------------------------------------------------------


def _threshold_metrics(precision: float, recall: float, f1: float) -> stats.ThresholdMetrics:
    confusion = stats.Confusion(tp=0, fp=0, tn=0, fn=0)
    return stats.ThresholdMetrics(
        threshold=0.5,
        confusion=confusion,
        precision=precision,
        recall=recall,
        f1=f1,
        precision_ci=(precision, precision),
        recall_ci=(recall, recall),
        f1_ci=(f1, f1),
    )


class TestClassifyGate:
    def test_likely_pass_when_every_floor_cleared(self):
        gate = classify_gate("reputational_harm", _threshold_metrics(0.9, 0.7, 0.75), n_positives=2)
        assert gate["status"] == "likely_pass"

    def test_drop_or_revise_taxonomy_with_catastrophic_shortfall_and_enough_positives(self):
        # reputational_harm floors: P>=0.85, R>=0.60, F1>=0.70. Precision 0.5 is
        # 0.35 below its floor (> 0.20 CATASTROPHIC_MARGIN), with 15 positives.
        gate = classify_gate("reputational_harm", _threshold_metrics(0.5, 0.7, 0.6), n_positives=15)
        assert gate["status"] == "drop_or_revise_taxonomy"

    def test_revise_or_gather_more_data_with_catastrophic_shortfall_but_too_few_positives(self):
        gate = classify_gate("reputational_harm", _threshold_metrics(0.5, 0.7, 0.6), n_positives=3)
        assert gate["status"] == "revise_or_gather_more_data"

    def test_revise_or_gather_more_data_with_small_shortfall(self):
        # Precision 0.80 is only 0.05 below the 0.85 floor -- not catastrophic.
        gate = classify_gate(
            "reputational_harm", _threshold_metrics(0.80, 0.65, 0.71), n_positives=50
        )
        assert gate["status"] == "revise_or_gather_more_data"


# --- evaluate_label ------------------------------------------------------------------------


class TestEvaluateLabel:
    def test_perfect_agreement_scenario(self):
        label_id = "personal_attack"
        corpus_by_id = {
            "i1": _corpus_item("i1", "prevalence"),
            "i2": _corpus_item("i2", "prevalence"),
            "i3": _corpus_item("i3", "prevalence"),
            "i4": _corpus_item("i4", "prevalence"),
            "i5": _corpus_item("i5", "enrichment"),
            "i6": _corpus_item("i6", "enrichment"),
        }
        probs = {"i1": 0.9, "i2": 0.1, "i3": 0.8, "i4": 0.2, "i5": 0.95, "i6": 0.05}
        classification_by_id = {mid: _record(mid, p) for mid, p in probs.items()}
        marks = {"i1": "yes", "i2": "no", "i3": "yes", "i4": "no", "i5": "yes", "i6": "no"}
        rater_labels = {"r1": {mid: _label_record(mid, "r1", mark) for mid, mark in marks.items()}}

        ev = evaluate_label(
            label_id,
            "reputational_harm",
            corpus_by_id,
            classification_by_id,
            rater_labels,
            seed=1,
            bootstrap_iterations=50,
        )

        assert ev.n_candidates == 6
        assert ev.n_scored == 6
        assert ev.n_excluded == 0
        assert ev.n_positives == 3
        assert ev.best.precision == pytest.approx(1.0)
        assert ev.best.recall == pytest.approx(1.0)
        assert ev.best.f1 == pytest.approx(1.0)
        assert ev.gate["status"] == "likely_pass"
        # Prevalence stratum only: i1-i4, 2 positives / 4 = 0.5.
        assert ev.prevalence["n"] == 4
        assert ev.prevalence["successes"] == 2
        assert ev.prevalence["proportion"] == pytest.approx(0.5)
        expected_ci = stats.wilson_interval(2, 4)
        assert ev.prevalence["ci"] == pytest.approx(expected_ci)
        assert ev.stratum_breakdown["prevalence"]["n"] == 4
        assert ev.stratum_breakdown["enrichment"]["n"] == 2

    def test_unsure_and_tied_items_are_excluded(self):
        label_id = "sarcasm"
        corpus_by_id = {
            "i1": _corpus_item("i1", "prevalence"),
            "i2": _corpus_item("i2", "prevalence"),
        }
        classification_by_id = {"i1": _record("i1", 0.5), "i2": _record("i2", 0.5)}
        rater_labels = {
            "r1": {
                "i1": _label_record("i1", "r1", "unsure"),
                "i2": _label_record("i2", "r1", "yes"),
            },
            "r2": {
                "i1": _label_record("i1", "r2", "unsure"),
                "i2": _label_record("i2", "r2", "no"),
            },
        }
        ev = evaluate_label(
            label_id,
            "friction",
            corpus_by_id,
            classification_by_id,
            rater_labels,
            seed=1,
            bootstrap_iterations=10,
        )
        # i1: both unsure -> excluded. i2: yes vs no -> tie -> excluded.
        assert ev.n_scored == 0
        assert ev.n_excluded == 2

    def test_items_never_rated_by_anyone_are_neither_scored_nor_excluded(self):
        label_id = "hostility"
        corpus_by_id = {"i1": _corpus_item("i1", "prevalence")}
        classification_by_id = {"i1": _record("i1", 0.5)}
        ev = evaluate_label(
            label_id,
            "reputational_harm",
            corpus_by_id,
            classification_by_id,
            {},
            seed=1,
            bootstrap_iterations=10,
        )
        assert ev.n_scored == 0
        assert ev.n_excluded == 0


# --- evaluate_tone ---------------------------------------------------------------------------


class TestEvaluateTone:
    def test_computes_exact_off_by_one_and_kappa(self):
        classification_by_id = {
            "i1": _record(
                "i1", 0.5, tone_probs={"0": 0.9, "1": 0.05, "2": 0.05, "3": 0.0, "4": 0.0}
            ),
            "i2": _record("i2", 0.5, tone_probs={"0": 0.0, "1": 0.0, "2": 0.0, "3": 0.1, "4": 0.9}),
        }
        rater_labels = {
            "r1": {
                "i1": _label_record("i1", "r1", "no", tone=0),
                "i2": _label_record("i2", "r1", "no", tone=4),
            }
        }
        tone = evaluate_tone(classification_by_id, rater_labels)
        assert tone.n_paired == 2
        assert tone.exact_agreement == pytest.approx(1.0)
        assert tone.off_by_one == pytest.approx(1.0)
        assert tone.weighted_kappa == pytest.approx(1.0)

    def test_no_paired_items_returns_none_fields(self):
        tone = evaluate_tone({}, {})
        assert tone.n_paired == 0
        assert tone.exact_agreement is None


# --- evaluate_agreement ----------------------------------------------------------------------


class TestEvaluateAgreement:
    def test_returns_empty_with_fewer_than_two_raters(self):
        corpus_items = [_corpus_item("i1", "prevalence")]
        rater_labels = {"r1": {"i1": _label_record("i1", "r1", "yes")}}
        assert evaluate_agreement(["personal_attack"], corpus_items, rater_labels) == {}

    def test_perfect_agreement_between_two_raters(self):
        corpus_items = [_corpus_item(f"i{i}", "prevalence") for i in range(1, 5)]
        marks = {"i1": "yes", "i2": "yes", "i3": "no", "i4": "no"}
        rater_labels = {
            "r1": {mid: _label_record(mid, "r1", mark) for mid, mark in marks.items()},
            "r2": {mid: _label_record(mid, "r2", mark) for mid, mark in marks.items()},
        }
        result = evaluate_agreement(["personal_attack"], corpus_items, rater_labels)
        assert result["personal_attack"] == pytest.approx(1.0)


# --- Loading helpers -------------------------------------------------------------------------


class TestLoaders:
    def test_load_classification_records_from_directory(self, tmp_path: Path):
        out_dir = tmp_path / "results"
        out_dir.mkdir()
        record = _record("i1", 0.5)
        (out_dir / "classifications.jsonl").write_text(
            json.dumps(record.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        records = load_classification_records(out_dir)
        assert set(records) == {"i1"}

    def test_load_jev_summary_returns_none_when_absent(self, tmp_path: Path):
        assert load_jev_summary(tmp_path) is None

    def test_load_jev_summary_reads_json(self, tmp_path: Path):
        (tmp_path / "summary.json").write_text(json.dumps({"calls_made": 3}), encoding="utf-8")
        summary = load_jev_summary(tmp_path)
        assert summary == {"calls_made": 3}

    def test_load_rater_labels_dedupes_by_filename_when_rater_field_collides(self, tmp_path: Path):
        path_a = tmp_path / "a.jsonl"
        path_b = tmp_path / "b.jsonl"
        path_a.write_text(
            json.dumps(_label_record("i1", "same_name", "yes")) + "\n", encoding="utf-8"
        )
        path_b.write_text(
            json.dumps(_label_record("i2", "same_name", "no")) + "\n", encoding="utf-8"
        )
        raters = load_rater_labels([path_a, path_b])
        assert len(raters) == 2


# --- End-to-end evaluate_pilot -----------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class TestEvaluatePilotEndToEnd:
    def _setup(self, tmp_path: Path, n_raters: int) -> dict:
        items = [
            {
                "id": f"i{i}",
                "stratum": "prevalence" if i <= 4 else "enrichment",
                "source": "mailing_list",
                "archive_url": f"https://example.invalid/{i}",
                "text": f"synthetic message {i}",
                "parent_text": None,
                "checksum": f"c{i}",
            }
            for i in range(1, 7)
        ]
        corpus_path = tmp_path / "corpus.jsonl"
        _write_jsonl(corpus_path, items)

        results_dir = tmp_path / "results"
        results_dir.mkdir()
        records = [_record(item["id"], 0.5 + 0.05 * i) for i, item in enumerate(items)]
        _write_jsonl(
            results_dir / "classifications.jsonl",
            [r.model_dump(mode="json") for r in records],
        )
        (results_dir / "summary.json").write_text(
            json.dumps(
                {
                    "calls_made": 6,
                    "cache_hits": 0,
                    "estimated_cost_usd": 0.001,
                    "mean_latency_seconds_per_call": 0.2,
                }
            ),
            encoding="utf-8",
        )

        label_paths = []
        for r in range(1, n_raters + 1):
            rows = [
                _label_record(item["id"], f"rater{r}", "yes" if i % 2 == 0 else "no")
                for i, item in enumerate(items)
            ]
            path = tmp_path / f"labels_rater{r}.jsonl"
            _write_jsonl(path, rows)
            label_paths.append(path)

        return {"corpus_path": corpus_path, "results_dir": results_dir, "label_paths": label_paths}

    def test_one_rater_has_no_agreement_statistic(self, tmp_path: Path):
        paths = self._setup(tmp_path, n_raters=1)
        result = evaluate_pilot(
            corpus_path=paths["corpus_path"],
            results_dir=paths["results_dir"],
            label_paths=paths["label_paths"],
            bootstrap_iterations=20,
        )
        assert result.n_corpus_items == 6
        assert result.n_classified_items == 6
        assert result.rater_names == ["rater1"]
        assert result.agreement == {}
        assert result.jev_summary is not None

    def test_two_raters_have_agreement_statistics(self, tmp_path: Path):
        paths = self._setup(tmp_path, n_raters=2)
        result = evaluate_pilot(
            corpus_path=paths["corpus_path"],
            results_dir=paths["results_dir"],
            label_paths=paths["label_paths"],
            bootstrap_iterations=20,
        )
        assert result.rater_names == ["rater1", "rater2"]
        assert result.agreement != {}
        assert set(result.agreement) == MESSAGE_LEVEL_LABELS
