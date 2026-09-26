"""Dimension status: the worst-key-metric rule (`docs/spec/SCORING.md` §5.3-
§5.4, issue #57).

A dimension's status is never an average or a vote across its member
metrics' statuses -- that would recreate the composite-score failure mode
D4/SCORING.md §2 rejects, one level down. Only `key`-role metrics (SCORING.md
§5.3, `scoring/registry.py`) can set a dimension's status:

- `declining` if any key metric (with a real, non-`insufficient_data`
  status) is `declining` -- regardless of how many other key or supporting
  metrics look fine.
- `improving` only if no key metric is `declining` and at least one key
  metric is `improving`.
- `stable` otherwise, when at least one key metric has a real status.
- `insufficient_data` if every key metric in the dimension is
  `insufficient_data` (or the dimension has no key metrics with data at all).
"""

from __future__ import annotations

from dataclasses import dataclass

from project_health.scoring.baseline import BaselineStatusResult


@dataclass(frozen=True)
class DimensionStatusResult:
    dimension: str
    status: str  # 'improving' | 'stable' | 'declining' | 'insufficient_data'
    driven_by: tuple[str, ...]  # key metric_id(s) that set `status` ('declining'/'improving' only)


def compute_dimension_status(
    dimension: str, key_metric_statuses: dict[str, BaselineStatusResult]
) -> DimensionStatusResult:
    """`key_metric_statuses` maps a dimension's key metric_ids to their
    `BaselineStatusResult` for the current month -- a metric with no result
    at all this run (e.g. not yet implemented, like `release_frequency`
    today) should simply be omitted from this mapping by the caller, exactly
    like an `insufficient_data` entry would be treated here.
    """
    real = {
        metric_id: result
        for metric_id, result in key_metric_statuses.items()
        if result.status != "insufficient_data"
    }
    if not real:
        return DimensionStatusResult(dimension=dimension, status="insufficient_data", driven_by=())

    declining = tuple(sorted(m for m, r in real.items() if r.status == "declining"))
    if declining:
        return DimensionStatusResult(dimension=dimension, status="declining", driven_by=declining)

    improving = tuple(sorted(m for m, r in real.items() if r.status == "improving"))
    if improving:
        return DimensionStatusResult(dimension=dimension, status="improving", driven_by=improving)

    return DimensionStatusResult(dimension=dimension, status="stable", driven_by=())
