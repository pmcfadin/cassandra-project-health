"""Pipeline runner (ARCHITECTURE.md §4.2-4.4, §5, §7.3, §11; issue #9).

`run_pipeline(...)` orchestrates one collection + metrics + (optional) site
run, end to end:

1. For each active source (`git`, `jira`, `asf_roster`, `github_profile`):
   collect from that source's stored watermark, write the validated raw
   partitions
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
  'commit_trailer'`) are **not** deduped on overlap the way JIRA's are:
  `collectors/git.py`'s watermark is an exact, exclusive commit-SHA range,
  so the same commit is never re-collected by the ordinary incremental walk,
  and that source's `event_id` is a deterministic function of `(repo, sha,
  reviewer, issue_key)` — a duplicate `event_id` there would indicate a bug,
  not an expected overlap.

  They **are** deduped on a different axis — `parser_version` (issue #77):
  `collectors/reviewer_trailer.py`'s `PARSER_VERSION` is bumped whenever a
  parsing-behavior change (e.g. issue #77's line-wrap fix) can change what
  an already-collected commit's trailer parses to. Since the watermark means
  that commit will never be re-walked by the ordinary incremental collector,
  a version bump also triggers a one-time, full-history re-derivation
  (`collectors/git.py`'s `derive_review_events`, called from `_collect_git`
  below) that appends a *new* `review_event` partition covering every
  commit, stamped with the new `parser_version` — never rewriting or
  deleting the original rows. `_dedupe_commit_trailer_review_events` then
  keeps, per commit (the sha embedded in `event_id`, since older rows
  predate any `source_ref`-style column), only the row(s) at that commit's
  *highest* `parser_version` — so the corrected reviewer set replaces the
  stale one at read time, exactly like the JIRA dedup above does for a
  different reason. `collectors/governance_git.py`'s `commit_record` table
  gets the identical treatment, keyed on its own plain `sha` column, via
  `_dedupe_governance_commit_records`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from project_health import storage
from project_health.leaderboard import build_leaderboards
from project_health.collectors.asf_roster import AsfRosterCollector
from project_health.collectors.git import (
    GitCollector,
    clone_or_fetch,
    derive_review_events,
    github_clone_url,
)
from project_health.collectors.github import GitHubCollector, resolve_github_token
from project_health.collectors.reviewer_trailer import PARSER_VERSION
from project_health.collectors.github_commit_authors import GitHubCommitAuthorCollector
from project_health.collectors.github_profile import GitHubProfileCollector
from project_health.collectors.jira import JiraCollector
from project_health.collectors.ponymail import PonyMailCollector
from project_health.collectors.security import SecurityCollector
from project_health.config import ProjectConfig
from project_health.metrics import METRIC_IDS, compute_all
from project_health.normalize.affiliation import (
    DEFAULT_GITHUB_COMPANY_LOOKBACK_MONTHS,
    build_affiliation_periods,
    load_affiliations_file,
    load_org_aliases,
    load_org_domains,
)
from project_health.normalize.identity import (
    extract_raw_identifiers,
    link_github_commit_authors,
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

# --- Governance compliance engine (issue #36) -------------------------------
#
# Kept in its own clearly-delimited block, called once from `run_pipeline`
# (see `_collect_governance` and its call site below), so this addition
# stays easy to isolate/rebase against #33/#42's parallel work on the
# shared collection functions above.
from project_health.collectors.github_checks import GitHubChecksCollector
from project_health.collectors.governance_git import CommitRecord, collect_commits, resolve_sha
from project_health.collectors.jira_comments import JiraCommentsCollector
from project_health.governance.checks import CheckstyleEvidence, CIEvidence, CommitFacts
from project_health.governance.engine import build_commit_compliance_rows, build_commit_facts_rows
from project_health.governance.metrics import compute_monthly_check_metrics
from project_health.governance.overrides import DEFAULT_OVERRIDES_PATH
from project_health.governance.overrides import load_overrides as load_governance_overrides
from project_health.governance.policy import DEFAULT_POLICY_PATH, load_policy
from project_health.governance.registry import build_governance_registry
from project_health.schema import get_schema as _governance_get_schema
from project_health.schema import validate as _governance_validate

ALL_SOURCES: tuple[str, ...] = (
    "git",
    "jira",
    "asf_roster",
    "ponymail",
    "governance",
    "security",
    "github_commit_authors",
    "github_profile",
    # issue #54: wires collectors/github.py (merged as part of issue #51,
    # PR #58) into the pipeline for the first time -- see _collect_github.
    "github",
)

# --- Governance evidence-collection tuning (issue #36 fixup cycle 1) --------
#
# Conservative per-run defaults, used whenever `projects/<id>.yaml` doesn't
# set its own `governance.max_github_calls_per_run` /
# `governance.max_jira_calls_per_run` — see `_governance_budget` below. A
# nightly GitHub Actions run has a 60-minute timeout and a shared
# ~1,000 req/hr token; at `github_checks.DEFAULT_MIN_REQUEST_INTERVAL`
# (0.25s/call) 500 calls costs ~125s, and at
# `jira_comments.DEFAULT_MIN_REQUEST_INTERVAL` (0.55s/call) 300 calls costs
# ~165s — both comfortably inside the timeout alongside the rest of the
# pipeline (git/jira collection, metrics, site generation).
DEFAULT_GOVERNANCE_MAX_GITHUB_CALLS_PER_RUN = 500
DEFAULT_GOVERNANCE_MAX_JIRA_CALLS_PER_RUN = 300

# --- JIRA issue_comment historical backfill (issue #79) ---------------------
#
# Issue #54 added comment-metadata collection to `collectors/jira.py`'s
# existing `/search` fetch, but that only ever covers issues fetched *after*
# #54 landed — production's JIRA watermark was already current when #54
# shipped, so the ~21.5k pre-existing issues have no `issue_comment` rows and
# never will without a dedicated backfill (`_collect_jira_comment_backfill`
# below). ~1,000 issues/run at `jira_comments.DEFAULT_MIN_REQUEST_INTERVAL`
# (0.55s/call, one call per issue) costs ~9-10 minutes — comfortably inside a
# nightly run's budget alongside everything else — and clears the full
# backlog in roughly 2-3 weeks, same order of magnitude as the issue's own
# estimate. Configurable via `projects/<id>.yaml`'s
# `jira_comment_backfill.max_issues_per_run` (`_jira_comment_backfill_budget`).
DEFAULT_JIRA_COMMENT_BACKFILL_MAX_ISSUES_PER_RUN = 1000

# `code-style-checkstyle`'s GitHub check-run re-fetch window (issue #36
# fixup cycle 1): a check-run that's still pending/absent is only worth
# re-checking while its commit is recent — GitHub's own check-run/workflow
# history rolls off well before this (docs/spec/GOVERNANCE.md §10's
# checkstyle-since-2024 finding: only 25.5% of a 21-month window still had
# a retrievable run at all), so retrying a `unknown`-status old commit
# forever would just burn budget for a result that will never resolve.
GOVERNANCE_CHECK_RUN_RETRY_WINDOW_DAYS = 30

# `code-style-checkstyle`'s GitHub check-run *retention* horizon (issue #36
# fixup cycle 2, distinct from the 30-day *re-fetch* window above): beyond
# this many days old, a commit is never fetched at all — not even once —
# because docs/spec/GOVERNANCE.md §10's live finding says GitHub's own
# check-run history has almost certainly already rolled off, so a fetch
# would just spend budget to learn nothing. Configurable via
# `projects/<id>.yaml`'s `governance.checkstyle_retention_days`
# (`_governance_checkstyle_retention_days`).
DEFAULT_GOVERNANCE_CHECKSTYLE_RETENTION_DAYS = 400

# GitHub Checks API conclusions that are a definitive failure on their own
# (mirrors `governance.checks._FAILING_CONCLUSIONS`) — used by
# `_governance_check_run_latest_is_resolved_map` to decide a sha's latest
# fetch batch is final: any one of these means "resolved", regardless of
# what the *other* checkstyle run name in that same batch says (a red
# `ant-check-jdk11` is definitive even while `ant-check-jdk17` is still
# queued). Absent a failure, every row in the batch must be `success` for
# the batch to count as resolved.
_GOVERNANCE_FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required"}
)

# The `governance_check_run` / `governance_ci_evidence` sentinel value
# recorded when a fetch found nothing at all (as opposed to "not yet
# fetched") — never a real GitHub check-run name.
_GOVERNANCE_NO_RUN_FOUND_SENTINEL = "__none_found__"

# github_profile issue #52 fixup cycle 1 (orchestrator feedback): "roughly
# 700 logins over history, budgeted per run" -- a sane per-run default so a
# single run doesn't try to fetch all of them in one shot even when the
# caller doesn't pass an explicit --max-github-profiles. Overridable via
# `max_github_profiles=` / `--max-github-profiles`; the already-cached-login
# skip (`_collect_github_profile`) means unfetched logins carry over and
# converge across a handful of nightly runs.
DEFAULT_MAX_GITHUB_PROFILES_PER_RUN = 300


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


def _commit_sha_from_commit_trailer_event_id(event_id: str) -> str | None:
    """The commit sha embedded in a `commit_trailer` `review_event.event_id`
    (issue #77): `collectors/git.py` always mints
    `f"git:{repo_label}:{sha}:review:{reviewer}:{issue_key or 'none'}"`.

    Used as the dedup key instead of a `source_ref`-style column because a
    row collected before issue #77 has no such column (schemas only ever
    gain new *nullable* fields, never rewritten in place, D3) — but every
    row, old or new, already has this exact `event_id` shape, so parsing it
    is the one grouping key that reaches all the way back to the very first
    `commit_trailer` row ever collected. `maxsplit=3` is what keeps this
    correct even if `reviewer` itself contains a `:` (unlikely, but the
    format doesn't forbid it): the sha is always the third `:`-delimited
    field, regardless of what's packed into the remainder.

    Returns `None` for an `event_id` that doesn't match this shape (should
    never happen for a real `commit_trailer` row; defensive rather than
    raising, so one malformed row can't break dedup for every other row).
    """
    parts = event_id.split(":", 3)
    if len(parts) < 4 or parts[0] != "git" or not parts[3].startswith("review:"):
        return None
    return parts[2]


def _dedupe_commit_trailer_review_events(table: pa.Table) -> pa.Table:
    """Keep only the highest-`parser_version` row(s) per commit (issue #77).

    See the module docstring's "Read-time dedupe" section for the full
    design: a `PARSER_VERSION` bump triggers a full-history re-derivation
    that appends new, higher-`parser_version` `review_event` rows for every
    commit rather than rewriting the originals (append-only, D3). This is
    what makes that re-derivation actually *replace* a stale attribution at
    metrics/leaderboard read time — a row with a `None` `parser_version`
    (collected before this column existed) is treated as version 1, the
    original implicit version.

    Only meaningful for the `raw/git/review_event` table, which contains
    only `source == 'commit_trailer'` rows.
    """
    if table.num_rows == 0:
        return table
    rows = table.to_pylist()
    best_version: dict[str, int] = {}
    for row in rows:
        sha = _commit_sha_from_commit_trailer_event_id(row["event_id"])
        if sha is None:
            continue
        version = row["parser_version"] if row["parser_version"] is not None else 1
        if sha not in best_version or version > best_version[sha]:
            best_version[sha] = version
    kept = []
    for row in rows:
        sha = _commit_sha_from_commit_trailer_event_id(row["event_id"])
        version = row["parser_version"] if row["parser_version"] is not None else 1
        # A row whose event_id doesn't parse (should never happen for a real
        # commit_trailer row) is kept rather than silently dropped -- never
        # worse than not deduping at all.
        if sha is None or version == best_version[sha]:
            kept.append(row)
    kept.sort(key=lambda r: r["event_id"])
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


def _dedupe_security_advisories(table: pa.Table) -> pa.Table:
    """Keep the latest `collected_at` row per `cve_id` (issue #55).

    Unlike `scorecard_check` (every run's rows are all kept -- that's the
    Scorecard *history*, deliberately never deduped), an advisory's own
    identity is its CVE id: a re-fetch of the same CVE (NVD's own metadata
    can change, e.g. a CVSS score correction or a newly added reference) is
    a refreshed snapshot of the same fact, not a new historical event. Same
    "dedupe at read time over immutable append-only raw data" pattern as
    `_dedupe_issue_rows`.
    """
    if table.num_rows == 0:
        return table
    best: dict[str, dict[str, Any]] = {}
    for row in table.to_pylist():
        cve_id = row["cve_id"]
        current = best.get(cve_id)
        if current is None or row["collected_at"] > current["collected_at"]:
            best[cve_id] = row
    kept = sorted(best.values(), key=lambda r: r["cve_id"])
    return pa.Table.from_pylist(kept, schema=table.schema)


def _dedupe_message_rows(table: pa.Table) -> pa.Table:
    """Keep the latest `source_snapshot_id` row per `message_id` (issue #35).

    `collectors/ponymail.py`'s watermark strategy always re-fetches the
    list's current month in full on every run (ARCHITECTURE.md §4.3), so the
    same `message_id` can land in more than one run's raw partition before
    that month completes. `source_snapshot_id` is `f"{run_id}:ponymail"`
    (collectors/ponymail.py), and `run_id`s sort lexicographically by
    collection time (same convention `_dedupe_roster_entries` relies on), so
    "latest `source_snapshot_id`" is "latest run that saw this message" --
    content for a given `message_id` never actually changes between runs
    (it's an immutable archived email), so this is a pure dedupe, not a
    "pick the freshest fact" resolution the way `_dedupe_issue_rows` is.
    """
    if table.num_rows == 0:
        return table
    best: dict[str, dict[str, Any]] = {}
    for row in table.to_pylist():
        message_id = row["message_id"]
        current = best.get(message_id)
        if current is None or row["source_snapshot_id"] > current["source_snapshot_id"]:
            best[message_id] = row
    kept = sorted(best.values(), key=lambda r: r["message_id"])
    return pa.Table.from_pylist(kept, schema=table.schema)


def _dedupe_pr_rows(table: pa.Table) -> pa.Table:
    """Keep the latest `updated_at` row per `(repo, number)` (issue #54).

    `collectors/github.py`'s opaque-cursor watermark deliberately re-fetches
    a PR whose `updatedAt` has moved since the prior run (module docstring
    "Watermark strategy") -- same "raw is append-only, dedupe at read time"
    contract `_dedupe_issue_rows` uses for JIRA issues.
    """
    if table.num_rows == 0:
        return table
    best: dict[tuple[str, int], dict[str, Any]] = {}
    for row in table.to_pylist():
        key = (row["repo"], row["number"])
        current = best.get(key)
        if current is None or row["updated_at"] > current["updated_at"]:
            best[key] = row
    kept = sorted(best.values(), key=lambda r: (r["repo"], r["number"]))
    return pa.Table.from_pylist(kept, schema=table.schema)


def _dedupe_pr_review_rows(table: pa.Table) -> pa.Table:
    """Keep one row per `review_id` (issue #54).

    A re-fetched PR (its `updatedAt` moved) re-emits its *entire* reviews
    connection, including reviews already collected -- `review_id` is
    GitHub's own immutable GraphQL node id, an exact natural key, so any one
    occurrence is as good as another (unlike `_dedupe_pr_rows`, there's no
    "later snapshot is more current" distinction to make).
    """
    if table.num_rows == 0:
        return table
    best: dict[str, dict[str, Any]] = {}
    for row in table.to_pylist():
        best.setdefault(row["review_id"], row)
    kept = sorted(best.values(), key=lambda r: r["review_id"])
    return pa.Table.from_pylist(kept, schema=table.schema)


def _dedupe_issue_comment_rows(table: pa.Table) -> pa.Table:
    """Keep one row per `comment_id` (issue #54) -- same natural-key dedupe
    as `_dedupe_pr_review_rows`, for the same "re-fetched issue re-emits its
    comment metadata" reason (`collectors/jira.py`'s own watermark overlap,
    module docstring "Watermark strategy")."""
    if table.num_rows == 0:
        return table
    best: dict[str, dict[str, Any]] = {}
    for row in table.to_pylist():
        best.setdefault(row["comment_id"], row)
    kept = sorted(best.values(), key=lambda r: r["comment_id"])
    return pa.Table.from_pylist(kept, schema=table.schema)


# --- Per-source collection -----------------------------------------------


def _reparse_commit_trailer_review_events_if_needed(
    data_dir: Path,
    workdir: Path,
    repo_cfg: Any,
    repo_label: str,
    run_id: str,
    partition_date: date,
    bot_patterns: Sequence[Any],
    had_prior_history: bool,
) -> dict[str, Any]:
    """One-time, full-history re-derivation of `commit_trailer` `review_event`
    rows when `reviewer_trailer.PARSER_VERSION` has bumped since this data
    dir's last run (issue #77 — see the module docstring's "Read-time
    dedupe" section for the full design).

    `had_prior_history` is `watermark is not None` from *before* this run's
    own incremental collection — a fresh data dir (never collected before)
    needs no reparse, since every row it's about to write already uses the
    current parser. Marked via its own watermark key
    (`table="review_event_parser_version"`, storing `str(PARSER_VERSION)`
    the same way a real commit-sha watermark stores a sha) so this is a
    no-op on every run after the one that actually reparses.

    Returns a stats dict for the manifest: `status` is `'skipped'` (no bump
    pending), `'ok'` (reparsed and written), or `'failed'` (reparse raised —
    logged, but never propagated: the ordinary incremental collection this
    run already succeeded and must not be rolled back over a supplementary
    correction pass failing).
    """
    marker = storage.read_watermark(data_dir, "git", table="review_event_parser_version")
    if marker == str(PARSER_VERSION) or not had_prior_history:
        if marker != str(PARSER_VERSION):
            storage.write_watermark(
                data_dir, "git", str(PARSER_VERSION), table="review_event_parser_version"
            )
        return {"status": "skipped", "parser_version": PARSER_VERSION}

    _log(
        "commit_trailer_reparse_started",
        source="git",
        from_marker=marker,
        to_parser_version=PARSER_VERSION,
    )
    try:
        reparse_result = derive_review_events(
            repo_path=workdir,
            repo_label=repo_label,
            default_branch=repo_cfg.default_branch,
            bot_patterns=bot_patterns,
            source_snapshot_id=f"{run_id}:git:reparse",
        )
        storage.write_partition(
            data_dir,
            "git",
            "review_event",
            partition_date,
            f"{run_id}-reparse",
            reparse_result.review_event,
        )
        storage.write_watermark(
            data_dir, "git", str(PARSER_VERSION), table="review_event_parser_version"
        )
        _log(
            "commit_trailer_reparse_succeeded",
            source="git",
            commits_scanned=reparse_result.commits_scanned,
            review_rows_written=reparse_result.review_event.num_rows,
            unparsed_reviewed_by_count=reparse_result.unparsed_reviewed_by_count,
            placeholder_reviewer_commits=reparse_result.placeholder_reviewer_commits,
        )
        return {
            "status": "ok",
            "parser_version": PARSER_VERSION,
            "commits_scanned": reparse_result.commits_scanned,
            "review_rows_written": reparse_result.review_event.num_rows,
        }
    except Exception as exc:  # noqa: BLE001 - never roll back this run's own successful collection
        _log("commit_trailer_reparse_failed", source="git", error=str(exc))
        return {"status": "failed", "parser_version": PARSER_VERSION, "reason": str(exc)}


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
        # issue #77: a `PARSER_VERSION` bump triggers a one-time, full-history
        # re-derivation of `commit_trailer` `review_event` rows -- see
        # `_reparse_commit_trailer_review_events_if_needed`'s docstring. Runs
        # after the ordinary incremental collection above has already
        # succeeded and advanced its watermark; a reparse failure is reported
        # but never turns this (already-successful) source result `failed`.
        reviewer_reparse = _reparse_commit_trailer_review_events_if_needed(
            data_dir,
            workdir,
            repo_cfg,
            repo_label,
            run_id,
            partition_date,
            config.bot_patterns,
            had_prior_history=watermark is not None,
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
            # issue #77: reports whether a commit_trailer parser-version
            # bump's full-history reparse ran this run.
            "commit_trailer_reparse": reviewer_reparse,
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


def _record_jira_comment_checked(
    data_dir: Path,
    partition_date: date,
    run_id: str,
    issues: pa.Table,
    comments: pa.Table,
    checked_via: str,
) -> None:
    """Append one `comment_backfill_checked` row (issue #79) per issue in
    `issues`, regardless of whether `comments` has any rows for it -- so a
    genuinely zero-comment issue is never mistaken for "never checked" and
    endlessly re-fetched by `_collect_jira_comment_backfill` below. Called
    both after the ordinary incremental `/search` fetch (`checked_via=
    'incremental'`) and after a backfill batch (`checked_via='backfill'`,
    see that function) -- either one fully covers the issues it touches.
    A no-op when `issues` is empty (nothing to mark checked).
    """
    if issues.num_rows == 0:
        return
    checked_at = datetime.now(timezone.utc)
    comment_counts: dict[str, int] = {}
    for row in comments.to_pylist():
        comment_counts[row["issue_key"]] = comment_counts.get(row["issue_key"], 0) + 1
    snapshot_id = f"{run_id}:jira_comment_checked"
    seen: dict[str, None] = {}
    for row in issues.to_pylist():
        seen.setdefault(row["issue_key"], None)
    rows = [
        {
            "issue_key": issue_key,
            "checked_at": checked_at,
            "comment_count": comment_counts.get(issue_key, 0),
            "checked_via": checked_via,
            "source_snapshot_id": snapshot_id,
        }
        for issue_key in seen
    ]
    table = pa.Table.from_pylist(rows, schema=_governance_get_schema("comment_backfill_checked"))
    storage.write_partition(
        data_dir, "jira", "comment_backfill_checked", partition_date, run_id, table
    )


def _jira_comment_backfill_budget(config: ProjectConfig) -> int:
    """`max_issues_per_run` for the historical `issue_comment` backfill
    (issue #79) from `projects/<id>.yaml`'s optional `jira_comment_backfill:`
    block, defaulting to `DEFAULT_JIRA_COMMENT_BACKFILL_MAX_ISSUES_PER_RUN` --
    same "arbitrary extra top-level key, read defensively" pattern as
    `_governance_budget` above (`ProjectConfig`'s own `extra="allow"`)."""
    raw = getattr(config, "jira_comment_backfill", None)
    if not isinstance(raw, dict):
        raw = {}
    return int(raw.get("max_issues_per_run", DEFAULT_JIRA_COMMENT_BACKFILL_MAX_ISSUES_PER_RUN))


def _jira_comment_backfill_checked_keys(data_dir: Path) -> set[str]:
    """Every `issue_key` the accumulated `comment_backfill_checked` table has
    a row for, from *either* an ordinary incremental fetch or a prior
    backfill run -- both are equally "covered" (see `_record_jira_comment_
    checked`'s docstring)."""
    table = storage.read_table(data_dir, "jira", "comment_backfill_checked")
    return set(table.column("issue_key").to_pylist())


def _jira_comment_backfill_eligible_keys_newest_first(
    data_dir: Path, checked: set[str]
) -> list[str]:
    """Every `issue_key` in the accumulated `jira/issue` table not yet in
    `checked` (issue #79), newest `created_at` first -- so a tight per-run
    budget resolves the most recently opened historical issues before
    working further back, the same newest-first bias
    `_ci_eligible_issue_keys_newest_first` applies to a different evidence
    backlog."""
    table = storage.read_table(data_dir, "jira", "issue")
    by_key: dict[str, datetime] = {}
    for row in table.to_pylist():
        key = row["issue_key"]
        created_at = row["created_at"]
        if key not in by_key or created_at > by_key[key]:
            by_key[key] = created_at
    eligible = [key for key in by_key if key not in checked]
    return sorted(eligible, key=lambda key: by_key[key], reverse=True)


def _collect_jira_comment_backfill(
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    max_issues: int,
    jira_base_url: str | None,
    jira_comments_factory: Callable[[str], object] | None,
) -> dict[str, Any]:
    """Budgeted, resumable historical backfill of `issue_comment` metadata
    for `time_to_first_response_jira` (issue #79) -- fetches up to
    `max_issues` issues that have never had their comments checked
    (`_jira_comment_backfill_checked_keys`), newest-created first, at the
    same JIRA politeness pacing `collectors/jira_comments.py`'s
    `JiraCommentsCollector` already uses for governance's CI-evidence
    backfill (`_collect_governance_ci_evidence` above). Modeled directly on
    that function's budget/resume/never-fail shape.

    Never raises and never marks the `jira` source `'failed'` on its own
    account -- a partial backfill is a legitimate, resumable state (this
    run's leftover backlog is simply `pending` for the next one), not an
    outage; an exception constructing the collector, or fetching one issue's
    comments, is logged and that issue is left `pending` rather than
    aborting the batch.

    Returns `{checked, pending, calls_made}` (recorded on the manifest under
    `sources.jira.backfill`), the same shape
    `site.manifest.GovernanceEvidenceStats` already models for governance's
    equivalent per-run backlog stats.
    """
    stats: dict[str, Any] = {"checked": 0, "pending": 0, "calls_made": 0}
    if not jira_base_url or max_issues <= 0:
        return stats

    checked = _jira_comment_backfill_checked_keys(data_dir)
    eligible = _jira_comment_backfill_eligible_keys_newest_first(data_dir, checked)
    if not eligible:
        return stats

    to_fetch = eligible[:max_issues]

    try:
        collector = (jira_comments_factory or JiraCommentsCollector)(jira_base_url)
    except Exception as exc:  # noqa: BLE001 - an evidence source's outage must not abort the run.
        _log("jira_comment_backfill_failed", error=str(exc))
        stats["pending"] = len(eligible)
        return stats

    checked_at = datetime.now(timezone.utc)
    comment_rows: list[dict[str, Any]] = []
    checked_rows: list[dict[str, Any]] = []
    snapshot_id = f"{run_id}:jira_comment_backfill"
    try:
        for issue_key in to_fetch:
            try:
                found_rows = collector.fetch_comment_metadata(issue_key)
            except Exception as exc:  # noqa: BLE001 - one bad issue must not stop the batch.
                _log("jira_comment_backfill_issue_failed", issue_key=issue_key, error=str(exc))
                continue
            for row in found_rows:
                comment_rows.append({**row, "source_snapshot_id": snapshot_id})
            checked_rows.append(
                {
                    "issue_key": issue_key,
                    "checked_at": checked_at,
                    "comment_count": len(found_rows),
                    "checked_via": "backfill",
                    "source_snapshot_id": snapshot_id,
                }
            )
            stats["checked"] += 1
    finally:
        stats["calls_made"] = collector.call_count
        collector.close()

    stats["pending"] = len(eligible) - stats["checked"]

    # A separate `run_id` suffix (matches `_reparse_governance_commit_
    # records_if_needed`'s own `f"{run_id}-reparse"`) -- `_collect_jira`'s
    # ordinary incremental fetch already wrote a `part-<run_id>.parquet`
    # partition for these same two tables this run; reusing plain `run_id`
    # here would collide with `storage.write_partition`'s
    # never-overwrite guarantee.
    backfill_run_id = f"{run_id}-comment-backfill"
    partition_date = started_at.date()
    if comment_rows:
        comment_table = pa.Table.from_pylist(
            comment_rows, schema=_governance_get_schema("issue_comment")
        )
        storage.write_partition(
            data_dir, "jira", "issue_comment", partition_date, backfill_run_id, comment_table
        )
    if checked_rows:
        checked_table = pa.Table.from_pylist(
            checked_rows, schema=_governance_get_schema("comment_backfill_checked")
        )
        storage.write_partition(
            data_dir,
            "jira",
            "comment_backfill_checked",
            partition_date,
            backfill_run_id,
            checked_table,
        )

    _log(
        "jira_comment_backfill_completed",
        run_id=run_id,
        checked=stats["checked"],
        pending=stats["pending"],
        calls_made=stats["calls_made"],
    )
    return stats


def _collect_jira(
    config: ProjectConfig,
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    max_issues: int | None,
    collector_factory: Callable[[ProjectConfig], JiraCollector] | None,
    jira_comment_backfill_factory: Callable[[str], object] | None = None,
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
        # issue #54: comment metadata rides along on the same JIRA search
        # request (collectors/jira.py module docstring) -- zero extra API
        # calls beyond what this source already spends.
        storage.write_partition(
            data_dir, "jira", "issue_comment", partition_date, run_id, result.comments
        )
        # issue #79: every issue this run fetched (regardless of comment
        # count) is now current on comments -- record that so the historical
        # backfill below never re-treats it as pending.
        _record_jira_comment_checked(
            data_dir,
            partition_date,
            run_id,
            result.issues,
            result.comments,
            checked_via="incremental",
        )
        if result.next_watermark:
            storage.write_watermark(data_dir, "jira", result.next_watermark)
        record_last_good_snapshot(data_dir, "jira", run_id)
        _log(
            "source_collect_succeeded",
            source="jira",
            records_collected=result.issue_count,
            review_event_count=result.review_event_count,
            comment_count=result.comment_count,
            next_watermark=result.next_watermark,
        )
        source_result: dict[str, Any] = {
            "status": "ok",
            "watermark": result.next_watermark,
            "records_collected": result.issue_count,
            "comment_count": result.comment_count,
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="jira", error=str(exc))
        source_result = {
            "status": "failed",
            "watermark": watermark,
            "records_collected": 0,
            "last_good_snapshot": read_last_good_snapshot(data_dir, "jira"),
            "reason": str(exc),
        }
    finally:
        collector.close()

    # issue #79: the budgeted, resumable historical `issue_comment` backfill
    # -- its own try/except (inside `_collect_jira_comment_backfill`) means a
    # backfill hiccup never turns an otherwise-successful ordinary fetch
    # above into a `'failed'` jira source, and a partial backfill never
    # marks this run degraded (it isn't part of `metrics.registry.
    # METRIC_IDS`'s `metrics_missing` check).
    jira_base_url = (
        getattr(config.issue_tracker, "base_url", None) if config.issue_tracker else None
    )
    source_result["backfill"] = _collect_jira_comment_backfill(
        data_dir,
        run_id,
        started_at,
        _jira_comment_backfill_budget(config),
        jira_base_url,
        jira_comment_backfill_factory,
    )
    return source_result


def _collect_github(
    config: ProjectConfig,
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    max_prs_per_repo: int | None,
    collector_factory: Callable[[ProjectConfig], GitHubCollector] | None,
) -> dict[str, Any]:
    """Collect GitHub PR/review/comment metadata (issue #51's collector,
    wired into the pipeline for the first time by issue #54).

    Per-repo watermarks (an opaque GraphQL cursor per repo,
    `collectors/github.py` module docstring) are stored as one JSON object
    under the `github` source key in `state/watermarks.json` -- the same
    read-modify-write contract `_collect_ponymail`'s per-list watermark blob
    uses. The per-run API budget is `GitHubCollector`'s own
    `rate_limit_floor` (module default 500 GraphQL points remaining):
    `collect()` stops each repo cleanly once that floor is hit and marks
    every subsequent configured repo `'skipped'`, so a large first backfill
    across `pull_requests.repos` (7 repos for Cassandra) legitimately spans
    several nightly runs -- reflected here as `status: 'partial'`, not
    `'failed'` (`collectors/github.py`'s three-valued `GitHubCollectionResult
    .status`).
    """
    raw_watermark = storage.read_watermark(data_dir, "github")
    watermarks: dict[str, str | None] = json.loads(raw_watermark) if raw_watermark else {}
    snapshot_id = f"{run_id}:github"

    _log("source_collect_started", source="github", watermarks=watermarks)
    collector = (collector_factory or GitHubCollector)(config)
    try:
        result = collector.collect(
            watermarks=watermarks, snapshot_id=snapshot_id, max_prs_per_repo=max_prs_per_repo
        )
        partition_date = started_at.date()
        storage.write_partition(data_dir, "github", "pr", partition_date, run_id, result.prs)
        storage.write_partition(
            data_dir, "github", "pr_review", partition_date, run_id, result.reviews
        )
        storage.write_partition(
            data_dir, "github", "pr_comment", partition_date, run_id, result.comments
        )
        next_watermarks = {repo: outcome.next_watermark for repo, outcome in result.repos.items()}
        storage.write_watermark(data_dir, "github", json.dumps(next_watermarks, sort_keys=True))
        # 'failed' means at least one repo hit a hard CollectionError -- still
        # record last_good_snapshot when it's merely 'partial' (a clean,
        # resumable rate-limit stop, not an outage): the data collected this
        # run is valid and already written.
        if result.status != "failed":
            record_last_good_snapshot(data_dir, "github", run_id)
        repo_statuses = {repo: outcome.status for repo, outcome in result.repos.items()}
        _log(
            "source_collect_succeeded",
            source="github",
            status=result.status,
            pr_count=result.prs.num_rows,
            review_count=result.reviews.num_rows,
            comment_count=result.comments.num_rows,
            repos=repo_statuses,
        )
        return {
            "status": result.status,
            "watermark": next_watermarks,
            "records_collected": result.prs.num_rows,
            "review_count": result.reviews.num_rows,
            "comment_count": result.comments.num_rows,
            "repos": repo_statuses,
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="github", error=str(exc))
        return {
            "status": "failed",
            "watermark": watermarks,
            "records_collected": 0,
            "last_good_snapshot": read_last_good_snapshot(data_dir, "github"),
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


def _governance_reviewers_by_issue(data_dir: Path) -> dict[str, tuple[str, ...]]:
    """`issue_key -> reviewer names`, from the already-collected JIRA
    `review_event` raw table (`source == 'jira_field'`) — no extra network
    call: this is the same JIRA-reviewer-field evidence the M0 collectors
    already fetch (`collectors/jira.py`), just re-keyed by issue for
    `governance/checks.py`'s `reviewer-present` check.
    """
    table = storage.read_table(data_dir, "jira", "review_event")
    by_issue: dict[str, list[str]] = {}
    for row in table.to_pylist():
        if row["source"] != "jira_field" or not row["issue_key"]:
            continue
        by_issue.setdefault(row["issue_key"], []).append(row["reviewer_raw_value"])
    return {key: tuple(dict.fromkeys(names)) for key, names in by_issue.items()}


def _governance_budget(config: ProjectConfig) -> tuple[int, int]:
    """`(max_github_calls_per_run, max_jira_calls_per_run)` from
    `projects/<id>.yaml`'s optional `governance:` block (issue #36 fixup
    cycle 1), defaulting to `DEFAULT_GOVERNANCE_MAX_*_CALLS_PER_RUN` so a
    project without that block still gets a bounded, polite per-run budget
    rather than an unbounded backfill every night. `ProjectConfig` allows
    arbitrary extra top-level keys (`config.py`'s own `extra="allow"`
    docstring), so `governance:` never has to be added to that pydantic
    model — it's read here as a plain dict, defensively.
    """
    raw = getattr(config, "governance", None)
    if not isinstance(raw, dict):
        raw = {}
    max_github = int(
        raw.get("max_github_calls_per_run", DEFAULT_GOVERNANCE_MAX_GITHUB_CALLS_PER_RUN)
    )
    max_jira = int(raw.get("max_jira_calls_per_run", DEFAULT_GOVERNANCE_MAX_JIRA_CALLS_PER_RUN))
    return max_github, max_jira


def _governance_checkstyle_retention_days(config: ProjectConfig) -> int:
    """How far back (in days, from `started_at`) `code-style-checkstyle`
    will even attempt a GitHub check-runs fetch (issue #36 fixup cycle 2) —
    from `projects/<id>.yaml`'s `governance.checkstyle_retention_days`,
    defaulting to `DEFAULT_GOVERNANCE_CHECKSTYLE_RETENTION_DAYS`.

    docs/spec/GOVERNANCE.md §10's live finding: GitHub Actions check-run
    history itself rolls off well before this project's own scoring horizon
    (only 25.5% of a 21-month sample still had a retrievable run at all) —
    fetching for a commit older than this is not "politely incremental", it
    is guaranteed wasted budget, since GitHub will never have the answer.
    Those commits are scored `unknown` directly (`checks.py`'s
    `score_code_style_checkstyle` `retention_cutoff` parameter), never
    fetched, and never counted as "pending" backlog — see
    `_collect_governance_check_runs`'s `skipped_outside_retention` stat.
    """
    raw = getattr(config, "governance", None)
    if not isinstance(raw, dict):
        raw = {}
    return int(raw.get("checkstyle_retention_days", DEFAULT_GOVERNANCE_CHECKSTYLE_RETENTION_DAYS))


def _governance_commit_record_rows(
    records: list[CommitRecord], source_snapshot_id: str
) -> list[dict]:
    """`CommitRecord` -> `commit_record` row dicts, shared by the ordinary
    incremental walk and the issue #77 full-history reparse below, so both
    always stamp the same `parser_version` (`reviewer_trailer.PARSER_VERSION`
    -- the version `records`' `trailer_reviewers` were actually extracted
    with, since both callers build `records` via `collect_commits`, which
    uses the current `ReviewerExtractor`)."""
    return [
        {
            "sha": r.sha,
            "branch": r.branch,
            "commit_date": r.commit_date,
            "message": r.message,
            "author": r.author,
            "author_email": r.author_email,
            "committer": r.committer,
            "committer_email": r.committer_email,
            "is_merge": r.is_merge,
            "trailer_reviewers": list(r.trailer_reviewers),
            "issue_keys": list(r.issue_keys),
            "changed_paths": list(r.changed_paths) if r.changed_paths is not None else None,
            "source_snapshot_id": source_snapshot_id,
            "parser_version": PARSER_VERSION,
        }
        for r in records
    ]


def _collect_governance_commit_records(
    data_dir: Path, workdir: Path, repo_cfg: Any, run_id: str, governance_since: str | None
) -> int:
    """Incrementally walk new commits (issue #36 fixup cycle 1) and append
    them to `raw/governance/commit_record`, then advance the
    `governance_git` watermark (a separate watermark key from the M0
    `GitCollector`'s `git`, since this walk deliberately includes merges).

    The walk itself is unbounded and cheap (local `git log`, no external API
    involved) — `governance_since` only bounds the very first run, when no
    watermark exists yet, to keep an initial backfill's *scoring* input
    reasonably sized; once a watermark exists, every subsequent run only
    ever walks commits strictly newer than it, regardless of `governance_since`.

    Returns the number of new commit records collected this run.
    """
    watermark = storage.read_watermark(data_dir, "governance_git")
    records = collect_commits(
        workdir,
        repo_cfg.default_branch,
        branch_label=repo_cfg.default_branch,
        since=governance_since,
        since_sha=watermark,
    )
    rows = _governance_commit_record_rows(records, f"{run_id}:governance_git")
    schema = _governance_get_schema("commit_record")
    table = _governance_validate(
        "commit_record",
        pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table(),
    )
    storage.write_partition(
        data_dir, "governance", "commit_record", datetime.now(timezone.utc).date(), run_id, table
    )
    # Always re-resolve and persist the current HEAD sha, even when `records`
    # is empty (the ref may not have moved, or moved to a merge/no-op state)
    # -- matches `GitCollector`'s own "watermark = git rev-parse ref" pattern.
    new_watermark = resolve_sha(workdir, repo_cfg.default_branch)
    storage.write_watermark(data_dir, "governance_git", new_watermark)
    return len(rows)


def _reparse_governance_commit_records_if_needed(
    data_dir: Path,
    workdir: Path,
    repo_cfg: Any,
    run_id: str,
    governance_since: str | None,
    had_prior_history: bool,
) -> dict[str, Any]:
    """Governance's counterpart to `_reparse_commit_trailer_review_events_if_needed`
    (issue #77): a `PARSER_VERSION` bump also invalidates `trailer_reviewers`
    on every already-collected `raw/governance/commit_record` row, and that
    table's own `since_sha` watermark means the ordinary incremental walk in
    `_collect_governance_commit_records` will never revisit those commits
    either. One-time, full-history re-walk (`since_sha=None`, but the same
    `governance_since` bound the very first ever run used, so the reparsed
    set matches exactly what a fresh backfill would collect today) written
    as its own new partition; `_dedupe_governance_commit_records` then keeps
    only each `sha`'s highest-`parser_version` row at scoring read time.

    Tracked via its own watermark key (`table="commit_record_parser_version"`
    on the `governance_git` source), independent of the real `since_sha`
    watermark `_collect_governance_commit_records` advances.
    """
    marker = storage.read_watermark(
        data_dir, "governance_git", table="commit_record_parser_version"
    )
    if marker == str(PARSER_VERSION) or not had_prior_history:
        if marker != str(PARSER_VERSION):
            storage.write_watermark(
                data_dir,
                "governance_git",
                str(PARSER_VERSION),
                table="commit_record_parser_version",
            )
        return {"status": "skipped", "parser_version": PARSER_VERSION}

    _log(
        "governance_commit_record_reparse_started",
        from_marker=marker,
        to_parser_version=PARSER_VERSION,
    )
    try:
        records = collect_commits(
            workdir,
            repo_cfg.default_branch,
            branch_label=repo_cfg.default_branch,
            since=governance_since,
            since_sha=None,
        )
        rows = _governance_commit_record_rows(records, f"{run_id}:governance_git:reparse")
        schema = _governance_get_schema("commit_record")
        table = _governance_validate(
            "commit_record",
            pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table(),
        )
        storage.write_partition(
            data_dir,
            "governance",
            "commit_record",
            datetime.now(timezone.utc).date(),
            f"{run_id}-reparse",
            table,
        )
        storage.write_watermark(
            data_dir,
            "governance_git",
            str(PARSER_VERSION),
            table="commit_record_parser_version",
        )
        _log(
            "governance_commit_record_reparse_succeeded",
            commits_scanned=len(records),
        )
        return {"status": "ok", "parser_version": PARSER_VERSION, "commits_scanned": len(records)}
    except Exception as exc:  # noqa: BLE001 - never roll back this run's own successful collection
        _log("governance_commit_record_reparse_failed", error=str(exc))
        return {"status": "failed", "parser_version": PARSER_VERSION, "reason": str(exc)}


def _dedupe_governance_commit_records(table: pa.Table) -> pa.Table:
    """Keep only the highest-`parser_version` row per `sha` (issue #77).

    `commit_record`'s natural key is a plain `sha` column (unlike
    `review_event`, which has to parse it out of `event_id` — see
    `_dedupe_commit_trailer_review_events`), so this is a simpler version of
    the same read-time supersede mechanism the module docstring describes. A
    `None` `parser_version` (a row collected before this column existed) is
    treated as version 1.
    """
    if table.num_rows == 0:
        return table
    rows = table.to_pylist()
    best_version: dict[str, int] = {}
    for row in rows:
        version = row["parser_version"] if row["parser_version"] is not None else 1
        sha = row["sha"]
        if sha not in best_version or version > best_version[sha]:
            best_version[sha] = version
    kept = [
        row
        for row in rows
        if (row["parser_version"] if row["parser_version"] is not None else 1)
        == best_version[row["sha"]]
    ]
    kept.sort(key=lambda r: r["sha"])
    return pa.Table.from_pylist(kept, schema=table.schema)


def _read_all_governance_commits(data_dir: Path) -> list[CommitFacts]:
    """Every commit ever collected by `_collect_governance_commit_records`
    (D3: scoring always reads the *entire* accumulated raw cache, never just
    this run's delta), deduped to each commit's latest-parser-version row
    (issue #77, `_dedupe_governance_commit_records`)."""
    table = _dedupe_governance_commit_records(
        storage.read_table(data_dir, "governance", "commit_record")
    )
    commits = []
    for row in table.to_pylist():
        changed_paths = row["changed_paths"]
        commits.append(
            CommitFacts(
                sha=row["sha"],
                branch=row["branch"],
                commit_date=row["commit_date"],
                message=row["message"],
                author=row["author"],
                committer=row["committer"],
                is_merge=row["is_merge"],
                trailer_reviewers=tuple(row["trailer_reviewers"]),
                issue_keys=tuple(row["issue_keys"]),
                changed_paths=tuple(changed_paths) if changed_paths is not None else None,
            )
        )
    return commits


def _governance_issue_updated_map(data_dir: Path) -> dict[str, datetime]:
    """`issue_key -> updated_at`, from the already-collected M0 `raw/jira/issue`
    table — this *is* "reusing the JIRA collector's approach" to a watermark
    (issue #36 fixup cycle 1): an issue's own `updated` field is what decides
    whether its CI-evidence comments are worth re-checking."""
    table = storage.read_table(data_dir, "jira", "issue")
    return {row["issue_key"]: row["updated_at"] for row in table.to_pylist()}


def _governance_ci_evidence_checked_map(data_dir: Path) -> dict[str, datetime]:
    """`issue_key -> issue_updated_at` of the *latest* check recorded in the
    accumulated `raw/governance/ci_evidence` table (regardless of whether
    that check found anything) — this is what "have we already checked this
    issue since it last changed" compares against."""
    table = storage.read_table(data_dir, "governance", "ci_evidence")
    latest: dict[str, datetime] = {}
    latest_checked_at_by_key: dict[str, datetime] = {}
    for row in table.to_pylist():
        key = row["issue_key"]
        if key not in latest_checked_at_by_key or row["checked_at"] > latest_checked_at_by_key[key]:
            latest_checked_at_by_key[key] = row["checked_at"]
            latest[key] = row["issue_updated_at"]
    return latest


def _governance_ci_evidence_found_map(data_dir: Path) -> dict[str, CIEvidence]:
    """`issue_key -> CIEvidence`, one entry per issue that has ever had a
    `found=True` row in the accumulated `raw/governance/ci_evidence` table
    (the most recently checked positive match wins if more than one)."""
    table = storage.read_table(data_dir, "governance", "ci_evidence")
    best: dict[str, tuple[datetime, CIEvidence]] = {}
    for row in table.to_pylist():
        if not row["found"]:
            continue
        key = row["issue_key"]
        checked_at = row["checked_at"]
        if key not in best or checked_at > best[key][0]:
            best[key] = (
                checked_at,
                CIEvidence(
                    issue_key=key,
                    comment_id=row["comment_id"],
                    comment_author=row["comment_author"],
                    comment_created_at=row["comment_created_at"],
                    matched_term=row["matched_term"],
                    matched_url=row["matched_url"],
                ),
            )
    return {key: evidence for key, (_checked_at, evidence) in best.items()}


def _ci_eligible_issue_keys_newest_first(commits: list[CommitFacts], ci_rule) -> list[str]:
    """Issue keys worth fetching JIRA-comment CI evidence for at all (issue
    #36 fixup cycle 2), newest-referencing-commit-first.

    Only commits where `pre-commit-ci-evidence` could actually change the
    result are considered: a commit before the rule's own `effective_from`
    (or on a branch the rule doesn't apply to at all) always scores
    `not_in_force` regardless of any evidence, so fetching for its issue key
    would be pure wasted budget — unless that same key is *also* referenced
    by a genuinely in-force commit, which the union here still catches.
    Ordering newest-referencing-commit-first means a tight per-run budget
    resolves the commits people are actually looking at before working
    through the historical backlog.
    """
    ci_eligible_commits = [
        c
        for c in commits
        if ci_rule.applies_to_branch(c.branch) and ci_rule.in_force_on(c.commit_date.date())
    ]
    latest_commit_date_by_issue: dict[str, datetime] = {}
    for c in ci_eligible_commits:
        for key in c.issue_keys:
            current = latest_commit_date_by_issue.get(key)
            if current is None or c.commit_date > current:
                latest_commit_date_by_issue[key] = c.commit_date
    return sorted(
        latest_commit_date_by_issue, key=lambda k: latest_commit_date_by_issue[k], reverse=True
    )


def _collect_governance_ci_evidence(
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    issue_keys_newest_first: list[str],
    max_calls: int,
    jira_base_url: str | None,
    jira_comments_factory: Callable[[str], object] | None,
) -> dict[str, Any]:
    """Fetch JIRA-comment CI evidence for issues that are new or have
    changed since they were last checked (issue #36 fixup cycle 1),
    stopping cleanly once `max_calls` HTTP requests have been made this run.

    `issue_keys_newest_first` (fixup cycle 2) is already both (a) restricted
    to only the issue keys referenced by a commit where
    `pre-commit-ci-evidence` could actually be in force and scorable (the
    caller, `_collect_governance`, filters out commits before the rule's
    `effective_from` — those are `not_in_force` regardless of evidence, so
    fetching for them would only ever waste budget) and (b) ordered by each
    issue's most recent referencing commit, newest first, so a tight budget
    resolves the commits people are actually looking at before working
    through the historical backlog.

    Returns a stats dict: `checked`, `skipped_up_to_date`, `pending`
    (eligible but left unfetched because the budget ran out), `calls_made`.
    Never raises — a JIRA outage degrades every affected check to `unknown`
    (via an empty evidence map at scoring time), it never aborts the run.
    """
    stats = {"checked": 0, "skipped_up_to_date": 0, "pending": 0, "calls_made": 0}
    if not jira_base_url or not issue_keys_newest_first:
        return stats

    issue_updated = _governance_issue_updated_map(data_dir)
    already_checked = _governance_ci_evidence_checked_map(data_dir)

    eligible: list[str] = []
    for key in issue_keys_newest_first:
        last_checked = already_checked.get(key)
        if last_checked is None:
            eligible.append(key)
            continue
        current_updated = issue_updated.get(key)
        if current_updated is not None and current_updated > last_checked:
            eligible.append(key)
        else:
            stats["skipped_up_to_date"] += 1

    if not eligible:
        return stats

    try:
        collector = (jira_comments_factory or JiraCommentsCollector)(jira_base_url)
    except Exception as exc:  # noqa: BLE001 - an evidence source's outage must not abort the run.
        _log("governance_jira_comments_failed", error=str(exc))
        stats["pending"] = len(eligible)
        return stats

    rows: list[dict[str, Any]] = []
    try:
        for key in eligible:
            if collector.call_count >= max_calls:
                break
            try:
                found = collector.fetch_ci_evidence(key)
            except Exception as exc:  # noqa: BLE001 - one bad issue must not stop the batch.
                _log("governance_jira_comments_issue_failed", issue_key=key, error=str(exc))
                continue
            checked_at = datetime.now(timezone.utc)
            issue_updated_at = issue_updated.get(key, checked_at)
            if found is not None:
                rows.append(
                    {
                        "issue_key": key,
                        "issue_updated_at": issue_updated_at,
                        "checked_at": checked_at,
                        "found": True,
                        "comment_id": found.comment_id,
                        "comment_author": found.comment_author,
                        "comment_created_at": found.comment_created_at,
                        "matched_term": found.matched_term,
                        "matched_url": found.matched_url,
                        "source_snapshot_id": f"{run_id}:governance_ci",
                    }
                )
            else:
                rows.append(
                    {
                        "issue_key": key,
                        "issue_updated_at": issue_updated_at,
                        "checked_at": checked_at,
                        "found": False,
                        "comment_id": None,
                        "comment_author": None,
                        "comment_created_at": None,
                        "matched_term": None,
                        "matched_url": None,
                        "source_snapshot_id": f"{run_id}:governance_ci",
                    }
                )
            stats["checked"] += 1
    finally:
        stats["calls_made"] = collector.call_count
        collector.close()

    stats["pending"] = len(eligible) - stats["checked"]

    schema = _governance_get_schema("ci_evidence")
    table = _governance_validate(
        "ci_evidence",
        pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table(),
    )
    storage.write_partition(
        data_dir, "governance", "ci_evidence", started_at.date(), run_id, table
    )
    return stats


def _governance_check_run_latest_batch(data_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """`sha -> every row from its *latest* fetch batch` (all rows sharing
    that sha's single most recent `fetched_at`, e.g. both `ant-check-jdk11`
    and `ant-check-jdk17` from one fetch, or the one sentinel "no run found"
    row) — a superseded, older fetch is never mixed in. Shared grouping
    logic behind both `_governance_check_run_latest_is_resolved_map` (is a
    sha worth re-fetching) and `_governance_check_run_evidence_for_scoring`
    (what evidence to score it with).
    """
    table = storage.read_table(data_dir, "governance", "check_run")
    latest_fetched_at: dict[str, datetime] = {}
    rows_by_sha: dict[str, list[dict[str, Any]]] = {}
    for row in table.to_pylist():
        sha = row["sha"]
        fetched_at = row["fetched_at"]
        if sha not in latest_fetched_at or fetched_at > latest_fetched_at[sha]:
            latest_fetched_at[sha] = fetched_at
            rows_by_sha[sha] = [row]
        elif fetched_at == latest_fetched_at[sha]:
            rows_by_sha[sha].append(row)
    return rows_by_sha


def _governance_check_run_latest_is_resolved_map(data_dir: Path) -> dict[str, bool]:
    """`sha -> is the latest fetch batch's result already final`, from the
    accumulated `raw/governance/check_run` table. `True` when the latest
    batch contains at least one terminal conclusion (a failing conclusion is
    definitive on its own, matching `checks.score_code_style_checkstyle`'s
    "failing takes priority" rule; otherwise every row in the batch must be
    `success`). `False` covers both the sentinel "no run found" batch and a
    batch with a real but still-pending/neutral conclusion. A sha absent
    from this dict has never been checked at all.
    """
    resolved: dict[str, bool] = {}
    for sha, rows in _governance_check_run_latest_batch(data_dir).items():
        conclusions = [row["conclusion"] for row in rows]
        has_failure = any(c in _GOVERNANCE_FAILING_CONCLUSIONS for c in conclusions)
        all_success = bool(conclusions) and all(c == "success" for c in conclusions)
        resolved[sha] = has_failure or all_success
    return resolved


def _governance_check_run_evidence_for_scoring(
    data_dir: Path,
) -> dict[str, tuple[CheckstyleEvidence, ...]]:
    """`sha -> checkstyle CheckstyleEvidence tuple` for scoring, built from
    each sha's *latest* fetch batch only — sentinel "no run found" rows
    contribute an empty tuple, identical to `checks.py`'s "never checked"
    case."""
    result: dict[str, tuple[CheckstyleEvidence, ...]] = {}
    for sha, rows in _governance_check_run_latest_batch(data_dir).items():
        evidence = tuple(
            CheckstyleEvidence(
                sha=sha,
                check_run_name=row["check_run_name"],
                conclusion=row["conclusion"],
                html_url=row["html_url"],
            )
            for row in rows
            if row["check_run_name"] != _GOVERNANCE_NO_RUN_FOUND_SENTINEL
        )
        if evidence:
            result[sha] = evidence
    return result


def _collect_governance_check_runs(
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    eligible_commits: list[CommitFacts],
    max_calls: int,
    owner: str,
    repo: str,
    github_checks_factory: Callable[[str, str], object] | None,
    *,
    retention_cutoff: datetime,
) -> dict[str, Any]:
    """Fetch GitHub checkstyle check-runs for shas that have never been
    checked, or whose last known result is still pending/absent and whose
    commit is under `GOVERNANCE_CHECK_RUN_RETRY_WINDOW_DAYS` old (issue #36
    fixup cycle 1) — stopping cleanly once `max_calls` HTTP requests have
    been made this run. Never raises.

    `retention_cutoff` (fixup cycle 2) is a hard floor: a commit older than
    this is never fetched at all, not even once — see
    `checks.score_code_style_checkstyle`'s `retention_cutoff` parameter,
    which independently recognizes this same cutoff at scoring time and
    reports these commits `unknown` with a distinct "outside GitHub
    retention" evidence string, so they're never confused with a commit
    that's merely still pending. `eligible_commits` here should already be
    everything in policy scope for this rule (`applies_to_branch`); this
    function does the (cheaper, purely local) retention/resolved/recency
    filtering to decide which of those are actually worth an HTTP call this
    run.
    """
    stats = {
        "checked": 0,
        "skipped_resolved": 0,
        "skipped_too_old": 0,
        "skipped_outside_retention": 0,
        "pending": 0,
        "calls_made": 0,
    }
    if not eligible_commits:
        return stats

    is_resolved = _governance_check_run_latest_is_resolved_map(data_dir)
    now = started_at
    cutoff = now - timedelta(days=GOVERNANCE_CHECK_RUN_RETRY_WINDOW_DAYS)

    # Newest commits first: when the budget is tight, the most recently
    # merged (and most likely to actually have a fresh check-run) commits
    # get priority over an old, probably-permanently-`unknown` backlog.
    eligible_commits = sorted(eligible_commits, key=lambda c: c.commit_date, reverse=True)

    to_fetch: list[CommitFacts] = []
    for commit in eligible_commits:
        if commit.commit_date < retention_cutoff:
            # Never fetched, ever -- GitHub's own check-run history has
            # almost certainly already rolled off (docs/spec/GOVERNANCE.md
            # §10); scoring independently reports this as `unknown` with
            # "check-run history outside GitHub retention", not as backlog.
            stats["skipped_outside_retention"] += 1
        elif commit.sha not in is_resolved:
            to_fetch.append(commit)  # never checked
        elif is_resolved[commit.sha]:
            stats["skipped_resolved"] += 1
        elif commit.commit_date >= cutoff:
            to_fetch.append(commit)
        else:
            stats["skipped_too_old"] += 1

    if not to_fetch:
        return stats

    try:
        collector = (github_checks_factory or GitHubChecksCollector)(owner, repo)
    except Exception as exc:  # noqa: BLE001 - an evidence source's outage must not abort the run.
        _log("governance_github_checks_failed", error=str(exc))
        stats["pending"] = len(to_fetch)
        return stats

    rows: list[dict[str, Any]] = []
    try:
        for commit in to_fetch:
            if collector.call_count >= max_calls:
                break
            try:
                runs = collector.fetch_checkstyle_evidence(commit.sha)
            except Exception as exc:  # noqa: BLE001 - one bad sha must not stop the batch.
                _log("governance_github_checks_sha_failed", sha=commit.sha, error=str(exc))
                continue
            fetched_at = datetime.now(timezone.utc)
            if runs:
                for run in runs:
                    rows.append(
                        {
                            "sha": commit.sha,
                            "commit_date": commit.commit_date,
                            "check_run_name": run.check_run_name,
                            "conclusion": run.conclusion,
                            "html_url": run.html_url,
                            "fetched_at": fetched_at,
                            "source_snapshot_id": f"{run_id}:governance_gh",
                        }
                    )
            else:
                rows.append(
                    {
                        "sha": commit.sha,
                        "commit_date": commit.commit_date,
                        "check_run_name": _GOVERNANCE_NO_RUN_FOUND_SENTINEL,
                        "conclusion": None,
                        "html_url": None,
                        "fetched_at": fetched_at,
                        "source_snapshot_id": f"{run_id}:governance_gh",
                    }
                )
            stats["checked"] += 1
    finally:
        stats["calls_made"] = collector.call_count
        collector.close()

    stats["pending"] = len(to_fetch) - stats["checked"]

    schema = _governance_get_schema("check_run")
    table = _governance_validate(
        "check_run",
        pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table(),
    )
    storage.write_partition(data_dir, "governance", "check_run", started_at.date(), run_id, table)
    return stats


def _collect_governance(
    config: ProjectConfig,
    data_dir: Path,
    workdir: Path,
    run_id: str,
    started_at: datetime,
    *,
    governance_since: str | None,
    policy_path: str | Path,
    overrides_path: str | Path,
    github_checks_factory: Callable[[str, str], object] | None,
    jira_comments_factory: Callable[[str], object] | None,
) -> dict[str, Any]:
    """Governance compliance engine (issue #36, fixup cycle 1): incrementally
    collect evidence, then score the *entire* accumulated commit set against
    `governance-policy.yaml` every run.

    Three independent collection steps, each incremental against its own
    source's actual constraints (never a source-wide `governance_since`
    re-fetch every night):

    1. `_collect_governance_commit_records` — a cheap, local, unbounded git
       walk, incremental via a commit-SHA watermark
       (`collectors/git.py`-style).
    2. `_collect_governance_ci_evidence` — JIRA-comment CI evidence,
       incremental via each issue's own `updated` field (reusing
       `collectors/jira.py`'s watermark idea), budget-limited per run.
    3. `_collect_governance_check_runs` — GitHub checkstyle check-runs,
       incremental via "already resolved, or too old to bother retrying",
       budget-limited per run.

    Scoring (`governance/engine.py`) then always reads the *entire*
    accumulated output of all three (D3 — "never incremental"), exactly like
    `metrics.compute_all` does for the M0 metrics; only *collection* is
    incremental here, for the same reason `collectors/git.py`/`jira.py`'s
    collection is incremental while `metrics/engine.py`'s computation isn't.

    `manifest["governance"]["status"]` is `'ok'` only when every eligible
    JIRA issue and GitHub sha was actually checked this run; if either
    budget ran out first, it's `'partial'` — the remaining backlog is picked
    up by a later run (the same "first backfill may take several nightly
    runs" contract `collectors/github.py`'s PR collector documents for its
    own GraphQL rate-limit budget). A failure here is caught and reported as
    `'failed'` (mirrors `_collect_git`/`_collect_jira`'s "a source outage
    must never abort the run" contract, §7.3) — the M0 pipeline (metrics,
    site) is unaffected either way.

    Governance metrics are deliberately **not** registered in
    `metrics.registry.METRIC_IDS` (see `governance/metrics.py`'s module
    docstring): they're scored/aggregated and written to their own
    `snapshots/<run_id>/governance_*.parquet` files, entirely outside
    `compute_all`/`_write_metrics_snapshot` — so a governance evidence gap,
    or a `'partial'` backlog run, can never trip issue #24's "a *registered*
    metric produced zero rows" `metrics_missing` check and mark the M0
    pipeline `degraded`. This is a deliberate scope boundary, not an
    oversight: governance's own completeness is reported entirely through
    `manifest["governance"]`.
    """
    if not config.repos:
        return {"status": "skipped", "reason": "no repos configured"}

    repo_cfg = config.repos[0]
    max_github_calls, max_jira_calls = _governance_budget(config)
    checkstyle_retention_days = _governance_checkstyle_retention_days(config)
    checkstyle_retention_cutoff = started_at - timedelta(days=checkstyle_retention_days)
    try:
        policy = load_policy(policy_path)
        overrides = load_governance_overrides(overrides_path)

        governance_git_watermark_before = storage.read_watermark(data_dir, "governance_git")
        git_records_collected = _collect_governance_commit_records(
            data_dir, workdir, repo_cfg, run_id, governance_since
        )
        # issue #77: a `PARSER_VERSION` bump triggers a one-time, full-history
        # re-derivation of `commit_record.trailer_reviewers` -- see
        # `_reparse_governance_commit_records_if_needed`'s docstring. Runs
        # after the ordinary incremental walk above so it never blocks it.
        commit_record_reparse = _reparse_governance_commit_records_if_needed(
            data_dir,
            workdir,
            repo_cfg,
            run_id,
            governance_since,
            had_prior_history=governance_git_watermark_before is not None,
        )
        commits = _read_all_governance_commits(data_dir)

        jira_reviewers_by_issue = _governance_reviewers_by_issue(data_dir)

        ci_rule = policy.rule("pre-commit-ci-evidence")
        issue_keys_newest_first = _ci_eligible_issue_keys_newest_first(commits, ci_rule)

        base_url = getattr(config.issue_tracker, "base_url", None) if config.issue_tracker else None
        ci_stats = _collect_governance_ci_evidence(
            data_dir,
            run_id,
            started_at,
            issue_keys_newest_first,
            max_jira_calls,
            base_url,
            jira_comments_factory,
        )
        ci_evidence_by_issue = _governance_ci_evidence_found_map(data_dir)

        checkstyle_rule = policy.rule("code-style-checkstyle")
        checkstyle_eligible_commits = [
            c for c in commits if not c.is_merge and checkstyle_rule.applies_to_branch(c.branch)
        ]
        check_run_stats = _collect_governance_check_runs(
            data_dir,
            run_id,
            started_at,
            checkstyle_eligible_commits,
            max_github_calls,
            repo_cfg.owner,
            repo_cfg.name,
            github_checks_factory,
            retention_cutoff=checkstyle_retention_cutoff,
        )
        checkstyle_runs_by_sha = _governance_check_run_evidence_for_scoring(data_dir)

        compliance_rows = build_commit_compliance_rows(
            policy,
            commits,
            jira_reviewers_by_issue=jira_reviewers_by_issue,
            ci_evidence_by_issue=ci_evidence_by_issue,
            checkstyle_runs_by_sha=checkstyle_runs_by_sha,
            checkstyle_retention_cutoff=checkstyle_retention_cutoff,
            overrides=overrides,
        )
        fact_rows = build_commit_facts_rows(commits)

        compliance_schema = _governance_get_schema("commit_compliance")
        compliance_table = _governance_validate(
            "commit_compliance",
            pa.Table.from_pylist(compliance_rows, schema=compliance_schema)
            if compliance_rows
            else compliance_schema.empty_table(),
        )
        fact_schema = _governance_get_schema("commit_fact")
        fact_table = _governance_validate(
            "commit_fact",
            pa.Table.from_pylist(fact_rows, schema=fact_schema)
            if fact_rows
            else fact_schema.empty_table(),
        )

        governance_metrics = compute_monthly_check_metrics(
            compliance_table, as_of=started_at.date(), run_id=run_id, computed_at=started_at
        )
        governance_registry = build_governance_registry(started_at)

        snapshot_dir = Path(data_dir) / "snapshots" / run_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(compliance_table, snapshot_dir / "governance_commit_compliance.parquet")
        pq.write_table(fact_table, snapshot_dir / "governance_commit_fact.parquet")
        pq.write_table(governance_metrics, snapshot_dir / "governance_metric_value.parquet")
        pq.write_table(
            governance_registry, snapshot_dir / "governance_metric_definition_version.parquet"
        )

        status = "ok" if ci_stats["pending"] == 0 and check_run_stats["pending"] == 0 else "partial"

        _log(
            "governance_scored",
            run_id=run_id,
            status=status,
            commits_scored=len(commits),
            compliance_rows=compliance_table.num_rows,
            policy_version=policy.version,
            ci_evidence=ci_stats,
            check_runs=check_run_stats,
        )
        return {
            "status": status,
            "commits_scored": len(commits),
            "compliance_rows": compliance_table.num_rows,
            "policy_version": policy.version,
            "git_records_collected": git_records_collected,
            "commit_record_reparse": commit_record_reparse,
            "ci_evidence": ci_stats,
            "check_runs": check_run_stats,
        }
    except Exception as exc:  # noqa: BLE001 - governance must never abort the run (§7.3-style)
        _log("governance_failed", error=str(exc))
        return {"status": "failed", "reason": str(exc)}


def _collect_security(
    config: ProjectConfig,
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    collector_factory: Callable[[ProjectConfig], SecurityCollector] | None,
) -> dict[str, Any]:
    """Collect OpenSSF Scorecard + NVD advisory data (issue #55, D21 item 3).

    Both `scorecard_check` and `security_advisory` are written as one raw
    partition per run, same append-only pattern as every other source; the
    Governance page's Security section (`site/generate.py`) reads the full
    accumulated history directly, independent of the metrics engine (this
    source registers no `metric_value` rows -- its per-check/per-CVE facts
    don't fit the monthly-windowed-rate shape the metrics registry assumes,
    and the issue itself permits skipping metric registration entirely
    rather than forcing a bad fit).
    """
    _log("source_collect_started", source="security")
    collector = (collector_factory or SecurityCollector)(config)
    try:
        result = collector.collect()
        partition_date = started_at.date()
        storage.write_partition(
            data_dir, "security", "scorecard_check", partition_date, run_id, result.scorecard_checks
        )
        storage.write_partition(
            data_dir, "security", "security_advisory", partition_date, run_id, result.advisories
        )
        record_last_good_snapshot(data_dir, "security", run_id)
        _log(
            "source_collect_succeeded",
            source="security",
            scorecard_checks_collected=result.scorecard_check_count,
            advisories_collected=result.advisory_count,
        )
        return {
            "status": "ok",
            "records_collected": result.scorecard_check_count + result.advisory_count,
            "scorecard_checks_collected": result.scorecard_check_count,
            "advisories_collected": result.advisory_count,
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="security", error=str(exc))
        return {
            "status": "failed",
            "records_collected": 0,
            "last_good_snapshot": read_last_good_snapshot(data_dir, "security"),
            "reason": str(exc),
        }
    finally:
        collector.close()


def _collect_github_commit_authors(
    config: ProjectConfig,
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    max_pages: int | None,
    collector_factory: Callable[[ProjectConfig], GitHubCommitAuthorCollector] | None,
) -> dict[str, Any]:
    """Walk `config.repos[0]`'s commit history for GitHub-asserted
    author-email -> login associations (D6, issue #52 fixup cycle 1) --
    see `collectors/github_commit_authors.py`. Own per-table watermark
    (`table="github_commit_author"`, issue #53's "backfill gap" pattern),
    since this table is added long after `git`'s own watermark exists.
    """
    watermark = storage.read_watermark(
        data_dir, "github_commit_authors", table="github_commit_author"
    )
    snapshot_id = f"{run_id}:github_commit_authors"

    _log("source_collect_started", source="github_commit_authors", watermark=watermark)
    collector = (collector_factory or GitHubCommitAuthorCollector)(config)
    try:
        result = collector.collect(
            watermark=watermark, snapshot_id=snapshot_id, max_pages=max_pages
        )
        partition_date = started_at.date()
        storage.write_partition(
            data_dir,
            "github_commit_authors",
            "github_commit_author",
            partition_date,
            run_id,
            result.associations,
        )
        if result.outcome.next_watermark:
            storage.write_watermark(
                data_dir,
                "github_commit_authors",
                result.outcome.next_watermark,
                table="github_commit_author",
            )
        _log(
            "source_collect_succeeded",
            source="github_commit_authors",
            status=result.outcome.status,
            commits_seen=result.outcome.commits_seen,
            associations_found=result.outcome.associations_found,
            pages_fetched=result.outcome.pages_fetched,
            next_watermark=result.outcome.next_watermark,
        )
        return {
            "status": result.outcome.status,
            "watermark": result.outcome.next_watermark,
            "records_collected": result.outcome.associations_found,
            "commits_seen": result.outcome.commits_seen,
            "reason": result.outcome.error,
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="github_commit_authors", error=str(exc))
        return {
            "status": "failed",
            "watermark": watermark,
            "records_collected": 0,
            "last_good_snapshot": read_last_good_snapshot(data_dir, "github_commit_authors"),
            "reason": str(exc),
        }
    finally:
        collector.close()


def _github_logins_seen(data_dir: Path) -> set[str]:
    """Every distinct `github_login` this run's accumulated raw data has
    observed (issue #52 fixup cycle 1): the `github_commit_author` table
    (GitHub's own commit-author association, `collectors/
    github_commit_authors.py`) is the real source of these today. Also
    scans `contribution_event`/`review_event` for a `github_login`-typed
    raw identifier directly, forward-compatible with a future collector
    (e.g. issue #54's PR-collector wiring) that populates one there without
    this function needing to change.
    """
    logins: set[str] = set()
    for row in storage.read_table(
        data_dir, "github_commit_authors", "github_commit_author"
    ).to_pylist():
        logins.add(row["login"])
    contribution_event = storage.read_table(data_dir, "git", "contribution_event")
    for row in contribution_event.to_pylist():
        if row["author_raw_type"] == "github_login":
            logins.add(row["author_raw_value"])
    for source in ("git", "jira"):
        review_event = storage.read_table(data_dir, source, "review_event")
        for row in review_event.to_pylist():
            if row["reviewer_raw_type"] == "github_login":
                logins.add(row["reviewer_raw_value"])
            if row["author_raw_type"] == "github_login":
                logins.add(row["author_raw_value"])
    return logins


def _collect_github_profile(
    data_dir: Path,
    run_id: str,
    started_at: datetime,
    max_profiles: int | None,
    collector_factory: Callable[[], GitHubProfileCollector] | None,
) -> dict[str, Any]:
    """Fetch the public `company` field (D6) for `github_login`s seen via
    `collectors/github_commit_authors.py`, skipping any login already
    cached in the accumulated `github_profile` raw table -- "fetched once
    per login" (issue #52). Budgeted via `max_profiles` (default
    `DEFAULT_MAX_GITHUB_PROFILES_PER_RUN`, this project's API-budget rule).

    Status is `'partial'` when the budget or a GitHub rate limit stopped
    collection before every discovered login was fetched this run --
    expected and self-healing (the un-fetched logins are picked up by a
    later run's "already cached" skip), not a source failure. `'ok'` only
    when every discovered-but-uncached login was actually fetched.
    """
    _log("source_collect_started", source="github_profile")
    already_cached = {
        row["login"]
        for row in storage.read_table(data_dir, "github_profile", "github_profile").to_pylist()
    }
    new_logins = sorted(_github_logins_seen(data_dir) - already_cached)
    snapshot_id = f"{run_id}:github_profile"
    budget = max_profiles if max_profiles is not None else DEFAULT_MAX_GITHUB_PROFILES_PER_RUN

    collector = (
        collector_factory or (lambda: GitHubProfileCollector(token=resolve_github_token()))
    )()
    try:
        result = collector.collect(new_logins, snapshot_id=snapshot_id, max_profiles=budget)
        partition_date = started_at.date()
        storage.write_partition(
            data_dir, "github_profile", "github_profile", partition_date, run_id, result.profiles
        )
        status = (
            "rate_limited"
            if result.rate_limited
            else ("partial" if result.profiles_collected < len(new_logins) else "ok")
        )
        _log(
            "source_collect_succeeded",
            source="github_profile",
            status=status,
            records_collected=result.profiles_collected,
            rate_limited=result.rate_limited,
            new_logins_seen=len(new_logins),
            budget=budget,
        )
        return {
            "status": status,
            "records_collected": result.profiles_collected,
            "new_logins_seen": len(new_logins),
        }
    except Exception as exc:  # noqa: BLE001 - a source outage must never abort the run (§7.3)
        _log("source_collect_failed", source="github_profile", error=str(exc))
        return {
            "status": "failed",
            "records_collected": 0,
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


def _write_leaderboard_snapshot(data_dir: Path, run_id: str, leaderboard_table: pa.Table) -> Path:
    """`snapshots/<run_id>/leaderboard.parquet` (D19, issue #56) — a sibling
    file to `metrics.parquet`, deliberately not merged into it: the
    `contributor_leaderboard` table has its own schema (`schema/tables.py`)
    and is read by `site/leaderboard_page.py`, never by `metrics.registry`."""
    snapshot_dir = Path(data_dir) / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / "leaderboard.parquet"
    pq.write_table(leaderboard_table, path)
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
    max_github_profiles: int | None = None,
    max_github_commit_author_pages: int | None = None,
    # issue #54: caps PR nodes fetched per repo this run (testing / smoke
    # runs), the same role max_jira_issues/max_ponymail_months play for their
    # own sources. Production callers leave this None -- GitHubCollector's
    # own rate_limit_floor is the real per-run budget (module docstring).
    max_prs_per_repo: int | None = None,
    now: datetime | None = None,
    code_sha: str | None = None,
    identity_overrides_path: str | Path | None = None,
    trigger: str = "manual",
    jira_collector_factory: Callable[[ProjectConfig], JiraCollector] | None = None,
    # issue #79: injects an offline-testable stand-in for `collectors.
    # jira_comments.JiraCommentsCollector` into the historical `issue_comment`
    # backfill (`_collect_jira_comment_backfill`) -- same `Callable[[str],
    # object]` shape as `governance_jira_comments_factory` below, since both
    # construct the same collector class from just a base URL.
    jira_comment_backfill_factory: Callable[[str], object] | None = None,
    github_collector_factory: Callable[[ProjectConfig], GitHubCollector] | None = None,
    asf_roster_collector_factory: Callable[[ProjectConfig], AsfRosterCollector] | None = None,
    ponymail_collector_factory: Callable[[ProjectConfig], PonyMailCollector] | None = None,
    # --- Governance compliance engine (issue #36) ---------------------------
    # Controlled the same way as `git`/`jira`/`asf_roster`: via `sources`
    # (default: all of `ALL_SOURCES`, so a plain `run_pipeline(...)` call
    # runs governance too). Pass `sources=["git", "jira"]` to opt out.
    governance_since: str | None = None,
    governance_policy_path: str | Path = DEFAULT_POLICY_PATH,
    governance_overrides_path: str | Path = DEFAULT_OVERRIDES_PATH,
    governance_jira_comments_factory: Callable[[str], object] | None = None,
    governance_github_checks_factory: Callable[[str, str], object] | None = None,
    security_collector_factory: Callable[[ProjectConfig], SecurityCollector] | None = None,
    github_commit_author_collector_factory: (
        Callable[[ProjectConfig], GitHubCommitAuthorCollector] | None
    ) = None,
    github_profile_collector_factory: Callable[[], GitHubProfileCollector] | None = None,
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

    `jira_comment_backfill_factory` (issue #79), given, replaces the default
    `JiraCommentsCollector(base_url)` construction the historical
    `issue_comment` backfill uses -- same offline-test injection pattern as
    `governance_jira_comments_factory` below, since both construct that same
    collector class.

    `asf_roster_collector_factory`, given, replaces the default
    `AsfRosterCollector(config)` construction — this is how tests inject an
    `AsfRosterCollector` wired to an offline `httpx.MockTransport`.

    `security_collector_factory`, given, replaces the default
    `SecurityCollector(config)` construction (issue #55) — same offline-test
    injection pattern as the two factories above.

    `github_commit_author_collector_factory`, given, replaces the default
    `GitHubCommitAuthorCollector(config)` construction (issue #52 fixup
    cycle 1) — this is how tests inject one wired to an offline
    `httpx.MockTransport`.

    `github_profile_collector_factory`, given, replaces the default
    `GitHubProfileCollector(token=...)` construction (issue #52) — this is
    how tests inject one wired to an offline `httpx.MockTransport`.
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
            config,
            data_dir,
            run_id,
            started_at,
            max_jira_issues,
            jira_collector_factory,
            jira_comment_backfill_factory,
        )
    if "github" in active_sources:
        source_results["github"] = _collect_github(
            config, data_dir, run_id, started_at, max_prs_per_repo, github_collector_factory
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
    if "security" in active_sources:
        source_results["security"] = _collect_security(
            config, data_dir, run_id, started_at, security_collector_factory
        )
    if "github_commit_authors" in active_sources:
        source_results["github_commit_authors"] = _collect_github_commit_authors(
            config,
            data_dir,
            run_id,
            started_at,
            max_github_commit_author_pages,
            github_commit_author_collector_factory,
        )
    if "github_profile" in active_sources:
        # Runs after github_commit_authors above so a login first observed
        # by *this* run's own commit-author walk is eligible for the same
        # run's profile fetch, not just a login seen on some prior run.
        source_results["github_profile"] = _collect_github_profile(
            data_dir, run_id, started_at, max_github_profiles, github_profile_collector_factory
        )

    # D3: identity resolution and metrics always recompute from the ENTIRE
    # accumulated raw cache, regardless of which sources were active (or
    # failed) this run — a failed source's previous raw data is still used
    # (§7.3), and a source that wasn't asked to run this time still
    # contributes its prior history.
    contribution_event = storage.read_table(data_dir, "git", "contribution_event")
    file_change_event = storage.read_table(data_dir, "git", "file_change_event")
    git_review_event = _dedupe_commit_trailer_review_events(
        storage.read_table(data_dir, "git", "review_event")
    )
    jira_review_event = _dedupe_jira_review_events(
        storage.read_table(data_dir, "jira", "review_event")
    )
    review_event = pa.concat_tables([git_review_event, jira_review_event])
    issue = _dedupe_issue_rows(storage.read_table(data_dir, "jira", "issue"))
    issue_comment = _dedupe_issue_comment_rows(
        storage.read_table(data_dir, "jira", "issue_comment")
    )
    # issue #79: which issues have ever had their comments checked (via the
    # ordinary incremental fetch or the historical backfill) -- read raw,
    # undeduped, since `time_to_first_response_jira`'s per-month coverage
    # check only ever needs `EXISTS(issue_key)`, which a duplicate row across
    # partitions can't change.
    comment_backfill_checked = storage.read_table(data_dir, "jira", "comment_backfill_checked")
    roster_raw = storage.read_table(data_dir, "asf_roster", "roster_entry")
    roster_entry = _dedupe_roster_entries(roster_raw)
    # issue #35: dev@/user@ message metadata (D3 -- always recomputed from
    # the entire accumulated raw cache, regardless of whether "ponymail" was
    # an active source this run).
    message = _dedupe_message_rows(storage.read_table(data_dir, "ponymail", "message"))
    ponymail_raw_watermark = storage.read_watermark(data_dir, "ponymail")
    ponymail_watermarks: dict[str, str | None] = (
        json.loads(ponymail_raw_watermark) if ponymail_raw_watermark else {}
    )
    # issue #54: GitHub PR/review data for metrics/dev_metrics.py's six
    # metrics. `pr_comment` is collected (governance/site provenance) but not
    # read here -- none of the issue #54 metrics need PR comment bodies or
    # metadata, only pr/pr_review.
    pr = _dedupe_pr_rows(storage.read_table(data_dir, "github", "pr"))
    pr_review = _dedupe_pr_review_rows(storage.read_table(data_dir, "github", "pr_review"))

    overrides = []
    if identity_overrides_path is not None and Path(identity_overrides_path).is_file():
        overrides = load_overrides(identity_overrides_path)

    raw_identifiers = extract_raw_identifiers(
        contribution_events=contribution_event,
        review_events=review_event,
        issues=issue,
    )
    resolution = resolve_identities(raw_identifiers, overrides, now=started_at)

    # D6 (issue #52 fixup cycle 1): fold in GitHub's own commit-author
    # association as automated, high-confidence identity_link rows before
    # anything downstream reads identity_link -- this is what lets a
    # gmail.com/apache.org/personal-domain commit email resolve to an
    # organization via that person's GitHub profile `company` field, not
    # just via `org_domains.yaml`'s much narrower domain coverage.
    github_commit_author_raw = storage.read_table(
        data_dir, "github_commit_authors", "github_commit_author"
    )
    commit_author_associations = [
        (row["email"], row["login"], row["sha"])
        for row in github_commit_author_raw.to_pylist()
    ]
    identity_link = link_github_commit_authors(
        resolution.identity_link, commit_author_associations, now=started_at
    )

    # `affiliation_period` is derived, deterministic data -- like
    # `identity_link` above, it's recomputed fresh every run from the
    # curated file + reviewed domain map + reviewed alias map + the
    # accumulated `github_profile` cache, never persisted to the data
    # branch itself (D3).
    curated_affiliations = (
        load_affiliations_file(config.affiliations_file) if config.affiliations_file else {}
    )
    org_domains = load_org_domains(config.org_domains_file) if config.org_domains_file else {}
    org_aliases = load_org_aliases(config.org_aliases_file) if config.org_aliases_file else {}
    github_profile_raw = storage.read_table(data_dir, "github_profile", "github_profile")
    github_company_lookback_months = (
        config.github_company_lookback_months
        if config.github_company_lookback_months is not None
        else DEFAULT_GITHUB_COMPANY_LOOKBACK_MONTHS
    )
    affiliation_period = build_affiliation_periods(
        identity_link=identity_link,
        curated=curated_affiliations,
        org_domains=org_domains,
        org_aliases=org_aliases,
        github_profile=github_profile_raw,
        github_company_lookback_months=github_company_lookback_months,
    )

    metrics_table: pa.Table | None = None
    metrics_error: str | None = None
    try:
        metrics_table = compute_all(
            {
                "contribution_event": contribution_event,
                "file_change_event": file_change_event,
                "review_event": review_event,
                "issue": issue,
                "identity_link": identity_link,
                "roster_entry": roster_entry,
                "affiliation_period": affiliation_period,
                "message": message,
                "pr": pr,
                "pr_review": pr_review,
                "issue_comment": issue_comment,
                "comment_backfill_checked": comment_backfill_checked,
            },
            as_of=started_at.date(),
            run_id=run_id,
            computed_at=started_at,
            config=config,
            ponymail_watermarks=ponymail_watermarks,
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

    # --- Governance compliance engine (issue #36) ---------------------------
    # A separate, independent step: never gates the M0 run's exit code or
    # `status` (mirrors a source-collection failure, §7.3 — see
    # `_collect_governance`'s own docstring). Recorded on the manifest as an
    # extra top-level key; `site.manifest.RunManifest` ignores unknown keys
    # (provenance/manifest.py's own docstring), so this is safe to add
    # without touching that loader.
    if "governance" in active_sources:
        manifest["governance"] = _collect_governance(
            config,
            data_dir,
            workdir,
            run_id,
            started_at,
            governance_since=governance_since,
            policy_path=governance_policy_path,
            overrides_path=governance_overrides_path,
            github_checks_factory=governance_github_checks_factory,
            jira_comments_factory=governance_jira_comments_factory,
        )

    # --- Contributor leaderboard (D19, issue #56) ----------------------------
    # A ranked top-N *table*, not a registered metric (leaderboard.py's own
    # module docstring explains why it's deliberately kept out of
    # metrics.registry.METRIC_IDS) -- so, like governance above, it never
    # gates the M0 run's exit code or `status`: a run whose leaderboard
    # computation fails, or that has no qualifying activity yet, still
    # reports `status: ok`/`degraded` purely on the M0 metrics above. This is
    # a deliberate choice (mirrors governance's own "never gates" contract):
    # the leaderboard is an optional, additive presentation of data the M0
    # metrics already require to be present for their own status, so a gap
    # here would only ever be a duplicate signal of a gap already visible
    # elsewhere (e.g. `metrics_missing` for a genuinely broken collector),
    # never new information worth failing the run over.
    if metrics_table is not None:
        try:
            leaderboard_result = build_leaderboards(
                {
                    "contribution_event": contribution_event,
                    "review_event": review_event,
                    "issue": issue,
                    "identity_link": identity_link,
                    "person_identity": resolution.person_identity,
                    "affiliation_period": affiliation_period,
                },
                as_of=started_at.date(),
                run_id=run_id,
                computed_at=started_at,
                config=config,
            )
            _write_leaderboard_snapshot(data_dir, run_id, leaderboard_result.table)
            manifest["leaderboard"] = {
                "status": "ok",
                "rows": leaderboard_result.table.num_rows,
                "activity_types": sorted(leaderboard_result.lists),
            }
            _log(
                "leaderboard_computed",
                run_id=run_id,
                rows=leaderboard_result.table.num_rows,
            )
        except Exception as exc:  # noqa: BLE001 - never gate the M0 run on this (see above)
            _log("leaderboard_failed", run_id=run_id, error=str(exc))
            manifest["leaderboard"] = {"status": "failed", "reason": str(exc)}

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
