"""Collector -> engine contract test (issue #24).

Golden tests for `metrics/engine.py` (`tests/test_metrics_engine.py`) always
go through `tests/fixtures/metrics/builders.py`, which builds
`contribution_event`/`review_event` rows directly rather than running the
real `GitCollector` -- so a mismatch between what `collectors/git.py` writes
into `event_type` and what `metrics/engine.py` filters on (issue #24: the
collector wrote `'code_commit'`, the engine filtered `'commit'`) can slip
past every golden test as long as the fixture builder's *own* default
happens to match one side or the other. It did: the builder defaulted to
`'commit'`, matching the engine, so every golden test passed while real data
silently produced zero rows for `active_contributors_monthly` and
`new_contributors_monthly`.

This test closes that gap: it runs the real `GitCollector` over the #3
fixture repo (`tests/fixtures/git/build_repo.py`), resolves identities the
same way the pipeline does, feeds the result through the real `compute_all`,
and asserts that every M0 metric with git-sourced inputs
(`active_contributors_monthly`, `new_contributors_monthly` from
`contribution_event`; `unique_reviewers_monthly`, `reviewer_hhi` from
`review_event`'s `commit_trailer` rows) actually yields at least one row with
real (`n >= 1`) data -- not just a dense, all-zero row.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from project_health.collectors.git import GitCollector
from project_health.config import load_project
from project_health.metrics.engine import compute_all
from project_health.normalize.identity import extract_raw_identifiers, resolve_identities
from project_health.schema import get_schema
from tests.fixtures.git.build_repo import build_repo

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_project(REPO_ROOT / "projects" / "cassandra.yaml")

UTC = timezone.utc
NOW = datetime(2026, 9, 25, tzinfo=UTC)
RUN_ID = "run-test-contract"

# The #3 fixture repo's commits span 2024-01 through 2024-08 (build_repo.py);
# any as_of after that keeps every one of those months "completed" (D5).
AS_OF = date(2024, 9, 15)

# M0 metrics whose qualifying activity comes from git-sourced rows: the
# first two from `contribution_event` (event_type = CODE_COMMIT), the last
# two from `review_event` rows the git collector emits from commit trailers
# (source = 'commit_trailer'). `median_resolution_latency_jira` and
# `stale_jira_rate` have no git inputs and are intentionally excluded.
GIT_SOURCED_METRICS = (
    "active_contributors_monthly",
    "new_contributors_monthly",
    "unique_reviewers_monthly",
    "reviewer_hhi",
)


@pytest.fixture
def repo(tmp_path) -> Path:
    repo_path = tmp_path / "repo"
    build_repo(repo_path)
    return repo_path


def test_every_git_sourced_metric_yields_at_least_one_row_with_real_data(repo):
    result = GitCollector().collect(
        repo_path=repo,
        repo_label="apache/cassandra",
        default_branch="trunk",
        watermark=None,
        bot_patterns=CONFIG.bot_patterns,
        source_snapshot_id=f"{RUN_ID}:git",
    )

    assert result.contribution_event.num_rows > 0
    assert result.review_event.num_rows > 0

    raw_identifiers = extract_raw_identifiers(
        contribution_events=result.contribution_event,
        review_events=result.review_event,
        issues=get_schema("issue").empty_table(),
    )
    resolution = resolve_identities(raw_identifiers, now=NOW)

    metrics_table = compute_all(
        {
            "contribution_event": result.contribution_event,
            "review_event": result.review_event,
            "issue": get_schema("issue").empty_table(),
            "identity_link": resolution.identity_link,
        },
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=NOW,
        config=CONFIG,
    )

    rows: list[dict] = metrics_table.to_pylist()

    for metric_id in GIT_SOURCED_METRICS:
        metric_rows = [r for r in rows if r["metric_id"] == metric_id]
        assert metric_rows, f"{metric_id}: expected at least one metric_value row, got none"
        assert any(r["n"] >= 1 for r in metric_rows), (
            f"{metric_id}: every emitted row had n == 0 -- the real GitCollector's output "
            f"never matched the engine's query (issue #24's failure mode)"
        )


def test_truck_factor_yields_a_row_with_real_data_from_the_real_collector(repo):
    """Issue #53's own version of this file's issue #24 regression check:
    `file_change_event` is a brand-new raw table with its own new fact-table
    fields (`change_type`, `file_path`) -- exactly the kind of
    collector/engine field-name or filter mismatch issue #24 warns about.
    This runs the real `GitCollector` (not the hand-built fixture in
    `tests/fixtures/metrics/builders.py`) over the #3 fixture repo and
    confirms `truck_factor` actually computes real (`n >= 1`) data from it.
    """
    result = GitCollector().collect(
        repo_path=repo,
        repo_label="apache/cassandra",
        default_branch="trunk",
        watermark=None,
        bot_patterns=CONFIG.bot_patterns,
        source_snapshot_id=f"{RUN_ID}:git",
    )
    assert result.file_change_event.num_rows > 0

    raw_identifiers = extract_raw_identifiers(
        contribution_events=result.contribution_event,
        review_events=result.review_event,
        issues=get_schema("issue").empty_table(),
    )
    resolution = resolve_identities(raw_identifiers, now=NOW)

    metrics_table = compute_all(
        {
            "contribution_event": result.contribution_event,
            "file_change_event": result.file_change_event,
            "review_event": result.review_event,
            "issue": get_schema("issue").empty_table(),
            "identity_link": resolution.identity_link,
        },
        as_of=AS_OF,
        run_id=RUN_ID,
        computed_at=NOW,
        config=CONFIG,
    )

    rows = [r for r in metrics_table.to_pylist() if r["metric_id"] == "truck_factor"]
    assert rows, "truck_factor: expected at least one metric_value row, got none"
    assert any(r["n"] >= 1 for r in rows), (
        "truck_factor: every emitted row had n == 0 -- the real GitCollector's "
        "file_change_event output never matched the engine's query"
    )


def test_contribution_event_type_from_real_collector_matches_engine_filter(repo):
    """Narrower regression pin for issue #24's exact bug: the `event_type`
    the real collector writes is the same string the engine filters
    `contribution_event` on, so this can never again drift silently."""
    from project_health.schema import CODE_COMMIT

    result = GitCollector().collect(
        repo_path=repo,
        repo_label="apache/cassandra",
        default_branch="trunk",
        watermark=None,
        bot_patterns=CONFIG.bot_patterns,
        source_snapshot_id=f"{RUN_ID}:git",
    )
    event_types = set(result.contribution_event.column("event_type").to_pylist())
    assert event_types == {CODE_COMMIT}
