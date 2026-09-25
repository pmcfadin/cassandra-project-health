"""Typed loader for `manifests/<run_id>.json` (ARCHITECTURE.md §5).

The site generator (#8) is this manifest's first reader; the metrics/
provenance task (#9) is its writer. This module is the one place that pins
down which manifest fields the site depends on and their shape, so #9's
writer and this reader agree on the contract without either reading the
other's code — the same "single accepted parsing" discipline `schema/`
applies to the Parquet tables.

Only the fields the site actually renders are modeled here:

- `run_id`, `completed_at` — the freshness banner (ARCHITECTURE.md §7.1
  mitigation 2) compares `completed_at` against build time.
- `pipeline_code_sha` — footer attribution (ARCHITECTURE.md §5).
- `sources.<name>.status` / `.last_good_snapshot` — per-source staleness
  badges (ARCHITECTURE.md §7.3).

Every other manifest field (`trigger`, `started_at`, `metrics_computed`,
`data_branch_commit`, ...) is accepted but ignored, not modeled, so a
future manifest key never breaks this loader.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class SourceStatus(BaseModel):
    """One entry in manifest `sources.<name>` (ARCHITECTURE.md §5, §7.3)."""

    model_config = ConfigDict(extra="allow")

    # status: 'ok' | 'stale' | 'failed'
    status: str
    last_good_snapshot: str | None = None
    reason: str | None = None


class RunManifest(BaseModel):
    """The subset of `manifests/<run_id>.json` the site generator reads."""

    model_config = ConfigDict(extra="allow")

    run_id: str
    completed_at: datetime | None = None
    pipeline_code_sha: str
    sources: dict[str, SourceStatus] = {}


def manifest_path(data_dir: str | Path, run_id: str) -> Path:
    """Path of the run manifest for `run_id` under `data_dir`."""
    return Path(data_dir) / "manifests" / f"{run_id}.json"


def load_manifest(data_dir: str | Path, run_id: str) -> RunManifest:
    """Load and validate `manifests/<run_id>.json` under `data_dir`.

    Raises `FileNotFoundError` if the manifest doesn't exist yet, and
    `pydantic.ValidationError` if it's missing a field this loader depends
    on (`run_id`, `pipeline_code_sha`).
    """
    path = manifest_path(data_dir, run_id)
    if not path.is_file():
        raise FileNotFoundError(f"run manifest not found: {path}")
    raw = json.loads(path.read_text())
    return RunManifest.model_validate(raw)
