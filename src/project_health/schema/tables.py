"""PyArrow schemas for the M0 normalized tables (ARCHITECTURE.md §3).

Identifiers (`*_id`, `*_key`) are modeled as ``pa.string()`` (e.g. UUIDs as
their canonical string form) rather than a UUID extension type, for simple,
portable Parquet interop. All timestamps are UTC-aware
(``pa.timestamp("us", tz="UTC")``); the `metric_value` window bounds are
plain dates (``pa.date32()``) per the issue's output contract.

Every fact table (`contribution_event`, `file_change_event`, `review_event`,
`issue`) carries a `source_snapshot_id` column (ARCHITECTURE.md §3), tracing
a row back to the collector run that produced it. Identity tables and
run/metric-registry tables do not.

Collectors (#4, #5) run before identity resolution (#6), so fact-table rows
they write can't know a resolved `*_identity_id` yet. Every fact table
therefore also carries its raw identifier(s) (e.g. `author_raw_type` /
`author_raw_value`) alongside the nullable `*_identity_id` column: collectors
always populate the raw columns and leave `*_identity_id` null, and identity
resolution (or a later re-run of it, since raw data is immutable) fills
`*_identity_id` in by joining those raw identifiers against `identity_link`.
See `schema/README.md` for the full identity-resolution join story.
"""

from __future__ import annotations

import pyarrow as pa

TIMESTAMP_UTC = pa.timestamp("us", tz="UTC")

# --- Identity model (ARCHITECTURE.md §3, column detail block) ---------------

PERSON_IDENTITY = pa.schema(
    [
        pa.field("identity_id", pa.string(), nullable=False),
        pa.field("display_name", pa.string(), nullable=True),
        # status: 'resolved' | 'provisional'
        pa.field("status", pa.string(), nullable=False),
        pa.field("created_at", TIMESTAMP_UTC, nullable=False),
    ]
)

IDENTITY_LINK = pa.schema(
    [
        pa.field("link_id", pa.string(), nullable=False),
        pa.field("identity_id", pa.string(), nullable=False),
        # source_type: 'git_email' | 'git_name' | 'github_login' | 'jira_username'
        #            | 'mailing_list_address'
        pa.field("source_type", pa.string(), nullable=False),
        pa.field("source_value", pa.string(), nullable=False),
        # confidence: 'exact' | 'high' | 'medium' | 'low'
        pa.field("confidence", pa.string(), nullable=False),
        pa.field("evidence", pa.string(), nullable=True),
        pa.field("linked_by", pa.string(), nullable=True),
        pa.field("linked_at", TIMESTAMP_UTC, nullable=False),
    ]
)

# --- Fact tables --------------------------------------------------------

CONTRIBUTION_EVENT = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("identity_id", pa.string(), nullable=True),
        # raw identifier as collected, e.g. type='git_email', value='alice@example.org' —
        # always populated by collectors, so identity resolution can be re-run over
        # immutable raw data (ARCHITECTURE.md §3, D2)
        pa.field("author_raw_type", pa.string(), nullable=False),
        pa.field("author_raw_value", pa.string(), nullable=False),
        pa.field("author_display_name", pa.string(), nullable=True),
        # event_type: one of `schema.event_types.CONTRIBUTION_EVENT_TYPES`
        # ('code_commit' today; METRICS.md §0.3)
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("occurred_at", TIMESTAMP_UTC, nullable=False),
        pa.field("repo", pa.string(), nullable=True),
        # natural identifier within its source: commit SHA, PR number, issue key, ...
        pa.field("source_ref", pa.string(), nullable=False),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

