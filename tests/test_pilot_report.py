"""Tests for `project_health.pilot.report` (issue #47).

The privacy test (`TestPublicReportNeverLeaksPrivateData`) is the one issue
#47 explicitly asks for: a synthetic fixture corpus with distinctive item
ids and message text is run through the full pilot-evaluate pipeline, and
the test fails if any of those ids or text substrings appear anywhere in
the rendered *public* report. The private report is checked for the
opposite property: it is allowed (expected) to carry item ids, but never
message text.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from project_health.classify.classifier import ClassificationRecord, Label, ToneIntensity, Usage
from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.pilot.evaluate import evaluate_pilot
from project_health.pilot.report import (
    render_private_report_markdown,
    render_public_report_markdown,
)

FIXED_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

# Distinctive, obviously-synthetic identifiers and text substrings that must
# never leak into the public report. Chosen to be long/unusual enough that
# an accidental match would be meaningful, not a coincidence.
_SECRET_ITEM_IDS = [
    "mail:zzq-super-secret-message-0001",
    "jira:CASSANDRA-99999:zzq-secret-comment-0002",
]
_SECRET_TEXT_SUBSTRINGS = [
    "the quokka jumped over the lazy compaction thread",
    "zzq-unmistakable-marker-in-body-text",
]
_SECRET_ARCHIVE_URL = "https://example.invalid/zzq-secret-archive-path"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _record(message_id: str, probability: float) -> ClassificationRecord:
    labels = {label_id: Label(probability=probability) for label_id in MESSAGE_LEVEL_LABELS}
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
        tone_intensity=ToneIntensity(
            score=1.0,
            confidence=0.7,
            probabilities={"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0},
        ),
    )


def _build_pilot_result(tmp_path: Path):
    items = [
        {
            "id": _SECRET_ITEM_IDS[0],
            "stratum": "prevalence",
            "source": "mailing_list",
            "archive_url": _SECRET_ARCHIVE_URL,
            "text": _SECRET_TEXT_SUBSTRINGS[0],
            "parent_text": None,
            "checksum": "c1",
        },
        {
            "id": _SECRET_ITEM_IDS[1],
            "stratum": "enrichment",
            "source": "jira_comment",
            "archive_url": _SECRET_ARCHIVE_URL + "-2",
            "text": _SECRET_TEXT_SUBSTRINGS[1],
            "parent_text": "some prior comment text, also private",
            "checksum": "c2",
        },
    ]
    corpus_path = tmp_path / "corpus.jsonl"
    _write_jsonl(corpus_path, items)

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    records = [_record(item["id"], 0.7) for item in items]
    _write_jsonl(
        results_dir / "classifications.jsonl", [r.model_dump(mode="json") for r in records]
    )
    (results_dir / "summary.json").write_text(
        json.dumps({"calls_made": 2, "cache_hits": 0, "estimated_cost_usd": 0.0001}),
        encoding="utf-8",
    )

    label_rows = [
        {
            "item_id": item["id"],
            "rater": "pmcfadin",
            "labels": {label_id: "yes" for label_id in MESSAGE_LEVEL_LABELS},
            "tone": 0,
            "note": None,
            "seconds": 20.0,
            "saved_at": "2026-01-01T00:00:00Z",
            "corpus_checksum": item["checksum"],
            "label_set_version": 1,
            "question_set_version": 1,
        }
        for item in items
    ]
    labels_path = tmp_path / "labels_pmcfadin.jsonl"
    _write_jsonl(labels_path, label_rows)

    return evaluate_pilot(
        corpus_path=corpus_path,
        results_dir=results_dir,
        label_paths=[labels_path],
        bootstrap_iterations=10,
    )


class TestPublicReportNeverLeaksPrivateData:
    def test_no_item_ids_no_text_no_archive_urls_no_rater_name(self, tmp_path: Path):
        result = _build_pilot_result(tmp_path)
        public_markdown = render_public_report_markdown(result)

        for secret_id in _SECRET_ITEM_IDS:
            assert secret_id not in public_markdown, (
                f"item id leaked into public report: {secret_id!r}"
            )
        for secret_text in _SECRET_TEXT_SUBSTRINGS:
            assert secret_text not in public_markdown, (
                f"message text leaked into public report: {secret_text!r}"
            )
        assert _SECRET_ARCHIVE_URL not in public_markdown
        assert "pmcfadin" not in public_markdown  # the rater's name

    def test_public_report_still_contains_aggregate_content(self, tmp_path: Path):
        result = _build_pilot_result(tmp_path)
        public_markdown = render_public_report_markdown(result)
        assert "## Recommendation" in public_markdown
        assert "personal_attack" in public_markdown  # label ids are fine, not private


class TestPrivateReportContent:
    def test_private_report_may_reference_item_ids_but_never_message_text(self, tmp_path: Path):
        result = _build_pilot_result(tmp_path)
        private_markdown = render_private_report_markdown(result)

        # Item ids ARE expected in the private report (join key into the
        # private corpus file) -- at least one should appear.
        assert any(secret_id in private_markdown for secret_id in _SECRET_ITEM_IDS)
        # But message text must never appear, even in the private report --
        # the report renders from structured records, never from the corpus
        # text itself.
        for secret_text in _SECRET_TEXT_SUBSTRINGS:
            assert secret_text not in private_markdown

    def test_private_report_includes_jev_cost_summary(self, tmp_path: Path):
        result = _build_pilot_result(tmp_path)
        private_markdown = render_private_report_markdown(result)
        assert "Jev cost and latency" in private_markdown
        assert "estimated_cost_usd" not in private_markdown  # rendered as prose, not a raw key
        assert "0.0001" in private_markdown or "Estimated cost (USD): 0.0001" in private_markdown
