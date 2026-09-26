"""Typed loader for `scoring.yaml` (issue #57, DECISIONS.md D20).

`scoring.yaml` (repo root) is the versioned, binding source of every constant
the baseline-status math (`docs/spec/SCORING.md` §4-§5) and the composite
health score (D20, amending D4) use. This module is the *only* place that
parses that file's raw YAML shape into typed objects -- the rest of the
scoring engine (`baseline.py`, `dimension.py`, `composite.py`, `engine.py`)
works against `ScoringConfig`/`BaselineConfig`/`CompositeConfig`, never the
raw dict. Mirrors the split `governance/policy.py` already established for
`governance-policy.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SCORING_PATH = Path(__file__).resolve().parents[3] / "scoring.yaml"


class ScoringConfigError(ValueError):
    """Raised when `scoring.yaml` doesn't match the shape this loader
    expects (a missing required key, a non-numeric threshold, etc.) -- fails
    loudly rather than silently scoring against a malformed config."""


@dataclass(frozen=True)
class BaselineConfig:
    trailing_months: int
    min_completed_months: int
    stable_threshold: float
    large_deviation_threshold: float
    confirmation_window_months: int
    confirmation_required: int
    mad_zero_floor_default: float
    mad_zero_floors: dict[str, float] = field(default_factory=dict)
    target_range_declining_only: bool = True

    def mad_zero_floor(self, metric_id: str) -> float:
        """The minimum-meaningful-change floor used when a metric's trailing
        baseline has MAD = 0 (SCORING.md §4.2's division-by-zero fallback) --
        a per-metric override from `mad_zero_floors` if one is documented,
        else `mad_zero_floor_default`."""
        return self.mad_zero_floors.get(metric_id, self.mad_zero_floor_default)


@dataclass(frozen=True)
class NormalizationConfig:
    z_scale: float
    target_range_scale: float
    neutral_score: float


@dataclass(frozen=True)
class CompositeConfig:
    dimension_weights: dict[str, float]
    excluded_dimensions: dict[str, str]
    normalization: NormalizationConfig
    insufficient_data_handling: str


@dataclass(frozen=True)
class ScoringConfig:
    scoring_version: str
    baseline: BaselineConfig
    composite: CompositeConfig
    source_path: Path


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ScoringConfigError(f"scoring.yaml: missing required key {key!r} in {context}")
    return mapping[key]


def _as_float(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoringConfigError(f"scoring.yaml: {context} must be a number, got {value!r}")
    return float(value)


def _as_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScoringConfigError(f"scoring.yaml: {context} must be an integer, got {value!r}")
    return value


def load_scoring_config(path: str | Path = DEFAULT_SCORING_PATH) -> ScoringConfig:
    """Parse `scoring.yaml` into a typed `ScoringConfig`.

    Raises `ScoringConfigError` on a missing/malformed field, `FileNotFoundError`
    if `path` doesn't exist, and `yaml.YAMLError` on unparseable YAML.
    """
    resolved_path = Path(path)
    raw = yaml.safe_load(resolved_path.read_text())
    if not isinstance(raw, dict):
        raise ScoringConfigError(f"scoring.yaml: top level must be a mapping, got {type(raw)!r}")

    scoring_version = _require(raw, "scoring_version", "top level")
    if not isinstance(scoring_version, str) or not scoring_version:
        raise ScoringConfigError("scoring.yaml: scoring_version must be a non-empty string")

    baseline_raw = _require(raw, "baseline", "top level")

    def _baseline_int(key: str) -> int:
        return _as_int(_require(baseline_raw, key, "baseline"), f"baseline.{key}")

    def _baseline_float(key: str) -> float:
        return _as_float(_require(baseline_raw, key, "baseline"), f"baseline.{key}")

    baseline = BaselineConfig(
        trailing_months=_baseline_int("trailing_months"),
        min_completed_months=_baseline_int("min_completed_months"),
        stable_threshold=_baseline_float("stable_threshold"),
        large_deviation_threshold=_baseline_float("large_deviation_threshold"),
        confirmation_window_months=_baseline_int("confirmation_window_months"),
        confirmation_required=_baseline_int("confirmation_required"),
        mad_zero_floor_default=_baseline_float("mad_zero_floor_default"),
        mad_zero_floors={
            str(k): _as_float(v, f"baseline.mad_zero_floors[{k!r}]")
            for k, v in (baseline_raw.get("mad_zero_floors") or {}).items()
        },
        target_range_declining_only=bool(baseline_raw.get("target_range_declining_only", True)),
    )

    composite_raw = _require(raw, "composite", "top level")
    dimension_weights_raw = _require(composite_raw, "dimension_weights", "composite")
    dimension_weights = {
        str(dim): _as_float(weight, f"composite.dimension_weights[{dim!r}]")
        for dim, weight in dimension_weights_raw.items()
    }
    if not dimension_weights:
        raise ScoringConfigError("scoring.yaml: composite.dimension_weights must not be empty")

    normalization_raw = _require(composite_raw, "normalization", "composite")

    def _normalization_float(key: str) -> float:
        return _as_float(_require(normalization_raw, key, "composite.normalization"), key)

    normalization = NormalizationConfig(
        z_scale=_normalization_float("z_scale"),
        target_range_scale=_normalization_float("target_range_scale"),
        neutral_score=_normalization_float("neutral_score"),
    )

    composite = CompositeConfig(
        dimension_weights=dimension_weights,
        excluded_dimensions={
            str(k): str(v) for k, v in (composite_raw.get("excluded_dimensions") or {}).items()
        },
        normalization=normalization,
        insufficient_data_handling=str(composite_raw.get("insufficient_data_handling", "")),
    )

    return ScoringConfig(
        scoring_version=scoring_version,
        baseline=baseline,
        composite=composite,
        source_path=resolved_path,
    )
