"""`project-health pilot-classify` (issue #47; DECISIONS.md D17, D18, D22).

Runs the pinned Jev classifier (`project_health.classify.classifier.
JevClassifier`, issue #45) over every item in the pilot corpus
(`classify/sample.py`, issue #44) and writes the resulting classification
records plus a cost/latency summary to a caller-supplied private directory.

D22 discipline: this module makes exactly the one model call class D22
permits (the pinned Jev classifier, via `JevClassifier.run`) and nothing
else -- no LLM triages, ranks, or interprets anything here. The classifier's
own input-hash cache (`ClassificationCache`, issue #45) is what makes a
re-run "cost nothing" (D22: "re-rendering never calls the model again"): a
cache file living in `--out` is read on every invocation and only messages
whose input hash isn't already cached are ever sent.

Each pilot corpus item (`classify/sample.py`'s `CorpusItem.to_jsonl_dict`)
is one standalone message, not a multi-message thread -- the pilot corpus
schema (issue #44/#46) carries no `thread_id` field at all, only `parent_
text`. This module treats each item as the root of its own one-message
"thread" (`thread_id = item.id`) purely so `NormalizedMessage`/
`ClassificationRecord` have a value for the field COMMUNITY-HEALTH.md §4.3
requires -- no thread-level derivation (§2.3) is computed or claimed here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_health.classify.classifier import (
    ClassificationCache,
    ClassificationRecord,
    CostCap,
    JevClassifier,
    MessageSource,
    NormalizedMessage,
    ParentContext,
    RunResult,
)
from project_health.classify.questions import MESSAGE_LEVEL_LABELS
from project_health.label.store import CorpusItem, load_corpus

DEFAULT_CACHE_FILENAME = "jev_cache.jsonl"
DEFAULT_RECORDS_FILENAME = "classifications.jsonl"
DEFAULT_SUMMARY_FILENAME = "summary.json"

# A cap high enough that no real pilot run (192 items, D18) will ever trip
# it -- this project's D10 cost cap exists to bound *ongoing* monthly spend,
# not a single one-off pilot batch, so `pilot-classify` defaults to "track
# cost, never pause" unless the caller passes an explicit, deliberate
# `--monthly-cap-usd`.
DEFAULT_MONTHLY_CAP_USD = 1000.0


@dataclass(frozen=True)
class PilotClassifyResult:
    items: list[CorpusItem]
    run_result: RunResult
    elapsed_seconds: float
    summary: dict[str, Any]
    records_path: Path
    summary_path: Path
    cache_path: Path


def _corpus_item_to_call(item: CorpusItem) -> tuple[NormalizedMessage, ParentContext]:
    source: MessageSource = item.source  # type: ignore[assignment]
    message = NormalizedMessage(
        message_id=item.id,
        thread_id=item.id,  # see module docstring: the pilot corpus has no thread_id
        source=source,
        text=item.text,
    )
    return message, ParentContext(text=item.parent_text)


def label_probability_summary(records: list[ClassificationRecord]) -> dict[str, dict[str, Any]]:
    """Per-label mean probability + item count across `records` -- exactly
    what issue #47's real-run report pastes ("Probabilities only, no
    text")."""
    sums: dict[str, float] = {label: 0.0 for label in MESSAGE_LEVEL_LABELS}
    counts: dict[str, int] = {label: 0 for label in MESSAGE_LEVEL_LABELS}
    for record in records:
        for label_id, label in record.labels.items():
            sums[label_id] += label.probability
            counts[label_id] += 1
    return {
        label_id: {
            "mean_probability": (sums[label_id] / counts[label_id]) if counts[label_id] else None,
            "n": counts[label_id],
        }
        for label_id in sorted(MESSAGE_LEVEL_LABELS)
    }


def run_pilot_classify(
    *,
    corpus_path: str | Path,
    out_dir: str | Path,
    api_key: str | None = None,
    concurrency: int = 4,
    classifier_version: str = "1.0.0",
    monthly_cap_usd: float = DEFAULT_MONTHLY_CAP_USD,
    pricing_config_path: str | Path | None = None,
    cache_filename: str = DEFAULT_CACHE_FILENAME,
    records_filename: str = DEFAULT_RECORDS_FILENAME,
    summary_filename: str = DEFAULT_SUMMARY_FILENAME,
    transport: Any = None,
    async_transport: Any = None,
    question_set: Any = None,
) -> PilotClassifyResult:
    """Classify every item in `corpus_path` and write records + a summary
    into `out_dir`. Safe (and free) to re-run: `out_dir`'s cache file is
    reused, so a message already classified in a prior run is never
    re-sent (D22).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    items = load_corpus(corpus_path)
    cache = ClassificationCache(out_dir / cache_filename)
    cost_cap = CostCap.from_config(
        monthly_cap_usd=monthly_cap_usd, pricing_config_path=pricing_config_path
    )
    classifier = JevClassifier(
        api_key=api_key,
        question_set=question_set,
        cache=cache,
        cost_cap=cost_cap,
        concurrency=concurrency,
        classifier_version=classifier_version,
        transport=transport,
        async_transport=async_transport,
    )

    calls = [_corpus_item_to_call(item) for item in items]
    start = time.monotonic()
    run_result = classifier.run(calls)
    elapsed_seconds = time.monotonic() - start

    records_path = out_dir / records_filename
    with open(records_path, "w", encoding="utf-8") as fh:
        for record in run_result.records:
            fh.write(json.dumps(record.model_dump(mode="json"), sort_keys=True))
            fh.write("\n")

    mean_latency = elapsed_seconds / run_result.calls_made if run_result.calls_made else None
    summary: dict[str, Any] = {
        "corpus_items": len(items),
        "status": run_result.status,
        "calls_made": run_result.calls_made,
        "cache_hits": run_result.cache_hits,
        "records_written": len(run_result.records),
        "input_tokens_used": run_result.input_tokens_used,
        "output_tokens_used": run_result.output_tokens_used,
        "estimated_cost_usd": run_result.estimated_cost_usd,
        "elapsed_seconds": elapsed_seconds,
        "mean_latency_seconds_per_call": mean_latency,
        "classifier_version": classifier.classifier_version,
        "question_set_version": classifier.question_set_version,
        "model_id_pinned": classifier.model_id,
        "label_probability_summary": label_probability_summary(run_result.records),
    }
    summary_path = out_dir / summary_filename
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return PilotClassifyResult(
        items=items,
        run_result=run_result,
        elapsed_seconds=elapsed_seconds,
        summary=summary,
        records_path=records_path,
        summary_path=summary_path,
        cache_path=cache.path,
    )
