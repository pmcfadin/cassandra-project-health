"""Tests for project_health.pipeline (issue #9).

End-to-end over the #3 fixtures: the git fixture repo
(tests/fixtures/git/build_repo.py), used as an offline "clone" by giving it
a self-referential `origin` remote so `clone_or_fetch`'s `git fetch` only
ever touches the local filesystem, and JIRA's `tests/fixtures/jira/*.json`
pages served through an injected `httpx.MockTransport`. Nothing here hits
the network.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from project_health import storage
from project_health.collectors.jira import JiraCollector
from project_health.config import load_project
from project_health.pipeline import (
    _dedupe_issue_rows,
    _dedupe_jira_review_events,
    run_pipeline,
)
from project_health.schema import get_schema, validate
from project_health.site.manifest import load_manifest
from tests.fixtures.git.build_repo import build_repo

JIRA_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "jira"


def _load_jira_fixture(name: str) -> dict:
    return json.loads((JIRA_FIXTURES_DIR / name).read_text())


PAGE_1 = _load_jira_fixture("search_with_reviewers.json")
PAGE_2 = _load_jira_fixture("search_with_reviewers_page2.json")
EMPTY_PAGE = {"expand": "schema,names", "startAt": 0, "maxResults": 5, "total": 0, "issues": []}

NOW = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)


def _paginated_transport(pages: dict[int, dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        start_at = int(query.get("startAt", ["0"])[0])
        if start_at not in pages:
            raise AssertionError(f"unexpected startAt={start_at}")
        return httpx.Response(200, json=pages[start_at])

    return httpx.MockTransport(handler)


def _always_503_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    return httpx.MockTransport(handler)


def _jira_factory(transport: httpx.MockTransport, max_retries: int = 2):
    def factory(config):
        return JiraCollector(
            config,
            transport=transport,
            page_size=5,
            max_retries=max_retries,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )

    return factory


@pytest.fixture
def config():
    return load_project("projects/cassandra.yaml")


@pytest.fixture
def git_workdir(tmp_path):
    repo = tmp_path / "repo"
    build_repo(repo)
    # A self-referential `origin` remote so `clone_or_fetch`'s `git fetch`
    # succeeds against the local filesystem -- this test suite never
    # touches the real network.
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(repo)],
        check=True,
        capture_output=True,
    )
    return repo


# --- End-to-end -------------------------------------------------------------


class TestEndToEnd:
    def test_produces_raw_partitions_snapshot_manifest_and_site(
        self, tmp_path, config, git_workdir
    ):
        data_dir = tmp_path / "data"
        site_out = tmp_path / "site"
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            site_out=site_out,
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport),
        )

        assert result.exit_code == 0
        assert result.manifest["sources"]["git"]["status"] == "ok"
        assert result.manifest["sources"]["jira"]["status"] == "ok"
        # 15 non-merge commits in the #3 fixture repo, 1 of them a bot
        # (github-actions[bot], excluded by projects/cassandra.yaml's
        # bot_patterns) -> 14 real contributions.
        assert result.manifest["sources"]["git"]["records_collected"] == 14
        assert result.manifest["sources"]["jira"]["records_collected"] == 10
        # Data-quality signal (issue #18): the #3 fixture repo has no
        # placeholder-reviewer ("TBD"/"none"/"n/a") trailers, so this is 0
        # here -- the field's presence/wiring is what's under test.
        assert result.manifest["sources"]["git"]["placeholder_reviewer_commits"] == 0

        # raw partitions
        assert list((data_dir / "raw" / "git" / "contribution_event").glob("date=*/part-*.parquet"))
        assert list((data_dir / "raw" / "git" / "review_event").glob("date=*/part-*.parquet"))
        assert list((data_dir / "raw" / "jira" / "issue").glob("date=*/part-*.parquet"))
        assert list((data_dir / "raw" / "jira" / "review_event").glob("date=*/part-*.parquet"))

        # snapshot
        snapshot_path = data_dir / "snapshots" / result.run_id / "metrics.parquet"
        assert snapshot_path.is_file()
        metrics_table = validate("metric_value", pq.read_table(snapshot_path))
        assert metrics_table.num_rows > 0

        # manifest -- and it must load cleanly through the site's own loader
        assert result.manifest_path.is_file()
        loaded = load_manifest(data_dir, result.run_id)
        assert loaded.run_id == result.run_id
        assert loaded.pipeline_code_sha == "abc1234"
        assert loaded.sources["git"].status == "ok"
        assert loaded.sources["jira"].status == "ok"

        # site
        assert (site_out / "index.html").is_file()
        assert result.manifest["site_deploy_status"] == "ok"

    def test_placeholder_reviewer_commits_surfaces_in_manifest(
        self, tmp_path, config, git_workdir
    ):
        """A commit whose trailer names only a placeholder reviewer (issue
        #18: `TBD`/`none`/`n/a`/...) emits no review_event row, but the
        count is still a data-quality signal worth surfacing in the
        manifest rather than silently dropped."""
        file_path = git_workdir / "placeholder_test.txt"
        file_path.write_text("content\n")
        subprocess.run(
            ["git", "-C", str(git_workdir), "add", "placeholder_test.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(git_workdir),
                "commit",
                "-m",
                "patch by Test Author; reviewed by TBD for CASSANDRA-999",
            ],
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Test Author",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test Author",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
            check=True,
            capture_output=True,
        )

        data_dir = tmp_path / "data"
        transport = _paginated_transport({0: EMPTY_PAGE})

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport),
        )

        assert result.manifest["sources"]["git"]["placeholder_reviewer_commits"] == 1

    def test_run_id_format_and_watermarks_advance(self, tmp_path, config, git_workdir):
        data_dir = tmp_path / "data"
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            now=NOW,
            code_sha="abc1234567",
            jira_collector_factory=_jira_factory(transport),
        )

        assert result.run_id == "2026-09-25T060000Z-abc1234"

        head_sha = subprocess.run(
            ["git", "-C", str(git_workdir), "rev-parse", "trunk"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert storage.read_watermark(data_dir, "git") == head_sha
        assert storage.read_watermark(data_dir, "jira") is not None


# --- Reproducibility ---------------------------------------------------------


class TestReproducibility:
    def test_second_run_with_no_new_data_adds_no_rows_and_matches_metrics(
        self, tmp_path, config, git_workdir
    ):
        data_dir = tmp_path / "data"
        transport_1 = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})

        first = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport_1),
        )
        assert first.exit_code == 0

        contribution_before = storage.read_table(data_dir, "git", "contribution_event").num_rows
        issue_before = storage.read_table(data_dir, "jira", "issue").num_rows

        # Second run: no new commits (git watermark is already at HEAD) and
        # JIRA returning nothing new -- a realistic "nothing changed since
        # last run" pass. Same UTC date as the first run (only the hour
        # differs) so `as_of` -- and therefore every window boundary -- is
        # identical, while the run_id still differs.
        transport_2 = _paginated_transport({0: EMPTY_PAGE})
        second = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            now=NOW.replace(hour=7),
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport_2),
        )
        assert second.exit_code == 0
        assert second.run_id != first.run_id
        assert second.manifest["sources"]["git"]["records_collected"] == 0
        assert second.manifest["sources"]["jira"]["records_collected"] == 0

        contribution_after = storage.read_table(data_dir, "git", "contribution_event").num_rows
        issue_after = storage.read_table(data_dir, "jira", "issue").num_rows
        assert contribution_after == contribution_before
        assert issue_after == issue_before

        first_metrics = pq.read_table(
            data_dir / "snapshots" / first.run_id / "metrics.parquet"
        ).to_pylist()
        second_metrics = pq.read_table(
            data_dir / "snapshots" / second.run_id / "metrics.parquet"
        ).to_pylist()

        def _comparable(rows: list[dict]) -> list[tuple]:
            # run_id/computed_at are expected to differ between runs; every
            # other field is the actual computed metric and must match
            # exactly (reproducibility).
            return sorted(
                (
                    r["metric_id"],
                    r["definition_version"],
                    r["window_start"],
                    r["window_end"],
                    r["value"],
                    r["n"],
                    r["flag"],
                    r["details_json"],
                )
                for r in rows
            )

        assert _comparable(first_metrics) == _comparable(second_metrics)


# --- Partial failure (ARCHITECTURE.md §7.3) ----------------------------------


class TestPartialFailure:
    def test_jira_failure_marks_source_failed_with_last_good_snapshot(
        self, tmp_path, config, git_workdir
    ):
        data_dir = tmp_path / "data"
        good_transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})

        first = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(good_transport),
        )
        assert first.manifest["sources"]["jira"]["status"] == "ok"

        broken_transport = _always_503_transport()
        second = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            now=NOW.replace(hour=7),
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(broken_transport, max_retries=2),
        )

        # A source outage never fails the whole run -- metrics still compute
        # from git's fresh data plus jira's last-known-good raw data.
        assert second.exit_code == 0
        assert second.manifest["sources"]["jira"]["status"] == "failed"
        assert second.manifest["sources"]["jira"]["last_good_snapshot"] == first.run_id
        assert "reason" in second.manifest["sources"]["jira"]
        assert second.manifest["sources"]["git"]["status"] == "ok"
        assert (data_dir / "snapshots" / second.run_id / "metrics.parquet").is_file()

        loaded = load_manifest(data_dir, second.run_id)
        assert loaded.sources["jira"].status == "failed"
        assert loaded.sources["jira"].last_good_snapshot == first.run_id


# --- Metrics-stage failure (ARCHITECTURE.md §7.3) ----------------------------


class TestMetricsFailure:
    def test_metrics_exception_exits_nonzero_and_skips_site(
        self, tmp_path, config, git_workdir, monkeypatch
    ):
        data_dir = tmp_path / "data"
        site_out = tmp_path / "site"
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic metrics engine failure")

        monkeypatch.setattr("project_health.pipeline.compute_all", _boom)

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            site_out=site_out,
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport),
        )

        assert result.exit_code == 1
        assert result.manifest["status"] == "failed"
        assert "synthetic metrics engine failure" in result.manifest["error"]
        assert result.manifest["site_deploy_status"] is None
        assert not (data_dir / "snapshots" / result.run_id / "metrics.parquet").exists()
        assert not site_out.exists() or not (site_out / "index.html").exists()

        # the manifest itself is still written, and still loads cleanly
        loaded = load_manifest(data_dir, result.run_id)
        assert loaded.run_id == result.run_id


# --- Read-time dedupe --------------------------------------------------------


class TestDedupe:
    def test_dedupe_issue_rows_keeps_latest_updated_at(self):
        schema = get_schema("issue")
        base = {
            "summary": None,
            "status": None,
            "status_category": None,
            "priority": None,
            "issue_type": None,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "resolved_at": None,
            "reporter_identity_id": None,
            "reporter_raw": None,
            "assignee_identity_id": None,
            "assignee_raw": None,
        }
        rows = [
            {
                **base,
                "issue_key": "CASSANDRA-1",
                "summary": "old snapshot",
                "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "source_snapshot_id": "run-1:jira",
            },
            {
                **base,
                "issue_key": "CASSANDRA-1",
                "summary": "re-fetched, newer",
                "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
                "source_snapshot_id": "run-2:jira",
            },
            {
                **base,
                "issue_key": "CASSANDRA-2",
                "summary": "unrelated issue",
                "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "source_snapshot_id": "run-1:jira",
            },
        ]
        table = validate("issue", pa.Table.from_pylist(rows, schema=schema))

        deduped = _dedupe_issue_rows(table)

        by_key = {r["issue_key"]: r for r in deduped.to_pylist()}
        assert deduped.num_rows == 2
        assert by_key["CASSANDRA-1"]["summary"] == "re-fetched, newer"
        assert by_key["CASSANDRA-2"]["summary"] == "unrelated issue"

    def test_dedupe_jira_review_events_keeps_latest_occurred_at_per_issue_and_reviewer(self):
        schema = get_schema("review_event")
        base = {
            "source": "jira_field",
            "reviewer_identity_id": None,
            "reviewer_raw_type": "jira_username",
            "author_identity_id": None,
            "author_raw_type": None,
            "author_raw_value": None,
            "repo": None,
        }
        rows = [
            {
                **base,
                "event_id": "e1",
                "reviewer_raw_value": "alice",
                "issue_key": "CASSANDRA-1",
                "occurred_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "evidence": "first fetch",
                "source_snapshot_id": "run-1:jira",
            },
            {
                **base,
                "event_id": "e2",
                "reviewer_raw_value": "alice",
                "issue_key": "CASSANDRA-1",
                "occurred_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
                "evidence": "re-fetched (issue's updated_at moved)",
                "source_snapshot_id": "run-2:jira",
            },
            {
                **base,
                "event_id": "e3",
                "reviewer_raw_value": "bob",
                "issue_key": "CASSANDRA-1",
                "occurred_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "evidence": "different reviewer, same issue",
                "source_snapshot_id": "run-1:jira",
            },
        ]
        table = validate("review_event", pa.Table.from_pylist(rows, schema=schema))

        deduped = _dedupe_jira_review_events(table)

        assert deduped.num_rows == 2
        by_reviewer = {r["reviewer_raw_value"]: r for r in deduped.to_pylist()}
        assert by_reviewer["alice"]["evidence"] == "re-fetched (issue's updated_at moved)"
        assert by_reviewer["bob"]["evidence"] == "different reviewer, same issue"

    def test_dedupe_functions_are_noop_on_empty_tables(self):
        assert _dedupe_issue_rows(get_schema("issue").empty_table()).num_rows == 0
        assert _dedupe_jira_review_events(get_schema("review_event").empty_table()).num_rows == 0
