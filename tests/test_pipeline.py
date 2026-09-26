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
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from project_health import storage
from project_health.collectors.asf_roster import AsfRosterCollector
from project_health.collectors.jira import JiraCollector
from project_health.collectors.ponymail import PonyMailCollector
from project_health.config import load_project
from project_health.pipeline import (
    _dedupe_issue_rows,
    _dedupe_jira_review_events,
    _dedupe_roster_entries,
    run_pipeline,
)
from project_health.schema import get_schema, validate
from project_health.site.manifest import load_manifest
from tests.fixtures.git.build_repo import build_repo

JIRA_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "jira"
ROSTER_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "asf_roster"


def _load_jira_fixture(name: str) -> dict:
    return json.loads((JIRA_FIXTURES_DIR / name).read_text())


def _load_roster_fixture(name: str) -> dict:
    return json.loads((ROSTER_FIXTURES_DIR / name).read_text())


PAGE_1 = _load_jira_fixture("search_with_reviewers.json")
PAGE_2 = _load_jira_fixture("search_with_reviewers_page2.json")
EMPTY_PAGE = {"expand": "schema,names", "startAt": 0, "maxResults": 5, "total": 0, "issues": []}

COMMITTEE_INFO = _load_roster_fixture("committee_info_cassandra.json")
PUBLIC_LDAP_PROJECTS = _load_roster_fixture("public_ldap_projects_cassandra.json")

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


def _roster_transport() -> httpx.MockTransport:
    """Mock transport for ASF roster endpoints."""
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "committee-info" in url_str or "committees" in request.url.path:
            return httpx.Response(200, json=COMMITTEE_INFO)
        elif "public_ldap_projects" in url_str or "projects" in request.url.path:
            return httpx.Response(200, json=PUBLIC_LDAP_PROJECTS)
        else:
            return httpx.Response(200, json=PUBLIC_LDAP_PROJECTS)

    return httpx.MockTransport(handler)


def _roster_factory(transport: httpx.MockTransport | None = None):
    """Create a factory for offline ASF roster collection."""
    transport = transport or _roster_transport()

    def factory(config):
        return AsfRosterCollector(
            config,
            transport=transport,
            max_retries=1,
            sleep_fn=lambda s: None,
        )

    return factory


# A minimal, single-month stats.lua fixture -- keeps the ponymail collector's
# offline "no new data" path in every pipeline test fast and deterministic
# (issue #33's collector has its own dedicated fixture-based tests in
# tests/test_ponymail_collector.py; here it's only wired in so run_pipeline's
# default `ALL_SOURCES` -- now including "ponymail" -- never touches the
# network in this file's tests).
PONYMAIL_STATS = {"firstYear": 2026, "firstMonth": 9, "lastYear": 2026, "lastMonth": 9}


def _ponymail_transport(mbox_content: bytes = b"") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("stats.lua"):
            return httpx.Response(200, json=PONYMAIL_STATS)
        return httpx.Response(200, content=mbox_content)

    return httpx.MockTransport(handler)


