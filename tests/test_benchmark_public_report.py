"""Tests for project_health.benchmark_public.report (issue #89; DECISIONS.md
D23; COMMUNITY-HEALTH.md §7).

The leak test below is the backstop this module's own docstring describes:
`DatasetLabelEvaluation`/`registry`/`run_manifest` never carry item text or
ids into `render_public_report_markdown` in the first place, but this test
still asserts a synthetic fixture's distinctive item ids never appear in the
rendered markdown, so a future refactor that *does* start passing per-item
detail in gets caught immediately.
"""

from __future__ import annotations

from project_health.benchmark_public.evaluate import evaluate_dataset_label
from project_health.benchmark_public.loaders import DatasetItem
from project_health.benchmark_public.mapping import LabelMapping
from project_health.benchmark_public.registry import DatasetSpec, Registry
from project_health.benchmark_public.report import render_public_report_markdown
from project_health.classify.classifier import ClassificationRecord, Label, Usage
from datetime import datetime, timezone

# Long/unusual "item ids" and a distinctive text substring -- if either leaked
# into the rendered markdown, this is not something that could happen by
# coincidence.
SECRET_ITEM_ID = "zzqx-secret-item-9f3c7a21"
SECRET_TEXT = "the quick brown zqxjklw fence jumps over"

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def _record(message_id: str, prob: float) -> ClassificationRecord:
    return ClassificationRecord(
        message_id=message_id,
        thread_id=message_id,
        source="mailing_list",
        classifier_version="1.0.0",
        question_set_version="1",
        model_id="jev-1.13.0",
        input_hash="h" * 64,
        classified_at=NOW,
        usage=Usage(input_tokens=10, output_tokens=1),
        labels={"hostility": Label(probability=prob)},
    )


def _spec() -> DatasetSpec:
    return DatasetSpec(
        id="ds1",
        name="Dataset One",
        citation="Author, Title, Year.",
        license="CC0",
        status="working",
        blocked_reason=None,
        files=(),
        loader="load_ds1",
        source_venue="mailing_list",
        source_venue_rationale="it's a mailing list",
        target_n=10,
        seed=1,
        reported_iaa="Krippendorff's alpha 0.9",
        caveats=("a caveat",),
    )


def test_public_report_never_leaks_item_ids_or_text() -> None:
    mapping = LabelMapping(
        dataset_id="ds1",
        our_label="hostility",
        raw_field="attack",
        positive_values=(True,),
        strength="strong",
        gating=True,
        notes="a mapping note",
    )
    items = [
        DatasetItem(SECRET_ITEM_ID, SECRET_TEXT, None, {"attack": True}),
        DatasetItem("i2", "some other text", None, {"attack": False}),
    ]
    records = {
        SECRET_ITEM_ID: _record(SECRET_ITEM_ID, 0.9),
        "i2": _record("i2", 0.1),
    }
    evaluation = evaluate_dataset_label(
        "ds1", mapping, items, records, population_n=2, population_positives=1, seed=1
    )
    assert evaluation is not None

    registry = Registry(version=1, datasets={"ds1": _spec()})
    manifest = {
        "classifier": {
            "calls_made": 2,
            "cache_hits": 0,
            "input_tokens_used": 20,
            "output_tokens_used": 2,
            "estimated_cost_usd": 0.0001,
            "elapsed_seconds": 0.5,
            "mean_latency_seconds_per_call": 0.25,
            "status": "completed",
            "classifier_version": "1.0.0",
            "question_set_version": "1",
            "model_id_pinned": "jev-1.13.0",
        },
        "blocked_datasets": [{"name": "Blocked Dataset", "reason": "dead link"}],
        "datasets": {"ds1": {"sample_n": 2, "population_n": 2}},
    }

    markdown = render_public_report_markdown(registry, {"ds1": [evaluation]}, manifest)

    assert SECRET_ITEM_ID not in markdown
    assert SECRET_TEXT not in markdown
    assert "i2" not in markdown  # the other item's internal id also never appears

    # But the aggregate-level facts we *want* published are there.
    assert "Dataset One" in markdown
    assert "hostility" in markdown
    assert "CC0" in markdown
    assert "Blocked Dataset" in markdown
    assert "dead link" in markdown
    assert "no label has a calibrated" in markdown.lower()
