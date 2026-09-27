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
    compute_input_hash,
)
from project_health.classify.preprocess import preprocess_text
from project_health.classify.text_fetch import build_state
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


def _input_hash_for_call(
    message: NormalizedMessage, context: ParentContext, classifier: JevClassifier
) -> str:
    """The exact `input_hash` `JevClassifier` computes internally for this
    `(message, context)` pair (`classifier.classify`/`run_async`'s own
    `compute_input_hash(build_state(...), question_set_version, model_id)`
    call, duplicated here read-only so this module can look a record up from
    the cache by hash without needing a second, private entry point on
    `JevClassifier` itself)."""
    state = build_state(message.text, message.source, context.text)
    return compute_input_hash(state, classifier.question_set_version, classifier.model_id)


def _fan_out_by_input_hash(
    calls: list[tuple[NormalizedMessage, ParentContext, str]],
    cache: ClassificationCache,
    classifier: JevClassifier,
) -> dict[str, ClassificationRecord]:
    """`{message_id: ClassificationRecord}` for **every** call, joined by
    `input_hash` rather than by `ClassificationRecord.message_id`.

    Bug this fixes (orchestrator review of issue #89): several sampled items
    across datasets share byte-identical (text, parent, source) after
    preprocessing -- e.g. two different Ferreira comments that both read just
    "LGTM", or two ToxiCR rows with the same one-word review remark -- and so
    hash to the same `input_hash`. `JevClassifier`'s own cache is correctly
    keyed by `input_hash` (one record per distinct input, D22's whole point),
    but that one record's `.message_id` field can only ever hold the *first*
    item's id to reach that hash. Building `classification_by_message_id`
    from `RunResult.records` (keyed by `record.message_id`) therefore left
    every *other* item sharing that hash with no entry at all -- silently
    excluded from evaluation, no error, no count.

    The fix: for every call (every sampled item, duplicates included),
    recompute its own `input_hash` and look the record up directly from the
    cache. Every item that has a cached record (which, after a completed --
    not cost-cap-paused -- run, is every item) gets mapped to it, regardless
    of how many other items share that same hash.
    """
    result: dict[str, ClassificationRecord] = {}
    for message, context, message_id in calls:
        input_hash = _input_hash_for_call(message, context, classifier)
        record = cache.get(input_hash)
        if record is not None:
            result[message_id] = record
    return result


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

    # Fan out by input_hash, not by ClassificationRecord.message_id -- see
    # `_fan_out_by_input_hash`'s docstring for the bug this avoids (duplicate
    # inputs silently missing from evaluation).
    classification_by_message_id = _fan_out_by_input_hash(calls, cache, classifier)

    # message_id is "public_benchmark:<dataset_id>:<item_id>" -- group each
    # call back to its dataset by that prefix.
    calls_by_dataset: dict[str, list[tuple[NormalizedMessage, ParentContext, str]]] = {
        dataset_id: [] for dataset_id in dataset_run_data
    }
    for dataset_id in dataset_run_data:
        prefix = f"{_MESSAGE_ID_PREFIX}:{dataset_id}:"
        calls_by_dataset[dataset_id] = [c for c in calls if c[2].startswith(prefix)]

    dataset_manifest: dict[str, Any] = {}
    for dataset_id, run_data in dataset_run_data.items():
        dataset_calls = calls_by_dataset[dataset_id]
        distinct_inputs = {
            (m.text, c.text, m.source) for m, c, _mid in dataset_calls
        }
        n_evaluated = sum(1 for _m, _c, mid in dataset_calls if mid in classification_by_message_id)
        summary = run_data.sampling.to_summary_dict()
        summary["n_sampled"] = len(dataset_calls)
        summary["n_distinct_inputs"] = len(distinct_inputs)
        summary["n_evaluated"] = n_evaluated
        dataset_manifest[dataset_id] = summary

    manifest: dict[str, Any] = {
        "registry_version": registry.version,
        "mapping_version": mapping_set.version,
        "blocked_datasets": blocked,
        "datasets": dataset_manifest,
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