REVIEW_EVENT = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        # source: 'commit_trailer' | 'jira_field' (ARCHITECTURE.md §3.1)
        pa.field("source", pa.string(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("reviewer_identity_id", pa.string(), nullable=True),
        # raw reviewer identifier: commit-trailer names use type='git_name',
        # JIRA reviewer fields use type='jira_username' — always populated by collectors
        pa.field("reviewer_raw_type", pa.string(), nullable=False),
        pa.field("reviewer_raw_value", pa.string(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("author_identity_id", pa.string(), nullable=True),
        # raw author (patch-by) identifier, when the source records one; not every
        # review_event row has a known patch author at collection time
        pa.field("author_raw_type", pa.string(), nullable=True),
        pa.field("author_raw_value", pa.string(), nullable=True),
        pa.field("issue_key", pa.string(), nullable=True),
        pa.field("repo", pa.string(), nullable=True),
        pa.field("occurred_at", TIMESTAMP_UTC, nullable=False),
        pa.field("evidence", pa.string(), nullable=True),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

FILE_CHANGE_EVENT = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors. Resolves via
        # the same (author_raw_type, author_raw_value) pair as the commit's own
        # `contribution_event` row -- a file_change_event's author is never a distinct
        # identity from that commit's author.
        pa.field("identity_id", pa.string(), nullable=True),
        pa.field("author_raw_type", pa.string(), nullable=False),
        pa.field("author_raw_value", pa.string(), nullable=False),
        pa.field("author_display_name", pa.string(), nullable=True),
        # change_type: the first letter of `git log --name-status`'s status code for this
        # (commit, file) pair -- 'A' (added), 'M' (modified), 'D' (deleted), 'R' (renamed),
        # 'C' (copied), 'T' (type changed); any similarity-score digits git appends to R/C
        # (e.g. 'R100') are dropped (issue #53, truck_factor). For a rename/copy row,
        # `file_path` is the destination path -- history under the source path is not
        # relinked to it (a documented truck_factor limitation: DOA is computed per literal
        # path, not per rename-followed file identity, matching the Avelino et al. paper's
        # own git-log-based approach).
        pa.field("change_type", pa.string(), nullable=False),
        pa.field("file_path", pa.string(), nullable=False),
        pa.field("occurred_at", TIMESTAMP_UTC, nullable=False),
        pa.field("repo", pa.string(), nullable=True),
        # natural identifier within its source: commit SHA (same as the commit's
        # `contribution_event.source_ref`)
        pa.field("source_ref", pa.string(), nullable=False),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

ISSUE = pa.schema(
    [
        pa.field("issue_key", pa.string(), nullable=False),
        pa.field("summary", pa.string(), nullable=True),
        pa.field("status", pa.string(), nullable=True),
        pa.field("status_category", pa.string(), nullable=True),
        pa.field("priority", pa.string(), nullable=True),
        pa.field("issue_type", pa.string(), nullable=True),
        pa.field("created_at", TIMESTAMP_UTC, nullable=False),
        pa.field("updated_at", TIMESTAMP_UTC, nullable=False),
        pa.field("resolved_at", TIMESTAMP_UTC, nullable=True),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("reporter_identity_id", pa.string(), nullable=True),
        # raw JIRA username as collected; nullable because not every issue has a
        # reporter/assignee set
        pa.field("reporter_raw", pa.string(), nullable=True),
        pa.field("assignee_identity_id", pa.string(), nullable=True),
        pa.field("assignee_raw", pa.string(), nullable=True),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

ROSTER_ENTRY = pa.schema(
    [
        pa.field("entry_id", pa.string(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("identity_id", pa.string(), nullable=True),
        # raw ASF id as collected; always populated by collectors
        pa.field("asf_id", pa.string(), nullable=False),
        pa.field("display_name", pa.string(), nullable=True),
        # role: 'pmc' (from committee-info.json) or 'committer' (from other sources)
        pa.field("role", pa.string(), nullable=False),
        # project: typically 'cassandra' for this project
        pa.field("project", pa.string(), nullable=False),
        # effective_from: join date if available, null otherwise (per issue requirements)
        pa.field("effective_from", pa.date32(), nullable=True),
        # raw date string as collected (for audit trail)
        pa.field("effective_from_raw", pa.string(), nullable=True),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

# --- Run / provenance registry tables (ARCHITECTURE.md §5) ------------------

SOURCE_SNAPSHOT = pa.schema(
    [
        pa.field("snapshot_id", pa.string(), nullable=False),
        pa.field("run_id", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("collected_at", TIMESTAMP_UTC, nullable=False),
        pa.field("watermark_value", pa.string(), nullable=True),
        pa.field("record_count", pa.int64(), nullable=False),
    ]
)

RUN_MANIFEST = pa.schema(
    [
        pa.field("run_id", pa.string(), nullable=False),
        # trigger: 'schedule' | 'workflow_dispatch:backfill' | 'workflow_dispatch:monthly-edition'
        pa.field("trigger", pa.string(), nullable=False),
        pa.field("started_at", TIMESTAMP_UTC, nullable=False),
        pa.field("completed_at", TIMESTAMP_UTC, nullable=True),
        pa.field("pipeline_code_sha", pa.string(), nullable=False),
        pa.field("data_branch_commit", pa.string(), nullable=True),
        pa.field("site_deploy_status", pa.string(), nullable=True),
        pa.field("metrics_computed", pa.list_(pa.string()), nullable=True),
        pa.field("metrics_skipped_insufficient_data", pa.list_(pa.string()), nullable=True),
        # per-source status/watermark/record_count detail (ARCHITECTURE.md §5 manifest
        # example), flattened to a JSON string column so this table stays a flat,
        # Parquet-friendly shape rather than a nested struct-of-struct.
        pa.field("sources_json", pa.string(), nullable=True),
    ]
)

METRIC_DEFINITION_VERSION = pa.schema(
    [
        pa.field("metric_id", pa.string(), nullable=False),
        pa.field("version", pa.string(), nullable=False),
        pa.field("description", pa.string(), nullable=True),
        pa.field("changed_at", TIMESTAMP_UTC, nullable=False),
        pa.field("changelog_note", pa.string(), nullable=True),
    ]
)

# --- Output contract (issue #2, consumed by metrics #7 and site #8) --------

METRIC_VALUE = pa.schema(
    [
        pa.field("metric_id", pa.string(), nullable=False),
        pa.field("definition_version", pa.string(), nullable=False),
        pa.field("window_start", pa.date32(), nullable=False),
        pa.field("window_end", pa.date32(), nullable=False),
        pa.field("value", pa.float64(), nullable=True),
        pa.field("n", pa.int64(), nullable=False),
        # flag: 'ok' | 'insufficient_data' (METRICS.md §0.6)
        pa.field("flag", pa.string(), nullable=False),
        pa.field("run_id", pa.string(), nullable=False),
        pa.field("computed_at", TIMESTAMP_UTC, nullable=False),
        pa.field("details_json", pa.string(), nullable=True),
    ]
)

TABLE_SCHEMAS: dict[str, pa.Schema] = {
    "person_identity": PERSON_IDENTITY,
    "identity_link": IDENTITY_LINK,
    "contribution_event": CONTRIBUTION_EVENT,
    "file_change_event": FILE_CHANGE_EVENT,
    "review_event": REVIEW_EVENT,
    "issue": ISSUE,
    "roster_entry": ROSTER_ENTRY,
    "source_snapshot": SOURCE_SNAPSHOT,
    "run_manifest": RUN_MANIFEST,
    "metric_definition_version": METRIC_DEFINITION_VERSION,
    "metric_value": METRIC_VALUE,
}
