"""M0 metrics engine (issue #7): registry + DuckDB computation over normalized tables."""

from __future__ import annotations

from project_health.metrics.engine import DEFINITION_VERSION, compute_all
from project_health.metrics.registry import METRIC_IDS, build_registry

__all__ = ["compute_all", "DEFINITION_VERSION", "METRIC_IDS", "build_registry"]