def _ponymail_factory(mbox_content: bytes = b""):
    transport = _ponymail_transport(mbox_content)

    def factory(config):
        return PonyMailCollector(
            config,
            transport=transport,
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
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster", "ponymail"],
            site_out=site_out,
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
        )

        # exit_code is 0 (ok) because all metrics are now computable with roster data
        assert result.exit_code == 0
        assert result.manifest["status"] == "ok"
        assert result.manifest["metrics_missing"] == []
        assert result.manifest["sources"]["git"]["status"] == "ok"
        assert result.manifest["sources"]["jira"]["status"] == "ok"
        assert result.manifest["sources"]["asf_roster"]["status"] == "ok"
        # issue #33: ponymail is a fourth first-class source in the manifest.
        assert result.manifest["sources"]["ponymail"]["status"] == "ok"
        assert result.manifest["sources"]["ponymail"]["records_collected"] == 0
        # issue #33 is collector-only: it registers no metrics of its own
        # (those come in a later issue), so every *currently* registered
        # metric (git/jira/roster-derived) must still compute cleanly --
        # adding the ponymail collector/tables must never make an unrelated,
        # already-registered metric go missing.
        # 15 non-merge commits in the #3 fixture repo, 1 of them a bot
        # (github-actions[bot], excluded by projects/cassandra.yaml's
        # bot_patterns) -> 14 real contributions.
        assert result.manifest["sources"]["git"]["records_collected"] == 14
        assert result.manifest["sources"]["jira"]["records_collected"] == 10
        # Roster: 49 PMC + 52 committers = 101 total
        assert result.manifest["sources"]["asf_roster"]["records_collected"] == 101
        # Data-quality signal (issue #18): the #3 fixture repo has no
        # placeholder-reviewer ("TBD"/"none"/"n/a") trailers, so this is 0
        # here -- the field's presence/wiring is what's under test.
        assert result.manifest["sources"]["git"]["placeholder_reviewer_commits"] == 0

        # raw partitions
        assert list((data_dir / "raw" / "git" / "contribution_event").glob("date=*/part-*.parquet"))
        assert list((data_dir / "raw" / "git" / "review_event").glob("date=*/part-*.parquet"))
        assert list((data_dir / "raw" / "jira" / "issue").glob("date=*/part-*.parquet"))
        assert list((data_dir / "raw" / "jira" / "review_event").glob("date=*/part-*.parquet"))
        roster_raw_dir = data_dir / "raw" / "asf_roster" / "roster_entry"
        assert list(roster_raw_dir.glob("date=*/part-*.parquet"))
        assert list((data_dir / "raw" / "ponymail" / "message").glob("date=*/part-*.parquet"))
        assert list(
            (data_dir / "raw" / "ponymail" / "message_thread").glob("date=*/part-*.parquet")
        )

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
        assert loaded.sources["asf_roster"].status == "ok"
        assert loaded.sources["ponymail"].status == "ok"

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
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster"],
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
        )

        assert result.manifest["sources"]["git"]["placeholder_reviewer_commits"] == 1

    def test_run_id_format_and_watermarks_advance(self, tmp_path, config, git_workdir):
        data_dir = tmp_path / "data"
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster", "ponymail"],
            now=NOW,
            code_sha="abc1234567",
            jira_collector_factory=_jira_factory(transport),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
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
        assert storage.read_watermark(data_dir, "ponymail") is not None


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
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster"],
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport_1),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
        )
        # Exit code is 0 (ok) because all metrics are computable with roster data
        assert first.exit_code == 0
        assert first.manifest["metrics_missing"] == []

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
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster"],
            now=NOW.replace(hour=7),
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport_2),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
        )
        # Exit code is 0 (ok) because all metrics are computable with roster data
        assert second.exit_code == 0
        assert second.manifest["metrics_missing"] == []
        assert second.run_id != first.run_id
        assert second.manifest["sources"]["git"]["records_collected"] == 0
        assert second.manifest["sources"]["jira"]["records_collected"] == 0
        assert second.manifest["sources"]["asf_roster"]["records_collected"] == 101

        contribution_after = storage.read_table(data_dir, "git", "contribution_event").num_rows
        issue_after = storage.read_table(data_dir, "jira", "issue").num_rows
        assert contribution_after == contribution_before
        assert issue_after == issue_before
        # Note: roster_entry accumulates in raw storage but is deduped during
        # metrics computation, so we check that metrics are consistent instead

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
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster"],
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(good_transport),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
        )
        assert first.manifest["sources"]["jira"]["status"] == "ok"

        broken_transport = _always_503_transport()
        second = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster"],
            now=NOW.replace(hour=7),
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(broken_transport, max_retries=2),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
        )

        # A JIRA source outage still produces valid metrics via prior JIRA data
        # plus current roster data -> exit code 0 (ok).
        # Metrics still compute from git's fresh data plus jira's last-known-good
        # raw data and roster's fresh data.
        assert second.exit_code == 0
        assert second.manifest["status"] == "ok"
        assert second.manifest["metrics_missing"] == []
        assert second.manifest["sources"]["jira"]["status"] == "failed"
        assert second.manifest["sources"]["jira"]["last_good_snapshot"] == first.run_id
        assert "reason" in second.manifest["sources"]["jira"]
        assert second.manifest["sources"]["git"]["status"] == "ok"
        assert second.manifest["sources"]["asf_roster"]["status"] == "ok"
        assert (data_dir / "snapshots" / second.run_id / "metrics.parquet").is_file()

        loaded = load_manifest(data_dir, second.run_id)
        assert loaded.sources["jira"].status == "failed"
        assert loaded.sources["jira"].last_good_snapshot == first.run_id


# --- file_change_event backfill gap (issue #53 fixup cycle 1) ---------------


class TestFileChangeEventBackfillGap:
    """A data dir collected before issue #53 (`file_change_event`,
    `truck_factor`) existed already has a `git` watermark sitting at HEAD.
    That table's *own* watermark (`storage.read_watermark(..., table=
    "file_change_event")`) must still read back `None` in that case, so its
    first-ever collection backfills full history instead of silently
    reusing `git`'s "already caught up" position and collecting nothing
    forever (the real bug found in review: `truck_factor` would compute zero
    rows on every run, `metrics_missing` would never clear, and the nightly
    deploy would stay `degraded`)."""

    def test_missing_file_change_event_watermark_backfills_and_clears_metrics_missing(
        self, tmp_path, config, git_workdir
    ):
        data_dir = tmp_path / "data"
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})

        # A normal first run: with this fix in place, `file_change_event`'s
        # watermark is written right alongside `git`'s.
        first = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            # issue #36: this test exercises the file_change_event watermark
            # fix specifically; governance's own coverage lives in
            # TestGovernanceIntegration below.
            sources=["git", "jira", "asf_roster"],
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport),
            asf_roster_collector_factory=_roster_factory(),
        )
        assert first.exit_code == 0
        assert first.manifest["metrics_missing"] == []
        file_change_event_before = storage.read_table(data_dir, "git", "file_change_event")
        assert file_change_event_before.num_rows > 0

        # Simulate a data dir from *before* this fix: `file_change_event` was
        # never collected (the table didn't exist yet), so its raw partitions
        # and its own watermark key are both absent -- but `git`'s commit
        # watermark (and contribution_event/review_event) already reached
        # HEAD from years of prior collection.
        shutil.rmtree(data_dir / "raw" / "git" / "file_change_event")
        watermarks_path = storage.watermarks_path(data_dir)
        watermarks = json.loads(watermarks_path.read_text())
        assert "git:file_change_event" in watermarks  # sanity: the fix did write it
        del watermarks["git:file_change_event"]
        watermarks_path.write_text(json.dumps(watermarks, indent=2, sort_keys=True))
        assert storage.read_watermark(data_dir, "git", table="file_change_event") is None
        assert storage.read_watermark(data_dir, "git") is not None  # unchanged: still at HEAD

        # Second run: no new commits (git_workdir hasn't moved), so the
        # `git` commit watermark's range is empty -- but `file_change_event`
        # must still backfill its full history from scratch.
        second = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            sources=["git", "jira", "asf_roster"],
            now=NOW.replace(hour=7),
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(_paginated_transport({0: EMPTY_PAGE})),
            asf_roster_collector_factory=_roster_factory(),
        )

        assert second.exit_code == 0
        assert second.manifest["status"] == "ok"
        assert second.manifest["metrics_missing"] == []
        assert second.manifest["sources"]["git"]["records_collected"] == 0
        assert second.manifest["sources"]["git"]["file_changes_collected"] == (
            file_change_event_before.num_rows
        )

        file_change_event_after = storage.read_table(data_dir, "git", "file_change_event")
        assert file_change_event_after.num_rows == file_change_event_before.num_rows

        metrics_table = pq.read_table(data_dir / "snapshots" / second.run_id / "metrics.parquet")
        truck_factor_rows = [
            r for r in metrics_table.to_pylist() if r["metric_id"] == "truck_factor"
        ]
        assert truck_factor_rows
        assert any(r["n"] >= 1 for r in truck_factor_rows)


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
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster"],
            site_out=site_out,
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
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


