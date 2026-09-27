"""End-to-end offline test for project_health.benchmark_public.runner (issue
#89): a tiny synthetic registry + mapping (real `load_toxicr` loader, a
synthetic in-memory xlsx -- not the real ToxiCR data) run through
`run_benchmark` with both the dataset download (`httpx`) and the Jev call
(`httpx2`) served by mocked transports. No network access anywhere.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import httpx2
import pytest
import yaml

from project_health.benchmark_public.evaluate import evaluate_benchmark
from project_health.benchmark_public.report import render_public_report_markdown
from project_health.benchmark_public.registry import load_registry
from project_health.benchmark_public.mapping import load_label_mapping
from project_health.benchmark_public.runner import message_id_for, run_benchmark
from project_health.classify.questions import MESSAGE_LEVEL_LABELS


def _build_toxicr_xlsx(path: Path, rows: list[tuple[str, int]]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["message", "is_toxic"])
    for message, is_toxic in rows:
        ws.append([message, is_toxic])
    wb.save(path)
    return path.read_bytes()


def _write_registry_and_mapping(tmp_path: Path, sha256: str, target_n: int) -> tuple[Path, Path]:
    registry_data = {
        "version": 1,
        "datasets": [
            {
                "id": "toy_toxicr",
                "name": "Toy ToxiCR",
                "citation": "Test Author, Test Paper, 2026.",
                "license": "CC0",
                "status": "working",
                "files": [
                    {
                        "url": "https://example.org/toy.xlsx",
                        "sha256": sha256,
                        "filename": "toy.xlsx",
                    }
                ],
                "loader": "load_toxicr",
                "source_venue": "github_pr_comment",
                "source_venue_rationale": "test fixture",
                "target_n": target_n,
                "seed": 7,
            }
        ],
    }
    mapping_data = {
        "version": 1,
        "mappings": {
            "toy_toxicr": [
                {
                    "our_label": "hostility",
                    "raw_field": "is_toxic",
                    "positive_values": [1],
                    "strength": "partial",
                    "gating": False,
                    "notes": "test mapping",
                }
            ]
        },
    }
    registry_path = tmp_path / "registry.yaml"
    mapping_path = tmp_path / "mapping.yaml"
    registry_path.write_text(yaml.safe_dump(registry_data), encoding="utf-8")
    mapping_path.write_text(yaml.safe_dump(mapping_data), encoding="utf-8")
    return registry_path, mapping_path


def _jev_transport() -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        state = body["state"]
        # A deterministic, content-dependent "toxic" probability so the
        # eventual precision/recall numbers aren't trivially degenerate: a
        # message containing "idiot" gets a high hostility probability.
        is_hostile_text = "idiot" in state["message"]["text"].lower()
        hostility_prob = 0.9 if is_hostile_text else 0.05
        answers = {
            label: {
                "type": "noul",
                "noul": hostility_prob if label == "hostility" else 0.05,
            }
            for label in MESSAGE_LEVEL_LABELS
        }
        answers["tone_intensity"] = {
            "type": "score",
            "score": 1.0,
            "confidence": 0.5,
            "legend": {0: "neutral", 1: "firm", 2: "sharp", 3: "heated", 4: "aggressive"},
            "probabilities": {0: 0.2, 1: 0.2, 2: 0.2, 3: 0.2, 4: 0.2},
        }
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": answers,
                "usage": {"input_tokens": 50, "output_tokens": 5},
            },
        )

    return httpx2.MockTransport(handler)


def test_run_benchmark_end_to_end(tmp_path: Path) -> None:
    xlsx_path = tmp_path / "source.xlsx"
    rows = [
        ("nit: please rename this variable", 0),
        ("you are an absolute idiot for suggesting this", 1),
        ("thanks, looks good to me", 0),
        ("this idiot approach will never work", 1),
    ]
    content = _build_toxicr_xlsx(xlsx_path, rows)
    sha256 = hashlib.sha256(content).hexdigest()

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    download_transport = httpx.MockTransport(download_handler)

    registry_path, mapping_path = _write_registry_and_mapping(tmp_path, sha256, target_n=10)
    cache_dir = tmp_path / "cache"

    run = run_benchmark(
        cache_dir=cache_dir,
        registry_path=registry_path,
        mapping_path=mapping_path,
        api_key="test-key",
        transport=download_transport,
        async_transport=_jev_transport(),
    )

    assert run.run_result.status == "completed"
    assert run.run_result.calls_made == 4
    assert (cache_dir / "jev_cache.jsonl").is_file()
    assert (cache_dir / "run_manifest.json").is_file()
    assert (cache_dir / "datasets" / "toy_toxicr" / "toy.xlsx").is_file()

    # A second run must be free: everything is already cached.
    run2 = run_benchmark(
        cache_dir=cache_dir,
        registry_path=registry_path,
        mapping_path=mapping_path,
        api_key="test-key",
        transport=download_transport,
        async_transport=_jev_transport(),
    )
    assert run2.run_result.calls_made == 0
    assert run2.run_result.cache_hits == 4

    registry = load_registry(registry_path)
    mapping_set = load_label_mapping(mapping_path)
    evaluations = evaluate_benchmark(run2, mapping_set, seed=1, bootstrap_iterations=10)
    assert "toy_toxicr" in evaluations
    hostility_eval = evaluations["toy_toxicr"][0]
    assert hostility_eval.our_label == "hostility"
    # Both "idiot" messages are ground-truth toxic and predicted hostile at
    # high probability; the other two are neither -- perfect separation.
    assert hostility_eval.best.precision == 1.0
    assert hostility_eval.best.recall == 1.0

    markdown = render_public_report_markdown(registry, evaluations, run2.manifest)
    assert "Toy ToxiCR" in markdown
    assert "hostility" in markdown
    # No dataset item text/ids anywhere in the public report.
    assert "idiot" not in markdown
    assert "nit: please rename" not in markdown


def test_duplicate_inputs_are_all_evaluated_not_silently_dropped(tmp_path: Path) -> None:
    """Orchestrator review of issue #89: two rows with byte-identical
    preprocessed text hash to the same `input_hash`, and `JevClassifier`'s
    cache stores exactly one record for that hash (D22's own design) --
    stamped with whichever item's `message_id` reached it first. Every
    sampled item must still be joinable back to that one record by its own
    `message_id`, not just the first one to arrive.
    """
    xlsx_path = tmp_path / "source.xlsx"
    # Two rows share the exact same message text ("LGTM") and the same
    # ground truth -- a duplicate input that must NOT be silently dropped
    # from evaluation.
    rows = [
        ("LGTM", 0),
        ("LGTM", 0),
        ("you idiot, this will never work", 1),
    ]
    content = _build_toxicr_xlsx(xlsx_path, rows)
    sha256 = hashlib.sha256(content).hexdigest()

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    registry_path, mapping_path = _write_registry_and_mapping(tmp_path, sha256, target_n=10)
    cache_dir = tmp_path / "cache"

    run = run_benchmark(
        cache_dir=cache_dir,
        registry_path=registry_path,
        mapping_path=mapping_path,
        api_key="test-key",
        transport=httpx.MockTransport(download_handler),
        async_transport=_jev_transport(),
    )

    # 3 sampled rows, only 2 distinct inputs ("LGTM" appears twice) -- but
    # every one of the 3 message_ids must still resolve to a record.
    assert run.manifest["datasets"]["toy_toxicr"]["n_sampled"] == 3
    assert run.manifest["datasets"]["toy_toxicr"]["n_distinct_inputs"] == 2
    assert run.manifest["datasets"]["toy_toxicr"]["n_evaluated"] == 3
    assert len(run.classification_by_message_id) == 3

    # Both "LGTM" rows resolve to a record with the identical (correct,
    # low) hostility probability, even though only one Jev call was made
    # for that shared input.
    assert run.run_result.calls_made == 2  # one call per distinct input
    lgtm_ids = [message_id_for("toy_toxicr", f"row_{i}") for i in (0, 1)]
    for message_id in lgtm_ids:
        record = run.classification_by_message_id[message_id]
        assert record.labels["hostility"].probability == pytest.approx(0.05)
