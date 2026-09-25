"""Unit tests for project_health.provenance.manifest.build_manifest (issue #24)."""

from __future__ import annotations

from datetime import datetime, timezone

from project_health.provenance.manifest import build_manifest

UTC = timezone.utc
STARTED = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 9, 25, 6, 5, tzinfo=UTC)


def _base_kwargs(**overrides):
    kwargs = dict(
        run_id="run-1",
        trigger="manual",
        started_at=STARTED,
        completed_at=COMPLETED,
        pipeline_code_sha="abc1234",
        sources={},
        metrics_computed=["active_contributors_monthly@1.0"],
        data_branch_commit=None,
        site_deploy_status=None,
        status="ok",
    )
    kwargs.update(overrides)
    return kwargs


def test_metrics_missing_defaults_to_empty_list():
    manifest = build_manifest(**_base_kwargs())
    assert manifest["metrics_missing"] == []
    assert manifest["status"] == "ok"


def test_metrics_missing_is_written_verbatim_alongside_degraded_status():
    manifest = build_manifest(
        **_base_kwargs(status="degraded", metrics_missing=["active_contributors_monthly"])
    )
    assert manifest["status"] == "degraded"
    assert manifest["metrics_missing"] == ["active_contributors_monthly"]


def test_metrics_skipped_insufficient_data_is_unaffected_by_metrics_missing():
    """`metrics_skipped_insufficient_data` (a below-floor metric that still
    emits a row) and `metrics_missing` (a metric that emitted zero rows) are
    distinct signals; this project's engine never skips a metric for being
    below its floor, so the former stays `[]` regardless of the latter."""
    manifest = build_manifest(
        **_base_kwargs(status="degraded", metrics_missing=["stale_jira_rate"])
    )
    assert manifest["metrics_skipped_insufficient_data"] == []