# --- Degraded run: a registered metric produced zero rows (issue #24) -------


class TestDegradedMetrics:
    def test_registered_metric_with_zero_rows_marks_manifest_degraded_and_exits_nonzero(
        self, tmp_path, config, git_workdir, monkeypatch
    ):
        """A registered metric (metrics.registry.METRIC_IDS) that computes
        successfully but yields zero `metric_value` rows -- e.g. issue #24's
        collector/engine `event_type` mismatch -- must never look like a
        clean `status: ok` run: the manifest is `degraded`, the metric is
        named in `metrics_missing`, and the CLI's exit code is non-zero. The
        site is still generated (§7.3 only blocks the site on a metrics
        *exception*, not on a metric quietly coming back empty) so the gap
        is visible there too.
        """
        from project_health.metrics import compute_all as real_compute_all

        data_dir = tmp_path / "data"
        site_out = tmp_path / "site"
        transport = _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})

        def _drop_stale_jira_rate(*args, **kwargs):
            table = real_compute_all(*args, **kwargs)
            keep = [r for r in table.to_pylist() if r["metric_id"] != "stale_jira_rate"]
            if not keep:
                return table.schema.empty_table()
            return pa.Table.from_pylist(keep, schema=table.schema)

        monkeypatch.setattr("project_health.pipeline.compute_all", _drop_stale_jira_rate)

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            # issue #36: these tests exercise git/jira collection specifically;
            # governance compliance scoring gets its own dedicated coverage in
            # TestGovernanceIntegration below rather than every call site here
            # needing an offline governance evidence-source stub.
            sources=["git", "jira", "asf_roster"],
            site_out=site_out,
            now=NOW,
            code_sha="abc1234",
            jira_collector_factory=_jira_factory(transport),
            asf_roster_collector_factory=_roster_factory(),
            ponymail_collector_factory=_ponymail_factory(),
        )

        assert result.exit_code != 0
        assert result.manifest["status"] == "degraded"
        # Only stale_jira_rate is missing (test dropped it); pmc_joins_quarterly is now computable
        assert sorted(result.manifest["metrics_missing"]) == [
            "stale_jira_rate",
        ]
        assert "stale_jira_rate" not in {
            m.split("@")[0] for m in result.manifest["metrics_computed"]
        }
        # the site is still (re)generated -- a degraded metrics stage isn't
        # the same failure mode as compute_all raising.
        assert result.manifest["site_deploy_status"] == "ok"
        assert (site_out / "index.html").is_file()

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

    def test_dedupe_roster_entries_keeps_latest_per_asf_id(self):
        schema = get_schema("roster_entry")
        base = {
            "entry_id": "entry-1",
            "identity_id": None,
            "display_name": "Test User",
            "role": "pmc",
            "project": "cassandra",
            "effective_from": None,
            "effective_from_raw": None,
        }
        rows = [
            {
                **base,
                "asf_id": "testuser",
                "source_snapshot_id": "run-1:asf_roster",
            },
            {
                **base,
                "asf_id": "testuser",
                "source_snapshot_id": "run-2:asf_roster",
                "display_name": "Test User (updated)",
            },
            {
                **base,
                "asf_id": "other",
                "source_snapshot_id": "run-1:asf_roster",
            },
        ]
        table = validate("roster_entry", pa.Table.from_pylist(rows, schema=schema))

        deduped = _dedupe_roster_entries(table)

        assert deduped.num_rows == 2
        by_id = {r["asf_id"]: r for r in deduped.to_pylist()}
        assert by_id["testuser"]["display_name"] == "Test User (updated)"
        assert by_id["other"]["source_snapshot_id"] == "run-1:asf_roster"

    def test_dedupe_functions_are_noop_on_empty_tables(self):
        assert _dedupe_issue_rows(get_schema("issue").empty_table()).num_rows == 0
        assert _dedupe_jira_review_events(get_schema("review_event").empty_table()).num_rows == 0
        assert _dedupe_roster_entries(get_schema("roster_entry").empty_table()).num_rows == 0


