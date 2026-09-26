"""Pipeline runner (ARCHITECTURE.md §4.2-4.4, §5, §7.3, §11; issue #9).

`run_pipeline(...)` orchestrates one collection + metrics + (optional) site
run, end to end:

1. For each active source (`git`, `jira`): collect from that source's stored
   watermark, write the validated raw partitions
   (`project_health.storage.write_partition`), and only *then* advance the
   watermark (ARCHITECTURE.md §4.3 — "the watermark is updated only after
   that source's raw partitions are written successfully"). A source that
   raises (after its own collector's internal retries are exhausted) is
   marked `'failed'` in the run manifest, pointing at its
   `last_good_snapshot` (the last run_id that source succeeded on,
   ARCHITECTURE.md §7.3); the run continues rather than aborting.
2. Identity resolution and metric computation always run over the **entire**
   accumulated raw cache — every partition ever written for every source,
   not just what this run collected (D3: "never incremental"). This is what
   makes a failed source's stale-but-still-used raw data, and a
   successful-but-empty collection, both produce the same metric output as
   if nothing had changed.
3. `snapshots/<run_id>/metrics.parquet` and `manifests/<run_id>.json` are
   written (`project_health.provenance.build_manifest`); the manifest is the
   run's primary observability artifact (ARCHITECTURE.md §11).
4. If metric computation itself raises (a bug, not a source outage), the run
   exits non-zero and the site is **not** (re)generated — ARCHITECTURE.md
   §7.3's "if metric computation fails ... the site is not redeployed". A
   source failing to *collect* is not this: metrics still compute from
   whatever raw data already exists, and the run's exit code stays 0.
5. If metric computation *succeeds* but a registered metric
   (`metrics.registry.METRIC_IDS`) produced zero `metric_value` rows (issue
   #24 — e.g. a collector/engine contract mismatch silently zeroing a metric
   out on real data), the manifest's `status` is `'degraded'` and
   `metrics_missing` lists the missing metric id(s); the run still exits
   non-zero so this is never silently green, but the site **is** still
   (re)generated, so the gap is visible there too rather than the whole
   deploy blocking on it.

## Read-time dedupe (document per issue #9)

Raw data is append-only (D3) and JIRA's watermark carries a deliberate
overlap margin (`collectors/jira.py`'s `WATERMARK_SAFETY_MARGIN` — JQL's
minute-granularity `updated >=` filter can't express an exact boundary), so
consecutive runs' raw `issue`/`review_event` partitions can (and routinely
will) both contain a row for the same issue / same (issue, reviewer) pair.
Rather than trying to collect exactly-once (impossible given the JQL
precision limit), this module dedupes at *read* time, before identity
resolution or metrics ever see the rows — the raw partitions themselves are
never rewritten or deleted:

- `issue` rows are deduped on `issue_key`, keeping the row with the latest
  `updated_at` (a re-fetched issue's later snapshot is always at least as
  current as an earlier one).
- `review_event` rows sourced from JIRA (`source == 'jira_field'`) are
  deduped on `(issue_key, reviewer_raw_value)`, keeping the latest
  `occurred_at`. This can't reuse `event_id` as a natural key the way the
  `issue` table's `issue_key` works: `collectors/jira.py` mints a fresh
  `uuid4()` `event_id` on every fetch, so the same reviewer credit gets a
  different `event_id` each time its issue is re-fetched.
- `review_event` rows sourced from a git commit trailer (`source ==
  'commit_trailer'`) are **not** deduped this way: `collectors/git.py`'s
  watermark is an exact, exclusive commit-SHA range, so the same commit is
  never re-collected, and that source's `event_id` is a deterministic
  function of `(repo, sha, reviewer, issue_key)` — a real duplicate there
  would indicate a bug, not an expected overlap, and this module doesn't
  paper over that by deduping it away.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from project_health import storage
from project_health.collectors.asf_roster import AsfRosterCollector
from project_health.collectors.git import GitCollector, clone_or_fetch, github_clone_url
from project_health.collectors.jira import JiraCollector
from project_health.collectors.ponymail import PonyMailCollector
from project_health.config import ProjectConfig
from project_health.metrics import METRIC_IDS, compute_all
from project_health.normalize.identity import (
    extract_raw_identifiers,
    load_overrides,
    resolve_identities,
)
from project_health.provenance import (
    build_manifest,
    read_last_good_snapshot,
    record_last_good_snapshot,
)
from project_health.site.generate import generate as generate_site
from project_health.site.manifest import manifest_path

ALL_SOURCES: tuple[str, ...] = ("git", "jira", "asf_roster", "ponymail")


def _log(event: str, **fields: Any) -> None:
    """One JSON-lines structured log record to stdout (ARCHITECTURE.md §11)."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def get_pipeline_code_sha() -> str:
    """The pipeline's own `main`-branch commit SHA (ARCHITECTURE.md §5).

    Prefers `GITHUB_SHA` (set by GitHub Actions) so a workflow run stamps
    the SHA the runner actually checked out; falls back to `git rev-parse
    HEAD` against this package's own repo checkout for local runs. Returns
    `"unknown"` if neither is available (e.g. an installed, non-editable,
    non-git checkout) rather than raising — a missing code SHA shouldn't be
    able to crash an otherwise-successful run.
    """
    import os

    env_sha = os.environ.get("GITHUB_SHA")
    if env_sha:
        return env_sha

    repo_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip()


