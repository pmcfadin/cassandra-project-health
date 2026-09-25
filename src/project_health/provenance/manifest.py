"""Run-manifest assembly and last-good-snapshot bookkeeping.

ARCHITECTURE.md §5 (provenance model / run manifest) and §7.3 (partial
failure — "a failed source never publishes stale-as-fresh").

Two things live here:

- `build_manifest` assembles the plain-dict JSON structure `pipeline.py`
  writes to `manifests/<run_id>.json`, matching §5's example shape
  (`run_id`, `trigger`, `started_at`/`completed_at`, `pipeline_code_sha`,
  `sources`, `metrics_computed`, ...). This is the *writer*; the site
  generator (#8) reads the same file back through
  `project_health.site.manifest.load_manifest`. The two modules don't import
  each other beyond that loader — `tests/test_pipeline.py` proves a manifest
  built here loads cleanly through it.
- `record_last_good_snapshot` / `read_last_good_snapshot` track, per source,
  the `run_id` of that source's most recent *successful* collection. When a
  source fails after retries, `pipeline.py` looks this up so the manifest's
  `sources.<source>.last_good_snapshot` can point at the last run whose raw
  data for that source is trustworthy (§7.3) — the site then renders a
  staleness badge sourced from exactly that field, never silently treating
  stale data as fresh.

Neither function here decides *what* counts as success/failure — that's
`pipeline.py`'s job, per source. This module only assembles/persists the
resulting record.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def build_manifest(
    *,
    run_id: str,
    trigger: str,
    started_at: datetime,
    completed_at: datetime | None,
    pipeline_code_sha: str,
    sources: dict[str, dict[str, Any]],
    metrics_computed: list[str],
    data_branch_commit: str | None,
    site_deploy_status: str | None,
    status: str,
    error: str | None = None,
    metrics_missing: list[str] | None = None,
) -> dict[str, Any]:
    """Build the run-manifest dict (ARCHITECTURE.md §5).

    `status` ('ok' | 'degraded' | 'failed') and `error` are this pipeline's
    own top-level run-outcome fields, additional to the illustrative §5
    example — they're what makes a metrics-stage failure (§7.3: "the site is
    not redeployed") visible directly in the committed manifest, not only via
    the CLI's exit code. `project_health.site.manifest.RunManifest` accepts
    (and ignores) any field it doesn't model, so adding these never breaks
    that loader.

    `metrics_missing` (issue #24) lists every *registered*
    (`metrics.registry.METRIC_IDS`) metric that produced zero `metric_value`
    rows this run -- e.g. a contract mismatch between a collector and the
    engine silently zeroing a metric out, the failure mode issue #24 fixes.
    A metric with data but below its sample floor is not "missing": it still
    emits a `flag='insufficient_data'` row (METRICS.md §0.6) and is never
    listed here. Callers pass `status='degraded'` whenever `metrics_missing`
    is non-empty; this function doesn't compute `status` itself, only
    defaults `metrics_missing` to `[]` and writes whatever `status` it's
    given.

    Every M0 metric emits one row per window even when that window is below
    its sample floor (`flag='insufficient_data'`, METRICS.md §0.6) — a
    metric is never entirely skipped for that reason, so
    `metrics_skipped_insufficient_data` is always `[]` here; it's still
    written for schema parity with §5's example shape.
    """
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "trigger": trigger,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat() if completed_at else None,
        "pipeline_code_sha": pipeline_code_sha,
        "sources": sources,
        "metrics_computed": metrics_computed,
        "metrics_skipped_insufficient_data": [],
        "metrics_missing": list(metrics_missing) if metrics_missing else [],
        "data_branch_commit": data_branch_commit,
        "site_deploy_status": site_deploy_status,
        "status": status,
    }
    if error is not None:
        manifest["error"] = error
    return manifest


def _last_good_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "state" / "last_good_runs.json"


def read_last_good_snapshot(data_dir: str | Path, source: str) -> str | None:
    """The `run_id` of `source`'s most recent successful collection, or `None`."""
    path = _last_good_path(data_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text()).get(source)


def record_last_good_snapshot(data_dir: str | Path, source: str, run_id: str) -> None:
    """Record `run_id` as `source`'s most recent successful collection.

    Read-modify-write against `state/last_good_runs.json`, the same shape as
    `project_health.storage.write_watermark`'s `state/watermarks.json`, so
    recording one source's snapshot never disturbs another's.
    """
    path = _last_good_path(data_dir)
    data: dict[str, str] = {}
    if path.is_file():
        data = json.loads(path.read_text())
    data[source] = run_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