# --- Ponymail backfill cap (issue #33 fixup) ---------------------------------


def _ponymail_factory_with_stats(stats_json: bytes, mbox_by_date: dict[str, bytes] | None = None):
    """A ponymail collector factory serving a custom `stats.lua` payload
    (rather than `PONYMAIL_STATS`'s single-month default) so backfill-cap
    behavior against a longer month range can be exercised without a live
    fetch."""
    mbox_by_date = mbox_by_date or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("stats.lua"):
            return httpx.Response(200, content=stats_json)
        query = parse_qs(request.url.query.decode())
        date = query["date"][0]
        return httpx.Response(200, content=mbox_by_date.get(date, b""))

    transport = httpx.MockTransport(handler)

    def factory(config):
        return PonyMailCollector(
            config,
            transport=transport,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )

    return factory


# 40 months (2023-01..2026-04) -- longer than the config default
# `mailing_lists.max_months_per_run: 36` (projects/cassandra.yaml), so a
# default (uncapped-by-CLI) run must still cap itself and report backlog.
FORTY_MONTH_STATS = json.dumps(
    {"firstYear": 2023, "firstMonth": 1, "lastYear": 2026, "lastMonth": 4}
).encode()


class TestPonymailBackfillCap:
    def test_default_cap_from_config_used_when_cli_flag_not_given(self, tmp_path, config):
        data_dir = tmp_path / "data"

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=tmp_path / "workdir",
            sources=["ponymail"],
            now=NOW,
            code_sha="abc1234",
            ponymail_collector_factory=_ponymail_factory_with_stats(FORTY_MONTH_STATS),
        )

        ponymail = result.manifest["sources"]["ponymail"]
        # projects/cassandra.yaml's mailing_lists.max_months_per_run is 36;
        # 40 months total - 1 current month - 35 oldest completed months
        # fetched = 4 months still outstanding, for each of dev/user.
        assert ponymail["backfill"] == {
            "dev": {"months_remaining": 4},
            "user": {"months_remaining": 4},
        }
        assert ponymail["partial"] is True

    def test_explicit_cli_cap_overrides_config_default(self, tmp_path, config):
        data_dir = tmp_path / "data"

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=tmp_path / "workdir",
            sources=["ponymail"],
            max_ponymail_months=5,
            now=NOW,
            code_sha="abc1234",
            ponymail_collector_factory=_ponymail_factory_with_stats(FORTY_MONTH_STATS),
        )

        ponymail = result.manifest["sources"]["ponymail"]
        # cap=5 -> 4 oldest completed months + the current month; 39
        # completed months total - 4 fetched = 35 still outstanding. This
        # differs from the config-default-cap case above (4 remaining),
        # proving the explicit CLI value -- not the config default -- was
        # actually used.
        assert ponymail["backfill"] == {
            "dev": {"months_remaining": 35},
            "user": {"months_remaining": 35},
        }
        assert ponymail["partial"] is True

    def test_second_capped_run_advances_watermark_until_caught_up(self, tmp_path, config):
        """A small synthetic 5-month range, capped to 2 months/run: the
        first run backfills the 2 oldest months (partial=True), the second
        run resumes from the advanced watermark and finishes the backlog
        (partial=False) -- demonstrating a capped run always advances the
        per-list watermark run over run, per ARCHITECTURE.md §4.3."""
        five_month_stats = json.dumps(
            {"firstYear": 2026, "firstMonth": 1, "lastYear": 2026, "lastMonth": 5}
        ).encode()
        data_dir = tmp_path / "data"

        first = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=tmp_path / "workdir",
            sources=["ponymail"],
            max_ponymail_months=2,
            now=NOW,
            code_sha="abc1234",
            ponymail_collector_factory=_ponymail_factory_with_stats(five_month_stats),
        )
        first_ponymail = first.manifest["sources"]["ponymail"]
        # cap=2 -> 1 oldest completed month ("2026-01") + current month
        # ("2026-05") fetched; 4 completed months total - 1 fetched = 3
        # remaining.
        assert first_ponymail["watermark"] == {"dev": "2026-01", "user": "2026-01"}
        assert first_ponymail["backfill"] == {
            "dev": {"months_remaining": 3},
            "user": {"months_remaining": 3},
        }
        assert first_ponymail["partial"] is True

        second = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=tmp_path / "workdir",
            sources=["ponymail"],
            max_ponymail_months=2,
            now=NOW.replace(hour=7),
            code_sha="abc1234",
            ponymail_collector_factory=_ponymail_factory_with_stats(five_month_stats),
        )
        second_ponymail = second.manifest["sources"]["ponymail"]
        # Resumes after "2026-01": needed = ["2026-02".."2026-05"] (4
        # months), still > cap=2 -> 1 more oldest completed month
        # ("2026-02") + current month fetched; watermark advances again.
        assert second_ponymail["watermark"] == {"dev": "2026-02", "user": "2026-02"}
        assert second_ponymail["backfill"] == {
            "dev": {"months_remaining": 2},
            "user": {"months_remaining": 2},
        }
        assert second_ponymail["partial"] is True
