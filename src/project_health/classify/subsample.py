"""Phase 2a gap-focused pilot subsampler (issue #90; DECISIONS.md D18, D22, D23).

`project-health pilot-subsample` draws a seeded, stratified subset of an
existing pilot corpus v0 (`classify/sample.py`'s `run_pilot_sample` output)
to build corpus v1: a smaller (default 80-item) "Cassandra-domain sample"
that D23 substitutes for D18's full 250-message owner pilot. Nothing is
re-fetched here -- every v1 row is copied byte-for-byte (text, parent_text,
checksum, archive_url, year, ...) from an existing v0 row; this module only
decides *which* v0 rows make the cut and in what presentation order they're
written.

## Why a subset, not a fresh sample (D23)

D23 narrows the owner/rater pilot to "a Cassandra-domain sample of 50-100
messages... [covering] the labels with no public ground truth, plus a quick
check that the public-data results hold on Cassandra's own venues" -- the six
gap labels (`evidence_based_argument`, `compromise_offer`, `acknowledgment`,
`resolution_marker`, `gatekeeping`, `status_authority_invocation`, per
`label.label_set.GAP_LABEL_IDS`) plus a lighter pass over the six labels the
public benchmark already covers well or partially (`label.label_set.
QUICK_CHECK_LABEL_IDS`). Re-sampling from scratch would mean a second full
dev@/JIRA scan; subsetting the already-frozen, already-fetched v0 corpus
(`classify/sample.py`) avoids that and keeps v1 a strict subset of a corpus
that has already been through v0's own eligibility/automated-sender/English
filtering.

## Keeping enrichment useful for the labels that actually have signal

v0's own enrichment stratum (`classify/sample.py`'s `RARE_LABELS`, drawn from
`classify/enrichment_filters_v2.yaml`'s keyword pre-filter) was built for
five labels: `personal_attack`, `gatekeeping`, `dismissiveness`,
`status_authority_invocation`, `sarcasm`. Of D23's six gap labels, only two
-- `gatekeeping` and `status_authority_invocation` -- overlap with that
pre-filter; the other four (`evidence_based_argument`, `compromise_offer`,
`acknowledgment`, `resolution_marker`) have no pre-filter signal in v0 at all
(D18 predates D23's gap-label taxonomy), so this module has no v0 metadata to
specially target them by -- subsetting can only preserve v0's overall
prevalence/enrichment proportions for those four, never further enrich them.

For the two gap labels the pre-filter *does* cover, this module re-applies
`classify.sample.enrichment_hits` to each v0 enrichment row's own
(already-fetched, already-preprocessed) `text`/`parent_text` -- deterministic
and side-effect-free, so it reproduces exactly the hit set v0's own scan
would have recorded for that row, even though v0 never persisted a per-item
"which label(s) matched" field. Rows hitting either label are reserved a
floor of the subsample's enrichment allocation (capped by availability and by
the enrichment stratum's own share of `size`); the remainder of the
enrichment slice is filled from the other v0 enrichment rows.

## Determinism

Every selection step reuses `classify.sample`'s existing `_stable_rank`/
`deterministic_sample` machinery (sort-by-stable-hash, never `random.sample`),
under namespaces this module owns (`"subsample:..."`) so a re-run with the
same `(seed, size)` against the same v0 file always yields the same v1 file,
byte for byte, and never collides with v0's own selection/shuffle
namespaces. Presentation order is re-shuffled the same way v0's own
`shuffle_presentation_order` blinds stratum membership (issue #44 fix round
3) -- a v1 rater must not be able to tell which items came from which v0
stratum, or which subsampling bucket, by position in the file either.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_health.classify.sample import (
    RARE_LABELS,
    EnrichmentFilterMap,
    _stable_rank,
    allocate_with_capacity,
    deterministic_sample,
    enrichment_hits,
    load_enrichment_filters,
)
from project_health.label.label_set import GAP_LABEL_IDS
from project_health.label.store import CorpusError

DEFAULT_SUBSAMPLE_SIZE = 80
# Issue #90's own number -- a convenient, memorable default seed, not a
# claim that it's special. Override with --seed for a different draw.
DEFAULT_SUBSAMPLE_SEED = 90

DEFAULT_ENRICHMENT_FILTERS_PATH = Path(__file__).with_name("enrichment_filters_v2.yaml")

# Of D23's six gap labels, only these two have a v0 keyword pre-filter
# (classify/enrichment_filters_v2.yaml) to re-check against -- see module
# docstring.
GAP_LABELS_WITH_ENRICHMENT_SIGNAL: tuple[str, ...] = tuple(
    label for label in RARE_LABELS if label in GAP_LABEL_IDS
)
GAP_LABELS_WITHOUT_ENRICHMENT_SIGNAL: tuple[str, ...] = tuple(
    label for label in GAP_LABEL_IDS if label not in GAP_LABELS_WITH_ENRICHMENT_SIGNAL
)

_REQUIRED_ROW_FIELDS = ("id", "stratum", "source", "archive_url", "text", "parent_text")


def read_corpus_rows(path: str | Path) -> list[dict[str, Any]]:
    """Read a corpus JSONL as raw dicts, preserving every field exactly as
    written (including fields `label.store.load_corpus` doesn't care about,
    e.g. `year`) -- unlike `label.store.load_corpus`, which parses into a
    fixed `CorpusItem` shape and would silently drop anything else. Validates
    only what this module actually needs: valid JSON objects, the required
    labeler-schema fields present, and no duplicate ids.
    """
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise CorpusError(f"corpus line {line_no}: invalid JSON ({exc.msg})") from exc
            if not isinstance(record, dict):
                raise CorpusError(f"corpus line {line_no}: expected a JSON object")
            missing = [field for field in _REQUIRED_ROW_FIELDS if field not in record]
            if missing:
                raise CorpusError(f"corpus line {line_no}: missing field(s) {missing}")
            row_id = str(record["id"])
            if row_id in seen_ids:
                raise CorpusError(f"corpus line {line_no}: duplicate id {row_id!r}")
            seen_ids.add(row_id)
            rows.append(record)
    if not rows:
        raise CorpusError(f"{path}: source corpus is empty")
    return rows


@dataclass(frozen=True)
class SubsampleSelection:
    selected_ids: list[str]
    stats: dict[str, Any]


def select_subsample(
    seed: int,
    rows: list[dict[str, Any]],
    enrichment_filter_map: EnrichmentFilterMap,
    size: int = DEFAULT_SUBSAMPLE_SIZE,
) -> SubsampleSelection:
    """Select `min(size, len(rows))` row ids out of `rows` (raw v0 corpus
    dicts), preserving v0's prevalence/enrichment proportions while reserving
    enough of the enrichment slice for `GAP_LABELS_WITH_ENRICHMENT_SIGNAL`
    hits (module docstring). Returns ids in an arbitrary (not yet shuffled --
    see `shuffle_presentation_order`) order, plus counts-only stats for the
    manifest -- never row text or extra ids beyond what's selected.
    """
    if size <= 0:
        raise ValueError("size must be positive")

    prevalence_rows = [r for r in rows if r.get("stratum") == "prevalence"]
    enrichment_rows = [r for r in rows if r.get("stratum") == "enrichment"]

    target = min(size, len(rows))
    weights = {
        "prevalence": float(len(prevalence_rows)),
        "enrichment": float(len(enrichment_rows)),
    }
    capacity = {"prevalence": len(prevalence_rows), "enrichment": len(enrichment_rows)}
    stratum_alloc = (
        allocate_with_capacity(target, weights, capacity)
        if sum(capacity.values()) > 0
        else {"prevalence": 0, "enrichment": 0}
    )

    prevalence_ids = deterministic_sample(
        seed,
        "subsample:prevalence",
        [str(r["id"]) for r in prevalence_rows],
        stratum_alloc["prevalence"],
    )

    # Re-derive each enrichment row's gap-label hits from its own already-
    # fetched text -- deterministic, no network, no re-fetch (module
    # docstring). Computed once per row so the same hit set backs both
    # selection and the manifest's candidate counts.
    hits_by_id: dict[str, frozenset[str]] = {
        str(row["id"]): enrichment_hits(
            row.get("text", ""), enrichment_filter_map, row.get("parent_text")
        )
        for row in enrichment_rows
    }
    gap_signal = set(GAP_LABELS_WITH_ENRICHMENT_SIGNAL)
    gap_hit_ids = [rid for rid, hits in hits_by_id.items() if hits & gap_signal]
    non_gap_ids = [rid for rid, hits in hits_by_id.items() if not (hits & gap_signal)]

    enrichment_budget = stratum_alloc["enrichment"]
    gap_reserved = min(len(gap_hit_ids), enrichment_budget)
    selected_gap_ids = deterministic_sample(
        seed, "subsample:enrichment:gap", gap_hit_ids, gap_reserved
    )
    remaining = enrichment_budget - len(selected_gap_ids)
    selected_non_gap_ids = deterministic_sample(
        seed, "subsample:enrichment:nongap", non_gap_ids, remaining
    )
    enrichment_ids = selected_gap_ids + selected_non_gap_ids
    selected_ids = prevalence_ids + enrichment_ids

    selected_gap_set = set(selected_gap_ids)
    candidates_in_source = {
        label: sum(1 for hits in hits_by_id.values() if label in hits)
        for label in GAP_LABELS_WITH_ENRICHMENT_SIGNAL
    }
    selected_counts_by_label = {
        label: sum(1 for rid in selected_gap_set if label in hits_by_id[rid])
        for label in GAP_LABELS_WITH_ENRICHMENT_SIGNAL
    }

    stats: dict[str, Any] = {
        "requested_size": size,
        "achieved_size": len(selected_ids),
        "source_counts": {
            "prevalence": len(prevalence_rows),
            "enrichment": len(enrichment_rows),
            "total": len(rows),
        },
        "selected_counts": {
            "prevalence": len(prevalence_ids),
            "enrichment": len(enrichment_ids),
            "enrichment_gap_hit": len(selected_gap_ids),
            "enrichment_non_gap": len(selected_non_gap_ids),
        },
        "gap_label_enrichment_signal": {
            "labels_with_v0_prefilter_signal": list(GAP_LABELS_WITH_ENRICHMENT_SIGNAL),
            "labels_with_no_v0_prefilter_signal": list(GAP_LABELS_WITHOUT_ENRICHMENT_SIGNAL),
            "candidates_in_source": candidates_in_source,
            "selected": selected_counts_by_label,
        },
    }
    return SubsampleSelection(selected_ids=selected_ids, stats=stats)


def shuffle_presentation_order(seed: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder `rows` into v1's final presentation order -- the same
    stable-hash mechanism `classify.sample.shuffle_presentation_order` uses
    for v0, under a namespace this module owns so it never entangles with (or
    leaks anything about) v0's own selection/shuffle draws. Deterministic for
    a given `seed`.
    """
    return sorted(
        rows, key=lambda row: _stable_rank(seed, "subsample_presentation_order", str(row["id"]))
    )