def make_run_id(started_at: datetime, code_sha: str) -> str:
    """`YYYY-MM-DDTHHMMSSZ-<shortsha>` (UTC), per issue #9's run_id format."""
    utc = started_at.astimezone(timezone.utc)
    return f"{utc.strftime('%Y-%m-%dT%H%M%SZ')}-{code_sha[:7]}"


class MetricsComputationError(RuntimeError):
    """Raised (internally) when metric computation fails.

    Not surfaced to callers of `run_pipeline` — it's caught there so the
    run's manifest and exit code can reflect ARCHITECTURE.md §7.3's "the
    deploy step is gated on that job's success" without an uncaught
    traceback replacing the manifest write.
    """


@dataclass(frozen=True)
class RunResult:
    """Return value of `run_pipeline`."""

    run_id: str
    manifest_path: Path
    manifest: dict[str, Any]
    exit_code: int


# --- Read-time dedupe (see module docstring) ---------------------------


def _dedupe_issue_rows(table: pa.Table) -> pa.Table:
    """Keep the latest `updated_at` row per `issue_key`."""
    if table.num_rows == 0:
        return table
    best: dict[str, dict[str, Any]] = {}
    for row in table.to_pylist():
        key = row["issue_key"]
        current = best.get(key)
        if current is None or row["updated_at"] > current["updated_at"]:
            best[key] = row
    kept = sorted(best.values(), key=lambda r: r["issue_key"])
    return pa.Table.from_pylist(kept, schema=table.schema)


def _dedupe_jira_review_events(table: pa.Table) -> pa.Table:
    """Keep the latest `occurred_at` row per `(issue_key, reviewer_raw_value)`.

    Only meaningful for `source == 'jira_field'` rows (see module
    docstring); this function is only ever called on the `raw/jira/
    review_event` table, which contains nothing else.
    """
    if table.num_rows == 0:
        return table
    best: dict[tuple[str | None, str], dict[str, Any]] = {}
    for row in table.to_pylist():
        key = (row["issue_key"], row["reviewer_raw_value"])
        current = best.get(key)
        if current is None or row["occurred_at"] > current["occurred_at"]:
            best[key] = row
    kept = sorted(
        best.values(), key=lambda r: (r["issue_key"] or "", r["reviewer_raw_value"], r["event_id"])
    )
    return pa.Table.from_pylist(kept, schema=table.schema)


def _dedupe_roster_entries(table: pa.Table) -> pa.Table:
    """Keep only the latest entry per `asf_id` (since roster is stateless).

    Roster data is collected fresh on every run from Whimsy's current state.
    Since it has no watermark, it accumulates across runs. This function
    dedupes to keep only the most recent snapshot of each member.
    """
    if table.num_rows == 0:
        return table
    best: dict[str, dict[str, Any]] = {}
    for row in table.to_pylist():
        asf_id = row["asf_id"]
        current = best.get(asf_id)
        # Keep the row from the most recent source_snapshot_id (lexicographically last)
        if current is None or row["source_snapshot_id"] > current["source_snapshot_id"]:
            best[asf_id] = row
    kept = sorted(best.values(), key=lambda r: r["asf_id"])
    return pa.Table.from_pylist(kept, schema=table.schema)