# --- Governance compliance engine wiring (issue #36) -------------------------
#
# `sources` now includes `"governance"` (default: all of `ALL_SOURCES`), so
# every test above opts back out to `sources=["git", "jira", "asf_roster"]`
# (everything except governance -- `asf_roster` stays in so `pmc_joins_
# quarterly`, a registered M0 metric since issue #50, still gets real data
# and a clean `status: "ok"`) to keep testing exactly what it always tested,
# offline, with no governance evidence-source stub needed. Governance's own
# end-to-end wiring gets its coverage here instead, using fixture data (the
# #3 git fixture repo's real reviewer trailers/issue keys, plus a couple of
# stub evidence-source collectors) — never the real JIRA-comments or
# GitHub-checks APIs.


class _StubJiraComments:
    """Offline stand-in for `collectors.jira_comments.JiraCommentsCollector`
    (issue #36 fixup cycle 1: `_collect_governance` now calls the *singular*
    `fetch_ci_evidence`/`call_count`-budgeted interface, not the old batch
    `fetch_ci_evidence_for_issues`)."""

    def __init__(self, evidence_by_issue: dict):
        self._evidence = evidence_by_issue
        self.call_count = 0

    def fetch_ci_evidence(self, issue_key):
        self.call_count += 1
        return self._evidence.get(issue_key)

    def close(self):
        pass


class _StubGitHubChecks:
    """Offline stand-in for `collectors.github_checks.GitHubChecksCollector`
    (issue #36 fixup cycle 1: singular `fetch_checkstyle_evidence`/
    `call_count`, matching the real collector's budgeted interface)."""

    def __init__(self, runs_by_sha: dict):
        self._runs = runs_by_sha
        self.call_count = 0

    def fetch_checkstyle_evidence(self, sha):
        self.call_count += 1
        return self._runs.get(sha, ())

    def close(self):
        pass


class _FailingGitHubChecks:
    """Simulates a GitHub API outage -- must not take down governance scoring."""

    def __init__(self):
        self.call_count = 0

    def fetch_checkstyle_evidence(self, sha):
        self.call_count += 1
        raise RuntimeError("synthetic GitHub API outage")

    def close(self):
        pass