@dataclass(frozen=True)
class PilotSubsampleResult:
    row_ids: list[str]  # in v1 presentation order
    manifest: dict[str, Any]
    corpus_path: Path
    manifest_path: Path


def run_pilot_subsample(
    *,
    source_corpus_path: str | Path,
    seed: int,
    size: int,
    corpus_output_path: str | Path,
    manifest_output_path: str | Path,
    enrichment_filters_path: str | Path | None = None,
) -> PilotSubsampleResult:
    """End-to-end subsampler orchestration, used by `project-health
    pilot-subsample` and directly by tests. Reads `source_corpus_path`
    (corpus v0), selects and shuffles a `size`-item subset, writes it as
    corpus v1 JSONL (labeler schema, byte-identical rows) plus a counts-only
    manifest to `corpus_output_path`/`manifest_output_path` -- but never
    decides where those paths live; the CLI points them at a private
    benchmark repo clone (D18), same as `classify.sample.run_pilot_sample`.
    """
    source_path = Path(source_corpus_path)
    rows = read_corpus_rows(source_path)

    filters_path = enrichment_filters_path or DEFAULT_ENRICHMENT_FILTERS_PATH
    filter_map = load_enrichment_filters(filters_path)

    selection = select_subsample(seed, rows, filter_map, size=size)
    rows_by_id = {str(r["id"]): r for r in rows}
    selected_rows = [rows_by_id[rid] for rid in selection.selected_ids]
    ordered_rows = shuffle_presentation_order(seed, selected_rows)

    corpus_path = Path(corpus_output_path)
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    with corpus_path.open("w", encoding="utf-8") as fh:
        for row in ordered_rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    corpus_checksum = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    source_checksum = hashlib.sha256(source_path.read_bytes()).hexdigest()

    counts_by_stratum_source: dict[str, int] = {}
    for row in ordered_rows:
        key = f"{row.get('stratum')}/{row.get('source')}"
        counts_by_stratum_source[key] = counts_by_stratum_source.get(key, 0) + 1

    manifest: dict[str, Any] = {
        "corpus_version": "v1",
        "source_corpus_version": "v0",
        "source_corpus_path": str(source_path),
        "source_corpus_checksum_sha256": source_checksum,
        "seed": seed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_size": size,
        "achieved_size": len(ordered_rows),
        "counts_by_stratum_source": counts_by_stratum_source,
        "selection": selection.stats,
        "corpus_checksum_sha256": corpus_checksum,
    }
    manifest_path = Path(manifest_output_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return PilotSubsampleResult(
        row_ids=[str(r["id"]) for r in ordered_rows],
        manifest=manifest,
        corpus_path=corpus_path,
        manifest_path=manifest_path,
    )