# --- Per-source collection -----------------------------------------------


def _collect_git(
    config: ProjectConfig,
    data_dir: Path,
    workdir: Path,
    run_id: str,
    started_at: datetime,
) -> dict[str, Any]:
    if not config.repos:
        return {
            "status": "failed",
            "watermark": None,
            "records_collected": 0,
            "last_good_snapshot": read_last_good_snapshot(data_dir, "git"),
            "reason": "no repos configured under projects/<id>.yaml `repos:`",
        }

    repo_cfg = config.repos[0]
    repo_label = f"{repo_cfg.owner}/{repo_cfg.name}"
    watermark = storage.read_watermark(data_dir, "git")
    # `file_change_event`'s own watermark (issue #53 fixup cycle 1 — the
    # "backfill gap"): tracked independently of `watermark` via
    # `storage.read_watermark`'s `table` parameter, precisely so a data dir
    # whose `git` watermark already reached HEAD before `file_change_event`
    # existed still backfills that table's full history on its first
    # collection, instead of silently inheriting `watermark`'s "already
    # caught up" position and never collecting anything for it.
    file_change_watermark = storage.read_watermark(data_dir, "git", table="file_change_event")
    snapshot_id = f"{run_id}:git"

    _log(
        "source_collect_started",
        source="git",
        repo=repo_label,
        watermark=watermark,
        file_change_watermark=file_change_watermark,
    )
    try:
        clone_or_fetch(github_clone_url(repo_cfg.owner, repo_cfg.name), workdir)
        result = GitCollector().collect(
            repo_path=workdir,
            repo_label=repo_label,
            default_branch=repo_cfg.default_branch,
            watermark=watermark,
            file_change_watermark=file_change_watermark,
            bot_patterns=config.bot_patterns,
            source_snapshot_id=snapshot_id,
            excluded_path_globs=config.truck_factor.excluded_path_globs,
        )
        partition_date = started_at.date()
        storage.write_partition(
            data_dir, "git", "contribution_event", partition_date, run_id, result.contribution_event
        )
        storage.write_partition(
            data_dir, "git", "file_change_event", partition_date, run_id, result.file_change_event
        )
        storage.write_partition(
            data_dir, "git", "review_event", partition_date, run_id, result.review_event
        )
        storage.write_watermark(data_dir, "git", result.next_watermark)
        # Both watermarks always advance to the same `next_watermark`: a
        # single `collect()` call always walks each table up to the same
        # `ref` (HEAD), regardless of which starting point (or none) each
        # table's own watermark gave it -- see collectors/git.py's docstring.
        storage.write_watermark(
            data_dir, "git", result.next_watermark, table="file_change_event"
        )
        record_last_good_snapshot(data_dir, "git", run_id)
        _log(
            "source_collect_succeeded",
            source="git",
            records_collected=result.commits_collected,
            file_changes_collected=result.file_changes_collected,
            bot_commits_excluded=result.bot_commits_excluded,
            placeholder_reviewer_commits=result.placeholder_reviewer_commits,
            next_watermark=result.next_watermark,
        )
        return {
            "status": "ok",
            "watermark": f"sha:{result.next_watermark}",
            "records_collected": result.commits_collected,
            # Data-quality signal (issue #18): commits whose trailer named
            # only a placeholder reviewer (`TBD`, `none`, `n/a`, ...) -- those
            # commits emit no review_event row, but the count itself is worth
            # surfacing in the manifest rather than silently dropped.
            "placeholder_reviewer_commits": result.placeholder_reviewer_commits,
            # issue #53 fixup cycle 1: surfaces in the manifest whether/how
            # much of file_change_event's own (independent) watermark range
            # was walked this run -- large on a first-ever backfill, small on
            # every steady-state run after.
            "file_changes_collected": result.file_changes_collected,
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="git", error=str(exc))
        return {
            "status": "failed",
            "watermark": watermark,
            "records_collected": 0,
            "last_good_snapshot": read_last_good_snapshot(data_dir, "git"),
            "reason": str(exc),
        }