def _latest_non_merge_sha(repo_path) -> str:
    """A real non-merge commit sha on trunk -- merge commits are excluded
    from `code-style-checkstyle` scoring (`_collect_governance`'s
    `checkstyle_shas` filter), so a test targeting that check needs a
    non-merge sha, not `trunk`'s HEAD (which is a merge commit in the #3
    fixture repo)."""
    return subprocess.run(
        ["git", "-C", str(repo_path), "log", "--no-merges", "-1", "--format=%H", "trunk"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# The #3 fixture repo's commits are pinned to 2024 dates (deterministic,
# never regenerated), but `NOW` here is 2026-09-25 -- well past
# `DEFAULT_GOVERNANCE_CHECKSTYLE_RETENTION_DAYS` (400 days, issue #36 fixup
# cycle 2). Tests exercising *checkstyle fetch behavior* specifically (not
# the retention cutoff itself, which gets its own dedicated test) need a
# much larger retention window so the fixture's fixed 2024 dates don't fall
# outside it purely because of how much real time has passed since the
# fixture was authored.
_LARGE_TEST_RETENTION_DAYS = 3650


def _with_governance(config, **overrides):
    """`config`, with its `governance:` block replaced by `overrides`
    (defaulting `checkstyle_retention_days` to a large value so fixture-repo
    commits from 2024 never fall outside it just because real time has
    moved on) -- `model_copy` since `ProjectConfig` is a pydantic model."""
    governance = {"checkstyle_retention_days": _LARGE_TEST_RETENTION_DAYS, **overrides}
    return config.model_copy(update={"governance": governance})


class TestGovernanceIntegration:
    def test_governance_produces_commit_compliance_and_metric_rows(
        self, tmp_path, config, git_workdir
    ):
        from project_health.collectors.github_checks import CheckstyleEvidence
        from project_health.collectors.jira_comments import CommentCIEvidence

        data_dir = tmp_path / "data"
        sha = _latest_non_merge_sha(git_workdir)

        jira_stub = _StubJiraComments(
            {
                "CASSANDRA-112": CommentCIEvidence(
                    issue_key="CASSANDRA-112",
                    comment_id="1",
                    comment_author="alice",
                    comment_created_at="2024-07-08T00:00:00.000+0000",
                    matched_term="jenkins",
                    matched_url="https://ci-cassandra.apache.org/job/x/1",
                )
            }
        )
        github_stub = _StubGitHubChecks(
            {
                sha: (
                    CheckstyleEvidence(
                        sha=sha, check_run_name="ant-check-jdk11", conclusion="success"
                    ),
                )
            }
        )

        result = run_pipeline(
            config=_with_governance(config),
            data_dir=data_dir,
            workdir=git_workdir,
            sources=["git", "jira", "asf_roster", "governance"],
            jira_collector_factory=_jira_factory(
                _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})
            ),
            asf_roster_collector_factory=_roster_factory(),
            now=NOW,
            code_sha="abc1234",
            governance_jira_comments_factory=lambda base_url: jira_stub,
            governance_github_checks_factory=lambda owner, repo: github_stub,
        )

        assert result.exit_code == 0
        governance = result.manifest["governance"]
        assert governance["status"] == "ok"
        assert governance["commits_scored"] > 0
        assert governance["compliance_rows"] > 0

        compliance = pq.read_table(
            data_dir / "snapshots" / result.run_id / "governance_commit_compliance.parquet"
        )
        assert compliance.num_rows == governance["compliance_rows"]
        rows = compliance.to_pylist()

        checkstyle_row = next(
            r for r in rows if r["sha"] == sha and r["check_id"] == "code-style-checkstyle"
        )
        assert checkstyle_row["result"] == "pass"

        metrics = pq.read_table(
            data_dir / "snapshots" / result.run_id / "governance_metric_value.parquet"
        )
        assert metrics.num_rows > 0
        assert {r["metric_id"] for r in metrics.to_pylist()} <= {
            "governance_reviewer_present_pass_rate",
            "governance_jira_ticket_referenced_pass_rate",
            "governance_pre_commit_ci_evidence_pass_rate",
            "governance_code_style_checkstyle_pass_rate",
        }

    def test_github_checks_outage_still_scores_other_checks_with_unknown(
        self, tmp_path, config, git_workdir
    ):
        """A GitHub API outage must never zero out governance's output --
        `reviewer-present`/`jira-ticket-referenced` still score from git
        alone, and `code-style-checkstyle` falls back to its documented
        `unknown` (never a missing row). The run's overall `status` is
        `'partial'` (issue #36 fixup cycle 1: the checkstyle backlog wasn't
        cleared this run, same as a budget cutoff) -- never `'failed'`, and
        the M0 pipeline is unaffected either way (governance metrics are
        outside `metrics.registry.METRIC_IDS`)."""
        data_dir = tmp_path / "data"

        result = run_pipeline(
            config=_with_governance(config),
            data_dir=data_dir,
            workdir=git_workdir,
            sources=["governance"],
            now=NOW,
            code_sha="abc1234",
            governance_jira_comments_factory=lambda base_url: _StubJiraComments({}),
            governance_github_checks_factory=lambda owner, repo: _FailingGitHubChecks(),
        )

        governance = result.manifest["governance"]
        assert governance["status"] == "partial"
        assert governance["commits_scored"] > 0

        rows = pq.read_table(
            data_dir / "snapshots" / result.run_id / "governance_commit_compliance.parquet"
        ).to_pylist()
        assert any(r["check_id"] == "reviewer-present" and r["result"] == "pass" for r in rows)
        checkstyle_rows = [r for r in rows if r["check_id"] == "code-style-checkstyle"]
        assert checkstyle_rows
        assert all(r["result"] == "unknown" for r in checkstyle_rows)

    def test_governance_never_registered_in_metric_ids_so_never_marks_run_degraded(
        self, tmp_path, config, git_workdir
    ):
        """Governance metrics are intentionally never added to
        `metrics.registry.METRIC_IDS` (see `governance/metrics.py`'s module
        docstring) -- this proves a governance-only run with zero JIRA/
        GitHub evidence still exits 0 / `status: ok`, never `degraded`,
        which is exactly the failure mode issue #24 guards the *M0* metrics
        against and which governance must never trip by accident.
        """
        data_dir = tmp_path / "data"

        result = run_pipeline(
            config=config,
            data_dir=data_dir,
            workdir=git_workdir,
            sources=["git", "jira", "asf_roster", "governance"],
            jira_collector_factory=_jira_factory(
                _paginated_transport({0: PAGE_1, 5: PAGE_2, 10: EMPTY_PAGE})
            ),
            asf_roster_collector_factory=_roster_factory(),
            now=NOW,
            code_sha="abc1234",
            governance_jira_comments_factory=lambda base_url: _StubJiraComments({}),
            governance_github_checks_factory=lambda owner, repo: _StubGitHubChecks({}),
        )

        assert result.exit_code == 0
        assert result.manifest["status"] == "ok"
        assert result.manifest["metrics_missing"] == []


