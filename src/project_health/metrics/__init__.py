"""M0 metrics engine (issue #7): registry + DuckDB computation over normalized tables."""

from __future__ import annotations

from project_health.metrics.engine import DEFINITION_VERSIONS, compute_all
from project_health.metrics.pr_backlog import build_pr_backlog_registry, compute_pr_backlog
from project_health.metrics.registry import METRIC_IDS, build_registry
from project_health.metrics.review_responsiveness import compute_review_responsiveness

__all__ = [
    "compute_all",
    "compute_pr_backlog",
    "compute_review_responsiveness",
    "build_pr_backlog_registry",
    "DEFINITION_VERSIONS",
    "METRIC_IDS",
    "build_registry",
]