def _collect_jira(
    config: ProjectConfig,
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    max_issues: int | None,
    collector_factory: Callable[[ProjectConfig], JiraCollector] | None,
) -> dict[str, Any]:
    watermark = storage.read_watermark(data_dir, "jira")
    snapshot_id = f"{run_id}:jira"

    _log("source_collect_started", source="jira", watermark=watermark)
    collector = (collector_factory or JiraCollector)(config)
    try:
        result = collector.collect(
            watermark=watermark, snapshot_id=snapshot_id, max_issues=max_issues
        )
        partition_date = started_at.date()
        storage.write_partition(data_dir, "jira", "issue", partition_date, run_id, result.issues)
        storage.write_partition(
            data_dir, "jira", "review_event", partition_date, run_id, result.review_events
        )
        if result.next_watermark:
            storage.write_watermark(data_dir, "jira", result.next_watermark)
        record_last_good_snapshot(data_dir, "jira", run_id)
        _log(
            "source_collect_succeeded",
            source="jira",
            records_collected=result.issue_count,
            review_event_count=result.review_event_count,
            next_watermark=result.next_watermark,
        )
        return {
            "status": "ok",
            "watermark": result.next_watermark,
            "records_collected": result.issue_count,
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="jira", error=str(exc))
        return {
            "status": "failed",
            "watermark": watermark,
            "records_collected": 0,
            "last_good_snapshot": read_last_good_snapshot(data_dir, "jira"),
            "reason": str(exc),
        }
    finally:
        collector.close()


def _collect_asf_roster(
    config: ProjectConfig,
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    collector_factory: Callable[[ProjectConfig], AsfRosterCollector] | None,
) -> dict[str, Any]:
    """Collect ASF roster data (PMC + committers) for metrics."""
    _log("source_collect_started", source="asf_roster")
    collector = (collector_factory or AsfRosterCollector)(config)
    try:
        result = collector.collect()
        # Store roster_entry to persistent storage so it's available for compute_all
        partition_date = started_at.date()
        storage.write_partition(
            data_dir, "asf_roster", "roster_entry", partition_date, run_id, result.roster_entries
        )
        _log(
            "source_collect_succeeded",
            source="asf_roster",
            records_collected=result.entry_count,
        )
        return {
            "status": "ok",
            "records_collected": result.entry_count,
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="asf_roster", error=str(exc))
        return {
            "status": "failed",
            "records_collected": 0,
            "reason": str(exc),
        }


def _collect_ponymail(
    config: ProjectConfig,
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    max_months_per_list: int | None,
    collector_factory: Callable[[ProjectConfig], PonyMailCollector] | None,
) -> dict[str, Any]:
    """Per-list watermarks (ARCHITECTURE.md §4.3) are stored as one JSON
    object under the `ponymail` source key in `state/watermarks.json` --
    `storage.write_watermark`'s value is an opaque string per source, so a
    `{"dev": "2026-08", "user": "2026-08"}`-shaped blob is what's read back
    and re-serialized here, the same read-modify-write contract every other
    source's single-string watermark uses.

    `max_months_per_list=None` (the CLI's `--max-ponymail-months` wasn't
    given) resolves to `config.mailing_lists.max_months_per_run` (issue #33
    fixup: a nightly run must not attempt an unbounded backfill of a
    long-lived list within `timeout-minutes: 60` alongside git/JIRA/roster)
    -- an explicit CLI value always wins over the config default.
    """
    raw_watermark = storage.read_watermark(data_dir, "ponymail")
    watermarks: dict[str, str | None] = json.loads(raw_watermark) if raw_watermark else {}
    snapshot_id = f"{run_id}:ponymail"

    effective_cap = max_months_per_list
    if effective_cap is None and config.mailing_lists is not None:
        effective_cap = getattr(config.mailing_lists, "max_months_per_run", None)

    _log(
        "source_collect_started",
        source="ponymail",
        watermark=watermarks,
        max_months_per_list=effective_cap,
    )
    collector = (collector_factory or PonyMailCollector)(config)
    try:
        result = collector.collect(
            watermarks=watermarks,
            snapshot_id=snapshot_id,
            max_months_per_list=effective_cap,
        )
        partition_date = started_at.date()
        storage.write_partition(
            data_dir, "ponymail", "message", partition_date, run_id, result.messages
        )
        storage.write_partition(
            data_dir, "ponymail", "message_thread", partition_date, run_id, result.message_threads
        )
        storage.write_watermark(
            data_dir, "ponymail", json.dumps(result.next_watermarks, sort_keys=True)
        )
        record_last_good_snapshot(data_dir, "ponymail", run_id)
        _log(
            "source_collect_succeeded",
            source="ponymail",
            records_collected=result.message_count,
            thread_count=result.thread_count,
            skipped_count=result.skipped_count,
            next_watermark=result.next_watermarks,
            backfill=result.backfill,
            partial=result.partial,
        )
        return {
            "status": "ok",
            "watermark": result.next_watermarks,
            "records_collected": result.message_count,
            "thread_count": result.thread_count,
            "skipped_count": result.skipped_count,
            # issue #33 fixup: lets the site/runbook tell "still backfilling"
            # (partial=True, per-list months_remaining > 0) from "done".
            "backfill": result.backfill,
            "partial": result.partial,
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="ponymail", error=str(exc))
        return {
            "status": "failed",
            "watermark": watermarks,
            "records_collected": 0,
            "last_good_snapshot": read_last_good_snapshot(data_dir, "ponymail"),
            "reason": str(exc),
        }
    finally:
        collector.close()