class TestCiEligibleIssueKeys:
    """Issue #36 fixup cycle 2: `_ci_eligible_issue_keys_newest_first`."""

    def test_excludes_issue_only_referenced_before_effective_from(self):
        from project_health.pipeline import _ci_eligible_issue_keys_newest_first
        from project_health.governance.checks import CommitFacts
        from project_health.governance.policy import DEFAULT_POLICY_PATH, load_policy

        policy = load_policy(DEFAULT_POLICY_PATH)
        ci_rule = policy.rule("pre-commit-ci-evidence")

        before = CommitFacts(
            sha="a" * 40,
            branch="trunk",
            commit_date=datetime(2019, 1, 1, tzinfo=timezone.utc),
            message="patch by X; reviewed by Y for CASSANDRA-1",
            author="X",
            committer="X",
            is_merge=False,
            issue_keys=("CASSANDRA-1",),
        )
        after = CommitFacts(
            sha="b" * 40,
            branch="trunk",
            commit_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            message="patch by X; reviewed by Y for CASSANDRA-2",
            author="X",
            committer="X",
            is_merge=False,
            issue_keys=("CASSANDRA-2",),
        )
        result = _ci_eligible_issue_keys_newest_first([before, after], ci_rule)
        assert result == ["CASSANDRA-2"]

    def test_orders_newest_referencing_commit_first(self):
        from project_health.pipeline import _ci_eligible_issue_keys_newest_first
        from project_health.governance.checks import CommitFacts
        from project_health.governance.policy import DEFAULT_POLICY_PATH, load_policy

        policy = load_policy(DEFAULT_POLICY_PATH)
        ci_rule = policy.rule("pre-commit-ci-evidence")

        older = CommitFacts(
            sha="a" * 40,
            branch="trunk",
            commit_date=datetime(2022, 1, 1, tzinfo=timezone.utc),
            message="patch by X; reviewed by Y for CASSANDRA-1",
            author="X",
            committer="X",
            is_merge=False,
            issue_keys=("CASSANDRA-1",),
        )
        newer = CommitFacts(
            sha="b" * 40,
            branch="trunk",
            commit_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            message="patch by X; reviewed by Y for CASSANDRA-2",
            author="X",
            committer="X",
            is_merge=False,
            issue_keys=("CASSANDRA-2",),
        )
        result = _ci_eligible_issue_keys_newest_first([older, newer], ci_rule)
        assert result == ["CASSANDRA-2", "CASSANDRA-1"]

    def test_still_included_if_also_referenced_by_an_in_force_commit(self):
        """The same issue key referenced by both a before- and
        after-effective_from commit must still be fetched -- excluding it
        would wrongly cost the in-force commit its evidence too."""
        from project_health.pipeline import _ci_eligible_issue_keys_newest_first
        from project_health.governance.checks import CommitFacts
        from project_health.governance.policy import DEFAULT_POLICY_PATH, load_policy

        policy = load_policy(DEFAULT_POLICY_PATH)
        ci_rule = policy.rule("pre-commit-ci-evidence")

        before = CommitFacts(
            sha="a" * 40,
            branch="trunk",
            commit_date=datetime(2019, 1, 1, tzinfo=timezone.utc),
            message="patch by X; reviewed by Y for CASSANDRA-1",
            author="X",
            committer="X",
            is_merge=False,
            issue_keys=("CASSANDRA-1",),
        )
        after = CommitFacts(
            sha="b" * 40,
            branch="trunk",
            commit_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            message="follow-up for CASSANDRA-1",
            author="X",
            committer="X",
            is_merge=False,
            issue_keys=("CASSANDRA-1",),
        )
        result = _ci_eligible_issue_keys_newest_first([before, after], ci_rule)
        assert result == ["CASSANDRA-1"]


