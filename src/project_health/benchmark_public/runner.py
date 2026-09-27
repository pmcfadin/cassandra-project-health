"""Download -> sample -> classify pipeline for `benchmark-public` (issue #89;
DECISIONS.md D22, D23).

Ties `download.py` (pinned fetch + sha256), `registry.py`/`loaders.py`
(per-dataset load), `sampling.py` (seeded stratified sample), and
`classify.classifier.JevClassifier` (the one D22-permitted model call)
together into one run: for every `working` dataset in the registry, download
its pinned file(s), load it, draw the seeded sample, build classifier state
per item (message + parent text when the dataset has one, tagged with the
dataset's chosen `source_venue`), and run the cached, cost-capped Jev
classifier over the combined item list from every dataset in one batch (so
`JevClassifier.run`'s bounded concurrency and D10-style cost cap apply across
the whole benchmark, not per dataset).

`ClassificationCache`/`CostCap` are exactly the same classes the pilot uses
(`pilot/classify_runner.py`) -- a re-run of this command is free for every
item already classified in a prior run (D22: "re-rendering never calls the
model again"), and a cost cap stops the run cleanly partway through rather
than overspending (issue #89: "Enforce a cost cap of $10").
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_health.benchmark_public.download import DownloadError, PinnedFile, fetch_pinned
from project_health.benchmark_public.loaders import get_loader
from project_health.benchmark_public.registry import DatasetSpec, Registry, load_registry
from project_health.benchmark_public.sampling import SamplingResult, stratified_sample
from project_health.classify.classifier import (
    ClassificationCache,
    ClassificationRecord,
    CostCap,
    JevClassifier,
    NormalizedMessage,
    ParentContext,
    RunResult,
)
from project_health.classify.preprocess import preprocess_text
from project_health.benchmark_public.mapping import LabelMappingSet, load_label_mapping

DEFAULT_CACHE_FILENAME = "jev_cache.jsonl"
DEFAULT_MANIFEST_FILENAME = "run_manifest.json"
# issue #89: "Enforce a cost cap of $10." Distinct from D10's ongoing monthly
# production cap -- this is a one-off benchmark run's own ceiling.
DEFAULT_MONTHLY_CAP_USD = 10.0


_MESSAGE_ID_PREFIX = "public_benchmark"


def message_id_for(dataset_id: str, item_id: str) -> str:
    """The `NormalizedMessage.message_id`/cache key for one dataset item --
    internal cache/join key only, never rendered in the public report
    (`report.py`'s leak test asserts this). `evaluate.py` uses the paired
    `item_id_from_message_id` to join a `ClassificationRecord` back to the
    `DatasetItem` (and its ground truth) it came from."""
    return f"{_MESSAGE_ID_PREFIX}:{dataset_id}:{item_id}"


def item_id_from_message_id(dataset_id: str, message_id: str) -> str | None:
    """Inverse of `message_id_for`, or `None` if `message_id` doesn't belong
    to `dataset_id` at all."""
    prefix = f"{_MESSAGE_ID_PREFIX}:{dataset_id}:"
    if not message_id.startswith(prefix):
        return None
    return message_id[len(prefix) :]


@dataclass(frozen=True)
class DatasetRunData:
    spec: DatasetSpec
    sampling: SamplingResult
    file_paths: tuple[Path, ...]


@dataclass(frozen=True)
class BenchmarkRunResult:
    datasets: dict[str, DatasetRunData]
    classification_by_message_id: dict[str, ClassificationRecord]
    run_result: RunResult
    elapsed_seconds: float
    cache_path: Path
    manifest: dict[str, Any]


def download_and_sample(
    registry: Registry,
    mapping_set: LabelMappingSet,
    cache_dir: str | Path,
    *,
    transport: Any = None,
) -> tuple[dict[str, DatasetRunData], list[dict[str, Any]]]:
    """Download + load + sample every `working` dataset. Returns
    `(dataset_run_data, blocked_report)` where `blocked_report` documents
    every `status: blocked` dataset (issue #89: report blocked datasets, never
    fabricate a loader for them) plus any `working` dataset whose download
    failed at run time despite the registry pin (network flake, upstream file
    moved since the pin was verified) -- that dataset is skipped for this run
    and reported the same way, rather than aborting the whole benchmark.
    """
    cache_dir = Path(cache_dir)
    dataset_dir = cache_dir / "datasets"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, DatasetRunData] = {}
    blocked: list[dict[str, Any]] = []

    for spec in registry.blocked():
        blocked.append(
            {
                "dataset_id": spec.id,
                "name": spec.name,
                "reason": spec.blocked_reason,
            }
        )

    for spec in registry.working():
        try:
            paths = tuple(
                fetch_pinned(
                    PinnedFile(url=f.url, sha256=f.sha256, filename=f.filename),
                    dataset_dir / spec.id,
                    transport=transport,
                )
                for f in spec.files
            )
        except DownloadError as exc:
            blocked.append(
                {
                    "dataset_id": spec.id,
                    "name": spec.name,
                    "reason": f"download failed at run time: {exc}",
                }
            )
            continue

        loader = get_loader(spec.loader)  # type: ignore[arg-type]
        items = loader(paths)
        mappings = mapping_set.for_dataset(spec.id)
        sampling = stratified_sample(
            spec.id,
            items,
            mappings,
            target_n=spec.target_n,  # type: ignore[arg-type]
            seed=spec.seed,  # type: ignore[arg-type]
        )
        results[spec.id] = DatasetRunData(spec=spec, sampling=sampling, file_paths=paths)

    return results, blocked


def _build_calls(
    dataset_run_data: dict[str, DatasetRunData],
) -> list[tuple[NormalizedMessage, ParentContext, str]]:
    """`[(message, context, message_id), ...]` for every sampled item across
    every dataset, text preprocessed exactly as the production pipeline would
    (`classify/preprocess.py`), tagged with the dataset's own `source_venue`.
    """
    calls: list[tuple[NormalizedMessage, ParentContext, str]] = []
    for dataset_id, run_data in dataset_run_data.items():
        venue = run_data.spec.source_venue_typed
        for item in run_data.sampling.items:
            message_id = message_id_for(dataset_id, item.item_id)
            text = preprocess_text(item.text, venue)
            parent_text = (
                preprocess_text(item.parent_text, venue) if item.parent_text is not None else None
            )
            message = NormalizedMessage(
                message_id=message_id,
                thread_id=message_id,  # each item is its own standalone unit, like the pilot
                source=venue,  # type: ignore[arg-type]
                text=text,
            )
            calls.append((message, ParentContext(text=parent_text), message_id))
    return calls


def run_benchmark(
    *,
    cache_dir: str | Path,
    registry_path: str | Path | None = None,
    mapping_path: str | Path | None = None,
    api_key: str | None = None,
    concurrency: int = 4,
    classifier_version: str = "1.0.0",
    monthly_cap_usd: float = DEFAULT_MONTHLY_CAP_USD,
    pricing_config_path: str | Path | None = None,
    cache_filename: str = DEFAULT_CACHE_FILENAME,
    manifest_filename: str = DEFAULT_MANIFEST_FILENAME,
    transport: Any = None,
    async_transport: Any = None,
    question_set: Any = None,
) -> BenchmarkRunResult:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    registry = load_registry(registry_path)
    mapping_set = load_label_mapping(mapping_path)

    dataset_run_data, blocked = download_and_sample(
        registry, mapping_set, cache_dir, transport=transport
    )

    calls = _build_calls(dataset_run_data)

    cache = ClassificationCache(cache_dir / cache_filename)
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

    start = time.monotonic()
    run_result = classifier.run([(m, c) for m, c, _mid in calls])
    elapsed_seconds = time.monotonic() - start

    classification_by_message_id = {record.message_id: record for record in run_result.records}

    manifest: dict[str, Any] = {
        "registry_version": registry.version,
        "mapping_version": mapping_set.version,
        "blocked_datasets": blocked,
        "datasets": {
            dataset_id: run_data.sampling.to_summary_dict()
            for dataset_id, run_data in dataset_run_data.items()
        },
        "classifier": {
            "status": run_result.status,
            "calls_made": run_result.calls_made,
            "cache_hits": run_result.cache_hits,
            "input_tokens_used": run_result.input_tokens_used,
            "output_tokens_used": run_result.output_tokens_used,
            "estimated_cost_usd": run_result.estimated_cost_usd,
            "elapsed_seconds": elapsed_seconds,
            "mean_latency_seconds_per_call": (
                elapsed_seconds / run_result.calls_made if run_result.calls_made else None
            ),
            "classifier_version": classifier.classifier_version,
            "question_set_version": classifier.question_set_version,
            "model_id_pinned": classifier.model_id,
        },
    }
    manifest_path = cache_dir / manifest_filename
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return BenchmarkRunResult(
        datasets=dataset_run_data,
        classification_by_message_id=classification_by_message_id,
        run_result=run_result,
        elapsed_seconds=elapsed_seconds,
        cache_path=cache.path,
        manifest=manifest,
    )
