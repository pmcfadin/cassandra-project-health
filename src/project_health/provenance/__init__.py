"""Run-manifest assembly + last-good-snapshot bookkeeping (ARCHITECTURE.md §5; issue #9)."""

from __future__ import annotations

from project_health.provenance.manifest import (
    build_manifest,
    read_last_good_snapshot,
    record_last_good_snapshot,
)

__all__ = ["build_manifest", "read_last_good_snapshot", "record_last_good_snapshot"]