class TestGovernanceIncrementalCollection:
    """Issue #36 fixup cycle 1: evidence collection must be incremental --
    a second run must not re-walk already-collected commits or re-fetch
    already-resolved JIRA/GitHub evidence."""

    def _run(
        self, config, data_dir, git_workdir, *, now, jira_stub=None, github_stub=None, **governance
    ):
        return run_pipeline(
            config=_with_governance(config, **governance),
            data_dir=data_dir,
            workdir=git_workdir,
            sources=["governance"],
            now=now,
            code_sha="abc1234",
            governance_jira_comments_factory=lambda base_url: (jira_stub or _StubJiraComments({})),
            governance_github_checks_factory=lambda owner, repo: (
                github_stub or _StubGitHubChecks({})
            ),
        )

    def test_second_run_walks_zero_new_commits(self, tmp_path, config, git_workdir):
        data_dir = tmp_path / "data"
        first = self._run(config, data_dir, git_workdir, now=NOW)
        assert first.manifest["governance"]["git_records_collected"] > 0

        second = self._run(config, data_dir, git_workdir, now=NOW.replace(hour=7))
        assert second.manifest["governance"]["git_records_collected"] == 0
        # Full accumulated commit set is unchanged in size across both runs.
        assert second.manifest["governance"]["commits_scored"] == (
            first.manifest["governance"]["commits_scored"]
        )

    def test_ci_evidence_not_size_free_but_rechecked_only_when_issue_updated(
        self, tmp_path, config, git_workdir
    ):
        """An issue checked once (found or not) is never re-fetched on a
        later run unless its `updated_at` (from the already-collected
        `raw/jira/issue` table) has moved forward since."""
        data_dir = tmp_path / "data"
        jira_stub = _StubJiraComments({})  # never finds anything
        first = self._run(config, data_dir, git_workdir, now=NOW, jira_stub=jira_stub)
        first_checked = first.manifest["governance"]["ci_evidence"]["checked"]
        assert first_checked > 0  # the fixture repo's commits reference real issue keys
        assert first.manifest["governance"]["ci_evidence"]["pending"] == 0

        # Second run, brand-new stub instance (so `call_count` starts at 0
        # again) -- with no `raw/jira/issue` data at all in this data_dir,
        # every previously-checked issue key has no known `updated_at`, so
        # none of them should be considered "changed since last check".
        second_stub = _StubJiraComments({})
        second = self._run(
            config, data_dir, git_workdir, now=NOW.replace(hour=7), jira_stub=second_stub
        )
        assert second.manifest["governance"]["ci_evidence"]["checked"] == 0
        assert second_stub.call_count == 0
        assert second.manifest["governance"]["ci_evidence"]["skipped_up_to_date"] == first_checked

    def test_check_run_success_is_never_refetched(self, tmp_path, config, git_workdir):
        from project_health.collectors.github_checks import CheckstyleEvidence

        data_dir = tmp_path / "data"
        sha = _latest_non_merge_sha(git_workdir)
        github_stub = _StubGitHubChecks(
            {
                sha: (
                    CheckstyleEvidence(
                        sha=sha, check_run_name="ant-check-jdk11", conclusion="success"
                    ),
                )
            }
        )
        first = self._run(config, data_dir, git_workdir, now=NOW, github_stub=github_stub)
        assert first.manifest["governance"]["check_runs"]["checked"] > 0

        second_stub = _StubGitHubChecks({})
        second = self._run(
            config, data_dir, git_workdir, now=NOW.replace(hour=7), github_stub=second_stub
        )
        # The one sha with a recorded `success` conclusion is resolved and
        # skipped; nothing else in the tiny fixture repo needs checking a
        # second time either, since `_StubGitHubChecks({})` "found nothing"
        # on the first run already marked every other sha checked too.
        assert second.manifest["governance"]["check_runs"]["checked"] == 0
        assert second_stub.call_count == 0
        assert second.manifest["governance"]["check_runs"]["skipped_resolved"] >= 1

    def test_check_run_pending_old_commit_is_not_retried(self, tmp_path, config, git_workdir):
        """A sha whose only known result is "no run found" and whose commit
        is older than the 30-day retry window (but still within the much
        larger retention horizon) must not be retried forever."""
        from project_health.pipeline import _collect_governance_check_runs
        from project_health.governance.checks import CommitFacts

        data_dir = tmp_path / "data"
        old_commit = CommitFacts(
            sha="a" * 40,
            branch="trunk",
            commit_date=NOW - timedelta(days=400),
            message="patch by X; reviewed by Y for CASSANDRA-1",
            author="X",
            committer="X",
            is_merge=False,
        )
        # Older than the 30-day re-fetch window, but within retention (a
        # separate, much larger cutoff, issue #36 fixup cycle 2) -- this
        # test is specifically about the retry window, not retention.
        retention_cutoff = NOW - timedelta(days=1000)
        first_stub = _StubGitHubChecks({})  # "no run found" for every sha
        first_stats = _collect_governance_check_runs(
            data_dir,
            "run-1",
            NOW,
            [old_commit],
            500,
            "apache",
            "cassandra",
            lambda o, r: first_stub,
            retention_cutoff=retention_cutoff,
        )
        assert first_stats["checked"] == 1
        assert first_stub.call_count == 1

        second_stub = _StubGitHubChecks({})
        second_stats = _collect_governance_check_runs(
            data_dir,
            "run-2",
            NOW,
            [old_commit],
            500,
            "apache",
            "cassandra",
            lambda o, r: second_stub,
            retention_cutoff=retention_cutoff,
        )
        assert second_stats["checked"] == 0
        assert second_stats["skipped_too_old"] == 1
        assert second_stub.call_count == 0

    def test_check_run_outside_retention_is_never_fetched(self, tmp_path, config, git_workdir):
        """A commit older than the retention horizon is never fetched at
        all -- not even once -- and is never counted as `pending` backlog
        (issue #36 fixup cycle 2)."""
        from project_health.pipeline import _collect_governance_check_runs
        from project_health.governance.checks import CommitFacts

        data_dir = tmp_path / "data"
        ancient_commit = CommitFacts(
            sha="b" * 40,
            branch="trunk",
            commit_date=NOW - timedelta(days=500),
            message="patch by X; reviewed by Y for CASSANDRA-2",
            author="X",
            committer="X",
            is_merge=False,
        )
        stub = _StubGitHubChecks({})
        stats = _collect_governance_check_runs(
            data_dir,
            "run-1",
            NOW,
            [ancient_commit],
            500,
            "apache",
            "cassandra",
            lambda o, r: stub,
            retention_cutoff=NOW - timedelta(days=400),
        )
        assert stats["checked"] == 0
        assert stats["skipped_outside_retention"] == 1
        assert stats["pending"] == 0
        assert stub.call_count == 0

    def test_budget_exhaustion_marks_run_partial(self, tmp_path, config, git_workdir):
        """A near-zero per-run budget must stop cleanly (not raise) and
        report `status: 'partial'` with a nonzero `pending` count, rather
        than silently claiming `'ok'` with an incomplete backlog."""
        data_dir = tmp_path / "data"
        result = self._run(
            config,
            data_dir,
            git_workdir,
            now=NOW,
            max_github_calls_per_run=0,
            max_jira_calls_per_run=0,
        )

        governance = result.manifest["governance"]
        assert governance["status"] == "partial"
        assert governance["ci_evidence"]["pending"] > 0
        assert governance["check_runs"]["pending"] > 0
        # The commits themselves are still fully walked and scored (the git
        # walk has no budget -- only the two external evidence fetches do);
        # every check still produces a row, just with `unknown` results for
        # the two evidence-dependent checks.
        assert governance["commits_scored"] > 0
        rows = pq.read_table(
            data_dir / "snapshots" / result.run_id / "governance_commit_compliance.parquet"
        ).to_pylist()
        assert any(r["check_id"] == "pre-commit-ci-evidence" for r in rows)
