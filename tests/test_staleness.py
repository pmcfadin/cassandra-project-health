"""Tests for project_health.site.staleness (issue #86, extended by #170).

Direct, model-level unit tests -- `RunManifest.model_validate(...)` built
in-memory, no full site generation (`tests/test_site.py` already covers
`source_staleness_badge`/`source_staleness_badges` through the real page
build; these exercise the function in isolation, including the issue #170
`'not_applicable'` -> "n/a" badge the site generator never had before).
"""

from __future__ import annotations

from project_health.site.manifest import RunManifest
from project_health.site.staleness import source_staleness_badge, source_staleness_badges


def _manifest(sources: dict) -> RunManifest:
    return RunManifest.model_validate(
        {
            "run_id": "2026-10-12T000000Z-abc1234",
            "completed_at": "2026-10-12T00:05:00+00:00",
            "pipeline_code_sha": "abc1234",
            "sources": sources,
        }
    )


class TestNotApplicableBadge:
    """issue #170: a `not_applicable` source (a project's config has no
    matching section, or the source is deliberately `shared_sources:`)
    renders its own "n/a" badge -- distinct from both "fresh" (no badge)
    and "stale"/"failed" (a refreshed-on/failed-on badge)."""

    def test_renders_n_a_text(self):
        manifest = _manifest(
            {"jira": {"status": "not_applicable", "reason": "no issue_tracker: configured"}}
        )
        badge = source_staleness_badge("jira", manifest)
        assert badge is not None
        assert badge["text"] == "n/a"
        assert badge["status"] == "not_applicable"

    def test_last_good_snapshot_is_none(self):
        manifest = _manifest({"ponymail": {"status": "not_applicable", "reason": "shared"}})
        badge = source_staleness_badge("ponymail", manifest)
        assert badge["last_good_snapshot"] is None

    def test_uses_the_source_label(self):
        manifest = _manifest({"jira": {"status": "not_applicable", "reason": "x"}})
        badge = source_staleness_badge("jira", manifest)
        assert badge["label"]  # non-empty; exact text owned by metrics_meta.SOURCE_LABELS

    def test_included_in_source_staleness_badges(self):
        manifest = _manifest(
            {
                "git": {"status": "ok"},
                "jira": {"status": "not_applicable", "reason": "x"},
            }
        )
        badges = source_staleness_badges(("git", "jira"), manifest)
        assert [b["source"] for b in badges] == ["jira"]
        assert badges[0]["text"] == "n/a"


class TestExistingStatusesUnchanged:
    """Guard against the issue #170 change regressing #86's original
    behavior for the statuses it already covered."""

    def test_ok_source_has_no_badge(self):
        manifest = _manifest({"git": {"status": "ok"}})
        assert source_staleness_badge("git", manifest) is None

    def test_partial_source_has_no_badge(self):
        manifest = _manifest({"github_profile": {"status": "partial"}})
        assert source_staleness_badge("github_profile", manifest) is None

    def test_missing_source_has_no_badge(self):
        manifest = _manifest({"git": {"status": "ok"}})
        assert source_staleness_badge("jira", manifest) is None

    def test_failed_source_still_renders_failed_text(self):
        manifest = _manifest(
            {
                "jira": {
                    "status": "failed",
                    "reason": "503",
                    "last_good_snapshot": "2026-09-24T074929Z-371daf4",
                }
            }
        )
        badge = source_staleness_badge("jira", manifest)
        assert badge["status"] == "failed"
        assert "collection failed on" in badge["text"]
