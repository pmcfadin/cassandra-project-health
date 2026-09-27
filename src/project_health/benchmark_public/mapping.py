"""Label mapping loader/validator for `benchmark-public` (issue #89;
DECISIONS.md D23).

`label_mapping_v1.yaml` is the versioned file translating each dataset's own
label scheme onto this project's 12 message-level labels
(`classify/questions.py`'s `MESSAGE_LEVEL_LABELS`, COMMUNITY-HEALTH.md §1.2).
Each entry records:

- `our_label`: the COMMUNITY-HEALTH.md §1.2 label this maps to (or, for a
  handful of `synthetic: true` entries, a documented pseudo-label that is
  **ours**, not the dataset's own construct -- see ToxiCR's `any_negative`
  rollup below).
- `raw_field`/`positive_values`: how to compute ground truth from a loaded
  item's `DatasetItem.raw_labels` dict (`loaders.py`) -- `raw_labels[raw_
  field] in positive_values` is a positive example of `our_label` for this
  dataset.
- `strength`: `strong` or `partial` (`docs/plans/2026-09-27-public-benchmark-
  datasets.md` §2/§3's own per-dataset judgment, carried over verbatim where
  the issue's own text repeats it).
- `gating`: whether this mapping counts toward COMMUNITY-HEALTH.md §6.4's
  precision/recall/F1 gate, or is informative-only. A dataset whose own
  inter-rater agreement is below this project's alpha >= 0.667 usable floor
  (§6.3) -- Wikipedia's alpha ~= 0.45 is the one case here -- is always
  informative, never gating, regardless of mapping strength (D23).
- `notes`: the one-line caveat this mapping's row in the public report shows.

Every `our_label` that isn't `synthetic` must be one of the 12 message-level
labels Jev actually answers -- `evaluate.py` looks its predicted probability
up directly on the `ClassificationRecord`. A `synthetic` entry instead names
`source_labels` (a subset of the 12) that `evaluate.py` combines by taking
the max predicted probability across them (issue #89: "ToxiCR's 'toxic' is
broad... map it to hostility or to an 'any_negative' rollup (the max of the
negative labels). If you add a rollup, document it as ours" -- this project
adds it for ToxiCR specifically, as `any_negative_rollup`, always `gating:
false`).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from project_health.classify.questions import MESSAGE_LEVEL_LABELS

DEFAULT_MAPPING_PATH = Path(__file__).with_name("label_mapping_v1.yaml")

VALID_STRENGTHS: frozenset[str] = frozenset({"strong", "partial"})


class MappingError(ValueError):
    """Raised when `label_mapping_v1.yaml` fails validation."""


@dataclasses.dataclass(frozen=True)
class LabelMapping:
    dataset_id: str
    our_label: str
    raw_field: str
    positive_values: tuple[Any, ...]
    strength: str | None  # None only for a synthetic rollup
    gating: bool
    notes: str
    synthetic: bool = False
    source_labels: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class LabelMappingSet:
    version: int
    by_dataset: dict[str, tuple[LabelMapping, ...]]

    def for_dataset(self, dataset_id: str) -> tuple[LabelMapping, ...]:
        return self.by_dataset.get(dataset_id, ())


def load_label_mapping(path: str | Path | None = None) -> LabelMappingSet:
    resolved = Path(path) if path is not None else DEFAULT_MAPPING_PATH
    with open(resolved, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise MappingError(f"{resolved}: expected a YAML mapping at the top level")

    version = raw.get("version")
    if not isinstance(version, int):
        raise MappingError(f"{resolved}: 'version' must be an integer")

    mappings_raw = raw.get("mappings")
    if not isinstance(mappings_raw, dict) or not mappings_raw:
        raise MappingError(
            f"{resolved}: 'mappings' must be a non-empty mapping of dataset id -> list"
        )

    by_dataset: dict[str, tuple[LabelMapping, ...]] = {}
    for dataset_id, entries in mappings_raw.items():
        if not isinstance(entries, list) or not entries:
            raise MappingError(f"{resolved}: dataset {dataset_id!r} must have a non-empty list")
        parsed = tuple(_parse_entry(dataset_id, entry, resolved) for entry in entries)
        by_dataset[dataset_id] = parsed

    return LabelMappingSet(version=version, by_dataset=by_dataset)


def _parse_entry(dataset_id: str, entry: dict[str, Any], source_path: Path) -> LabelMapping:
    def require(key: str) -> Any:
        if key not in entry or entry[key] in (None, ""):
            raise MappingError(
                f"{source_path}: dataset {dataset_id!r} mapping entry is missing {key!r}: {entry!r}"
            )
        return entry[key]

    our_label = require("our_label")
    synthetic = bool(entry.get("synthetic", False))
    raw_field = require("raw_field")
    positive_values = tuple(require("positive_values"))
    gating = bool(require("gating"))
    notes = require("notes")

    if synthetic:
        source_labels = tuple(require("source_labels"))
        bad = [label for label in source_labels if label not in MESSAGE_LEVEL_LABELS]
        if bad:
            raise MappingError(
                f"{source_path}: dataset {dataset_id!r} synthetic label {our_label!r} "
                f"source_labels contains non-message-level label(s): {bad}"
            )
        if gating:
            raise MappingError(
                f"{source_path}: dataset {dataset_id!r} synthetic label {our_label!r} "
                "must have gating: false (a synthetic rollup is always informative-only)"
            )
        strength = entry.get("strength")
        return LabelMapping(
            dataset_id=dataset_id,
            our_label=our_label,
            raw_field=raw_field,
            positive_values=positive_values,
            strength=strength,
            gating=False,
            notes=notes,
            synthetic=True,
            source_labels=source_labels,
        )

    if our_label not in MESSAGE_LEVEL_LABELS:
        raise MappingError(
            f"{source_path}: dataset {dataset_id!r} maps to {our_label!r}, which is not one "
            f"of COMMUNITY-HEALTH.md §1.2's message-level labels (and synthetic is not set)"
        )
    strength = require("strength")
    if strength not in VALID_STRENGTHS:
        raise MappingError(
            f"{source_path}: dataset {dataset_id!r} label {our_label!r} has strength "
            f"{strength!r}, expected one of {sorted(VALID_STRENGTHS)}"
        )

    return LabelMapping(
        dataset_id=dataset_id,
        our_label=our_label,
        raw_field=raw_field,
        positive_values=positive_values,
        strength=strength,
        gating=gating,
        notes=notes,
    )


def is_positive(mapping: LabelMapping, raw_labels: dict[str, Any]) -> bool | None:
    """Whether `raw_labels` is a positive example of `mapping.our_label`, or
    `None` if `mapping.raw_field` isn't present in `raw_labels` at all, or is
    present but `None` (this item can't be scored for this label -- e.g. a
    field only some loaders populate, or a Ferreira message that was never
    topic-classified at all).

    A raw value that is itself a `set`/`frozenset` (e.g. Ferreira's
    `tbdf_categories` -- one message can carry several TBDF categories at
    once) is treated as positive if it **intersects** `positive_values`,
    rather than requiring the whole set to equal one listed value.
    """
    if mapping.raw_field not in raw_labels:
        return None
    value = raw_labels[mapping.raw_field]
    if value is None:
        return None
    if isinstance(value, (set, frozenset)):
        return bool(value & set(mapping.positive_values))
    return value in mapping.positive_values