def _write_metrics_snapshot(data_dir: Path, run_id: str, metrics_table: pa.Table) -> Path:
    snapshot_dir = Path(data_dir) / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / "metrics.parquet"
    pq.write_table(metrics_table, path)
    return path


# --- Orchestration ----------------------------------------------------------


def run_pipeline(
    *,
    config: ProjectConfig,
    data_dir: str | Path,
    workdir: str | Path,
    sources: Sequence[str] | None = None,
    site_out: str | Path | None = None,
    max_jira_issues: int | None = None,
    max_ponymail_months: int | None = None,
    now: datetime | None = None,
    code_sha: str | None = None,
    identity_overrides_path: str | Path | None = None,
    trigger: str = "manual",
    jira_collector_factory: Callable[[ProjectConfig], JiraCollector] | None = None,
    asf_roster_collector_factory: Callable[[ProjectConfig], AsfRosterCollector] | None = None,
    ponymail_collector_factory: Callable[[ProjectConfig], PonyMailCollector] | None = None,
) -> RunResult:
    """Run one collect -> identity -> metrics -> manifest (-> site) pass.

    `now` and `code_sha` are injectable (not defaulted to wall-clock/`git
    rev-parse` internally) so tests can make a run fully deterministic;
    production callers (the CLI) leave both `None`.

    `jira_collector_factory` / `ponymail_collector_factory`, given, replace
    the default `JiraCollector(config)` / `PonyMailCollector(config)`
    construction — this is how tests inject a collector wired to an offline
    `httpx.MockTransport` (and a sleep-free retry loop) without
    `run_pipeline` needing to know about every one of the collector's tuning
    knobs.

    `asf_roster_collector_factory`, given, replaces the default
    `AsfRosterCollector(config)` construction — this is how tests inject an
    `AsfRosterCollector` wired to an offline `httpx.MockTransport`.
    """
    data_dir = Path(data_dir)
    workdir = Path(workdir)

    active_sources = list(sources) if sources else list(ALL_SOURCES)
    unknown = set(active_sources) - set(ALL_SOURCES)
    if unknown:
        raise ValueError(f"unknown source(s) {sorted(unknown)}; expected one of {ALL_SOURCES}")

    started_at = now if now is not None else datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    resolved_code_sha = code_sha or get_pipeline_code_sha()
    run_id = make_run_id(started_at, resolved_code_sha)

    _log(
        "run_started",
        run_id=run_id,
        sources=active_sources,
        pipeline_code_sha=resolved_code_sha,
    )

    source_results: dict[str, dict[str, Any]] = {}
    if "git" in active_sources:
        source_results["git"] = _collect_git(config, data_dir, workdir, run_id, started_at)
    if "jira" in active_sources:
        source_results["jira"] = _collect_jira(
            config, data_dir, run_id, started_at, max_jira_issues, jira_collector_factory
        )
    if "asf_roster" in active_sources:
        source_results["asf_roster"] = _collect_asf_roster(
            config, data_dir, run_id, started_at, asf_roster_collector_factory
        )
    if "ponymail" in active_sources:
        source_results["ponymail"] = _collect_ponymail(
            config,
            data_dir,
            run_id,
            started_at,
            max_ponymail_months,
            ponymail_collector_factory,
        )

    # D3: identity resolution and metrics always recompute from the ENTIRE
    # accumulated raw cache, regardless of which sources were active (or
    # failed) this run — a failed source's previous raw data is still used
    # (§7.3), and a source that wasn't asked to run this time still
    # contributes its prior history.
    contribution_event = storage.read_table(data_dir, "git", "contribution_event")
    file_change_event = storage.read_table(data_dir, "git", "file_change_event")
    git_review_event = storage.read_table(data_dir, "git", "review_event")
    jira_review_event = _dedupe_jira_review_events(
        storage.read_table(data_dir, "jira", "review_event")
    )
    review_event = pa.concat_tables([git_review_event, jira_review_event])
    issue = _dedupe_issue_rows(storage.read_table(data_dir, "jira", "issue"))
    roster_raw = storage.read_table(data_dir, "asf_roster", "roster_entry")
    roster_entry = _dedupe_roster_entries(roster_raw)

    overrides = []
    if identity_overrides_path is not None and Path(identity_overrides_path).is_file():
        overrides = load_overrides(identity_overrides_path)

    raw_identifiers = extract_raw_identifiers(
        contribution_events=contribution_event,
        review_events=review_event,
        issues=issue,
    )
    resolution = resolve_identities(raw_identifiers, overrides, now=started_at)

    metrics_table: pa.Table | None = None
    metrics_error: str | None = None
    try:
        metrics_table = compute_all(
            {
                "contribution_event": contribution_event,
                "file_change_event": file_change_event,
                "review_event": review_event,
                "issue": issue,
                "identity_link": resolution.identity_link,
                "roster_entry": roster_entry,
            },
            as_of=started_at.date(),
            run_id=run_id,
            computed_at=started_at,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any metrics
        # failure must exit non-zero and skip the site (§7.3), never
        # partially publish.
        metrics_error = f"{type(exc).__name__}: {exc}"
        _log("metrics_computation_failed", error=metrics_error)

    completed_at = datetime.now(timezone.utc)
    metrics_computed: list[str] = []
    metrics_missing: list[str] = []
    exit_code = 0

    if metrics_table is not None:
        _write_metrics_snapshot(data_dir, run_id, metrics_table)
        metric_rows = metrics_table.to_pylist()
        metrics_computed = sorted(
            {f"{row['metric_id']}@{row['definition_version']}" for row in metric_rows}
        )
        # A *registered* metric (metrics.registry.METRIC_IDS) that produced
        # zero metric_value rows this run is a silent failure -- most often a
        # contract mismatch between a collector and the engine (issue #24) --
        # not the same thing as a metric with data below its sample floor
        # (that still emits an `insufficient_data` row and is never
        # "missing"). Surface it loudly rather than letting the manifest say
        # `status: ok` while a metric quietly produced nothing.
        computed_metric_ids = {row["metric_id"] for row in metric_rows}
        metrics_missing = sorted(set(METRIC_IDS) - computed_metric_ids)
        _log("metrics_computed", run_id=run_id, metrics=metrics_computed)
        if metrics_missing:
            _log("metrics_missing", run_id=run_id, metrics_missing=metrics_missing)
            exit_code = 1
    else:
        exit_code = 1

    if metrics_table is None:
        run_status = "failed"
    elif metrics_missing:
        run_status = "degraded"
    else:
        run_status = "ok"

    manifest = build_manifest(
        run_id=run_id,
        trigger=trigger,
        started_at=started_at,
        completed_at=completed_at,
        pipeline_code_sha=resolved_code_sha,
        sources=source_results,
        metrics_computed=metrics_computed,
        data_branch_commit=None,
        site_deploy_status=None,
        status=run_status,
        error=metrics_error,
        metrics_missing=metrics_missing,
    )

    out_path = manifest_path(data_dir, run_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    _log("manifest_written", run_id=run_id, path=str(out_path))

    # ARCHITECTURE.md §7.3: a metrics-stage failure means the site is never
    # (re)generated — the previous good deployment stays live.
    if metrics_table is not None and site_out is not None:
        generate_site(data_dir, run_id, site_out, now=completed_at)
        manifest["site_deploy_status"] = "ok"
        out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        _log("site_generated", run_id=run_id, out_dir=str(site_out))

    _log("run_completed", run_id=run_id, status=manifest["status"], exit_code=exit_code)

    return RunResult(run_id=run_id, manifest_path=out_path, manifest=manifest, exit_code=exit_code)
