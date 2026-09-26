"""Tests for `project_health.pilot.classify_runner` (issue #47).

Offline by default: every `system_one` call goes through an injected
`httpx2.MockTransport`, mirroring `tests/test_classifier.py`'s pattern.
`tests/conftest.py`'s network block also catches any accidental real socket
connection regardless.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.pilot.classify_runner import label_probability_summary, run_pilot_classify


def _write_corpus(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item) + "\n")


def _corpus_items() -> list[dict]:
    return [
        {
            "id": "mail:1",
            "stratum": "prevalence",
            "source": "mailing_list",
            "archive_url": "https://example.invalid/1",
            "text": "I disagree with this approach on technical grounds.",
            "parent_text": None,
            "checksum": "a",
        },
        {
            "id": "jira:X-1:10",
            "stratum": "enrichment",
            "source": "jira_comment",
            "archive_url": "https://example.invalid/2",
            "text": "Agreed, committing this today.",
            "parent_text": "some prior comment",
            "checksum": "b",
        },
    ]


def _fixed_answers(value: float = 0.4) -> dict:
    answers = {label: {"type": "noul", "noul": value} for label in MESSAGE_LEVEL_LABELS}
    answers["tone_intensity"] = {
        "type": "score",
        "score": 1.0,
        "confidence": 0.5,
        "legend": {0: "n", 1: "f", 2: "s", 3: "h", 4: "a"},
        "probabilities": {0: 0.2, 1: 0.5, 2: 0.2, 3: 0.05, 4: 0.05},
    }
    return answers


def _mock_transport(value: float = 0.4, call_log: list[dict] | None = None) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if call_log is not None:
            call_log.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "usage": {"input_tokens": 50, "output_tokens": 10},
                "answers": _fixed_answers(value),
            },
        )

    return httpx2.MockTransport(handler)


class TestRunPilotClassify:
    def test_classifies_every_corpus_item(self, tmp_path: Path):
        corpus_path = tmp_path / "corpus.jsonl"
        _write_corpus(corpus_path, _corpus_items())
        out_dir = tmp_path / "out"

        result = run_pilot_classify(
            corpus_path=corpus_path, out_dir=out_dir, api_key="k", async_transport=_mock_transport()
        )

        assert len(result.items) == 2
        assert result.run_result.status == "completed"
        assert result.run_result.calls_made == 2
        assert result.run_result.cache_hits == 0
        assert result.records_path.exists()
        assert result.summary_path.exists()
        assert result.cache_path.exists()

        lines = result.records_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        message_ids = {json.loads(line)["message_id"] for line in lines}
        assert message_ids == {"mail:1", "jira:X-1:10"}

    def test_rerun_makes_zero_new_calls_and_costs_nothing(self, tmp_path: Path):
        corpus_path = tmp_path / "corpus.jsonl"
        _write_corpus(corpus_path, _corpus_items())
        out_dir = tmp_path / "out"
        call_log: list[dict] = []
        transport = _mock_transport(call_log=call_log)

        first = run_pilot_classify(
            corpus_path=corpus_path, out_dir=out_dir, api_key="k", async_transport=transport
        )
        assert first.run_result.calls_made == 2
        assert len(call_log) == 2

        second = run_pilot_classify(
            corpus_path=corpus_path, out_dir=out_dir, api_key="k", async_transport=transport
        )
        assert second.run_result.calls_made == 0
        assert second.run_result.cache_hits == 2
        assert second.summary["estimated_cost_usd"] == pytest.approx(0.0)
        assert len(call_log) == 2  # no new network calls on rerun

    def test_summary_has_cost_and_latency_fields(self, tmp_path: Path):
        corpus_path = tmp_path / "corpus.jsonl"
        _write_corpus(corpus_path, _corpus_items())
        out_dir = tmp_path / "out"

        result = run_pilot_classify(
            corpus_path=corpus_path, out_dir=out_dir, api_key="k", async_transport=_mock_transport()
        )

        summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
        assert summary["calls_made"] == 2
        assert summary["estimated_cost_usd"] > 0
        assert summary["elapsed_seconds"] >= 0
        assert summary["mean_latency_seconds_per_call"] is not None
        assert summary["classifier_version"]
        assert summary["question_set_version"]
        assert summary["model_id_pinned"]

    def test_summary_never_contains_message_text(self, tmp_path: Path):
        corpus_path = tmp_path / "corpus.jsonl"
        _write_corpus(corpus_path, _corpus_items())
        out_dir = tmp_path / "out"

        result = run_pilot_classify(
            corpus_path=corpus_path, out_dir=out_dir, api_key="k", async_transport=_mock_transport()
        )
        raw_summary = result.summary_path.read_text(encoding="utf-8")
        assert "I disagree with this approach" not in raw_summary
        assert "committing this today" not in raw_summary

    def test_label_probability_summary_computes_mean_and_count(self, tmp_path: Path):
        corpus_path = tmp_path / "corpus.jsonl"
        _write_corpus(corpus_path, _corpus_items())
        out_dir = tmp_path / "out"

        result = run_pilot_classify(
            corpus_path=corpus_path,
            out_dir=out_dir,
            api_key="k",
            async_transport=_mock_transport(0.4),
        )
        summary = label_probability_summary(result.run_result.records)
        for label_id in MESSAGE_LEVEL_LABELS:
            assert summary[label_id]["n"] == 2
            assert summary[label_id]["mean_probability"] == pytest.approx(0.4)

    def test_label_probability_summary_handles_zero_records(self):
        summary = label_probability_summary([])
        for label_id in MESSAGE_LEVEL_LABELS:
            assert summary[label_id]["n"] == 0
            assert summary[label_id]["mean_probability"] is None
