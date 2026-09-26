"""PyArrow schemas for the M0 normalized tables (ARCHITECTURE.md §3).

Identifiers (`*_id`, `*_key`) are modeled as ``pa.string()`` (e.g. UUIDs as
their canonical string form) rather than a UUID extension type, for simple,
portable Parquet interop. All timestamps are UTC-aware
(``pa.timestamp("us", tz="UTC")``); the `metric_value` window bounds are
plain dates (``pa.date32()``) per the issue's output contract.

Every fact table (`contribution_event`, `file_change_event`, `review_event`,
`issue`, `message`, `message_thread`) carries a `source_snapshot_id` column
(ARCHITECTURE.md §3), tracing a row back to the collector run that produced
it. Identity tables and run/metric-registry tables do not.

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

# `ISSUE_COMMENT` (issue #54; ARCHITECTURE.md §3's already-planned table name
# for tracker-agnostic issue comment metadata): comment *metadata* for an
# issue -- author and timestamp only, never the comment body -- collected as
# a side effect of `collectors/jira.py`'s existing `/rest/api/2/search` fetch
# (the `comment` field is requested alongside the standard issue fields
# already fetched, so this costs zero extra HTTP calls / API budget beyond
# what the M0 JIRA collector already spends). This is the "genuinely missing
# field" issue #54 needed to compute `time_to_first_response_jira`
# (METRICS.md §4): a first human, non-bot comment's author/timestamp per
# issue. Bot exclusion is applied at metrics-compute time (`metrics/
# engine.py`'s `_bot_identifiers`), not at collection time, matching every
# other raw fact table's convention. Named generically (not `jira_comment`)
# per ARCHITECTURE.md's own tracker-agnostic table list -- distinct from the
# unrelated `"jira_comment"` *string* `classify/preprocess.py`'s Phase 2a
# pipeline uses as a `source` tag for message preprocessing/classification.
ISSUE_COMMENT = pa.schema(
    [
        pa.field("comment_id", pa.string(), nullable=False),
        pa.field("issue_key", pa.string(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("author_identity_id", pa.string(), nullable=True),
        pa.field("author_raw_type", pa.string(), nullable=False),
        # null when JIRA returns a comment with no author (deleted account)
        pa.field("author_raw_value", pa.string(), nullable=True),
        pa.field("created_at", TIMESTAMP_UTC, nullable=False),
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

MESSAGE = pa.schema(
    [
        pa.field("message_id", pa.string(), nullable=False),
        # list: e.g. 'dev' | 'user' (projects/<id>.yaml `mailing_lists.lists`)
        pa.field("list", pa.string(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("sender_identity_id", pa.string(), nullable=True),
        # raw identifier as collected: always 'mailing_list_address' for this
        # source (ARCHITECTURE.md §3, schema/README.md "Raw identifiers vs.
        # resolved identity"); sender_raw_value is the lowercased `From:`
        # address.
        pa.field("sender_raw_type", pa.string(), nullable=False),
        pa.field("sender_raw_value", pa.string(), nullable=False),
        pa.field("sender_display_name", pa.string(), nullable=True),
        pa.field("occurred_at", TIMESTAMP_UTC, nullable=False),
        # D1/D16: sha256 hex digest of the raw `Subject:` header text --
        # never the subject text itself. No message body is ever read past
        # header-extraction time; see collectors/ponymail.py module
        # docstring and tests/test_ponymail_collector.py's
        # `test_no_body_content_reaches_any_output_column`.
        pa.field("subject_hash", pa.string(), nullable=False),
        # Message-ID this message's `In-Reply-To:` header names, if any.
        pa.field("in_reply_to", pa.string(), nullable=True),
        # Every Message-ID listed in this message's `References:` header, in
        # header order (oldest-first per RFC 5322 convention).
        pa.field("references", pa.list_(pa.string()), nullable=True),
        # Best-effort thread grouping key -- see collectors/ponymail.py
        # module docstring for the exact rule.
        pa.field("thread_id", pa.string(), nullable=False),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

# --- Affiliation (D6, issue #52) ---------------------------------------

AFFILIATION_PERIOD = pa.schema(
    [
        pa.field("entry_id", pa.string(), nullable=False),
        pa.field("identity_id", pa.string(), nullable=False),
        # organization: a curated/heuristic org name, or "unknown"
        # (normalize.affiliation.UNKNOWN_ORG) when unresolved -- D6 never
        # guesses, so "unknown" is an explicit, first-class value here, not
        # the absence of a row.
        pa.field("organization", pa.string(), nullable=False),
        # dated range (D6: "like CNCF gitdm"); null effective_from/effective_to
        # means "since always"/"still ongoing" -- only `source = 'curated'`
        # rows (from affiliations.yaml) are expected to carry real dates in
        # M0; the email_domain/github_company heuristics are undated (a
        # domain or a GitHub profile's `company` field says nothing about
        # *when* that affiliation started or ended), so they cover all time
        # unless a curated row for the same identity/date wins first
        # (normalize/affiliation.py's source-priority join).
        pa.field("effective_from", pa.date32(), nullable=True),
        pa.field("effective_to", pa.date32(), nullable=True),
        # source: 'curated' (affiliations.yaml, PR-reviewed, wins over
        # heuristics) | 'email_domain' (org_domains.yaml, a reviewed
        # domain->org map) | 'github_company' (a GitHub profile's public
        # `company` field, the least-reviewed of the three -- D6).
        pa.field("source", pa.string(), nullable=False),
        pa.field("evidence", pa.string(), nullable=True),
    ]
)

# --- GitHub commit-author association cache (D6, issue #52 fixup cycle 1) --

GITHUB_COMMIT_AUTHOR = pa.schema(
    [
        # git commit sha this association was observed on (evidence, D2.3).
        pa.field("sha", pa.string(), nullable=False),
        # commit author email exactly as GitHub's GraphQL API reports it
        # (case as GitHub returns it; normalize/affiliation.py lowercases
        # when joining against git_email identities).
        pa.field("email", pa.string(), nullable=False),
        # GitHub login GitHub itself asserts as this email's account owner
        # (`author.user.login`) -- an EXACT, platform-asserted fact, not a
        # guess. Only rows where GitHub found a linked account are written;
        # a commit whose author has no linked GitHub account has nothing to
        # cache.
        pa.field("login", pa.string(), nullable=False),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

MESSAGE_THREAD = pa.schema(
    [
        pa.field("thread_id", pa.string(), nullable=False),
        pa.field("list", pa.string(), nullable=False),
        pa.field("root_message_id", pa.string(), nullable=False),
        pa.field("started_at", TIMESTAMP_UTC, nullable=False),
        pa.field("last_activity_at", TIMESTAMP_UTC, nullable=False),
        pa.field("message_count", pa.int64(), nullable=False),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

# --- Security tables (issue #55, D21 item 3) --------------------------------
#
# Both tables are collected fresh on every `security` source run and are
# never overwritten (storage.write_partition's append-only guarantee) --
# this is deliberately how issue #55 gets "history from now on" for the
# OpenSSF Scorecard without a bespoke history mechanism: every run's raw
# per-check rows simply accumulate under their own `date=<run date>`
# partition, same as every other raw table.

SCORECARD_CHECK = pa.schema(
    [
        pa.field("check_id", pa.string(), nullable=False),
        # the OpenSSF-scored repo, e.g. "github.com/apache/cassandra"
        pa.field("repo", pa.string(), nullable=False),
        # the date field in api.securityscorecards.dev's response -- the
        # date the Scorecard run itself was computed, not our collection date
        pa.field("scorecard_date", pa.date32(), nullable=True),
        pa.field("scorecard_version", pa.string(), nullable=True),
        # the aggregate 0-10 score for this run, repeated on every check row
        # so a reader never has to join back to a separate "run" table to
        # see it next to a check (RESEARCH.md §6.2: never presented alone)
        pa.field("overall_score", pa.float64(), nullable=True),
        pa.field("check_name", pa.string(), nullable=False),
        # -1 means "not applicable / not detected" per Scorecard's own
        # convention (e.g. Packaging, Signed-Releases on apache/cassandra)
        pa.field("check_score", pa.float64(), nullable=True),
        pa.field("check_reason", pa.string(), nullable=True),
        # `details` list items joined, truncated -- the free-text evidence
        # Scorecard itself returns for this check
        pa.field("check_details_summary", pa.string(), nullable=True),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
        pa.field("collected_at", TIMESTAMP_UTC, nullable=False),
    ]
)

SECURITY_ADVISORY = pa.schema(
    [
        pa.field("advisory_id", pa.string(), nullable=False),
        pa.field("cve_id", pa.string(), nullable=False),
        pa.field("published_date", pa.date32(), nullable=True),
        pa.field("last_modified_date", pa.date32(), nullable=True),
        # severity: NVD's own baseSeverity string ('LOW'|'MEDIUM'|'HIGH'|'CRITICAL'),
        # null if NVD hasn't scored it
        pa.field("severity", pa.string(), nullable=True),
        pa.field("cvss_score", pa.float64(), nullable=True),
        pa.field("cvss_version", pa.string(), nullable=True),
        pa.field("summary", pa.string(), nullable=True),
        # human-readable summary of affected version range(s)/enumeration,
        # derived from the source's CPE match data -- metadata only, never
        # a claim about which line of code was vulnerable
        pa.field("affected_versions", pa.string(), nullable=True),
        pa.field("fixed_versions", pa.string(), nullable=True),
        pa.field("advisory_url", pa.string(), nullable=False),
        # source: 'nvd' (v1; cve.org/other sources may be added later)
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
        pa.field("collected_at", TIMESTAMP_UTC, nullable=False),
    ]
)

# --- GitHub profile cache (D6, issue #52) -------------------------------

GITHUB_PROFILE = pa.schema(
    [
        # GitHub login (case-sensitive as GitHub returns it).
        pa.field("login", pa.string(), nullable=False),
        # The profile's public `company` field, verbatim, or null if unset --
        # never inferred/guessed (D6). Free text, not itself a validated
        # organization name.
        pa.field("company", pa.string(), nullable=True),
        pa.field("fetched_at", TIMESTAMP_UTC, nullable=False),
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

# --- GitHub PR metadata (ARCHITECTURE.md §3; issue #51) ---------------------
#
# Metadata only, per D1/DECISIONS.md: PR/comment *bodies* are Phase 2a
# (classification) territory. `pr.title_hash` carries a sha256 hex digest of
# the PR title -- never the raw title string -- so a PR's subject line is
# still joinable/diffable across snapshots without persisting text content.
# `*_raw_type`/`*_raw_value` follow the same collectors-write-raw,
# identity-resolution-fills-`*_identity_id`-later split as every other fact
# table (see module docstring); GitHub's raw identifier type is always
# `'github_login'` here. A `None` `*_raw_value` means GitHub returned a null
# `author` (e.g. a deleted account), not a missing column.

PR = pa.schema(
    [
        pa.field("repo", pa.string(), nullable=False),
        pa.field("number", pa.int64(), nullable=False),
        # state: 'OPEN' | 'CLOSED' | 'MERGED' (GitHub GraphQL PullRequestState)
        pa.field("state", pa.string(), nullable=False),
        pa.field("is_draft", pa.bool_(), nullable=False),
        pa.field("merged", pa.bool_(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("author_identity_id", pa.string(), nullable=True),
        pa.field("author_raw_type", pa.string(), nullable=False),
        pa.field("author_raw_value", pa.string(), nullable=True),
        # sha256 hex digest of the PR title -- metadata only, never the raw title
        pa.field("title_hash", pa.string(), nullable=False),
        pa.field("created_at", TIMESTAMP_UTC, nullable=False),
        pa.field("updated_at", TIMESTAMP_UTC, nullable=False),
        pa.field("closed_at", TIMESTAMP_UTC, nullable=True),
        pa.field("merged_at", TIMESTAMP_UTC, nullable=True),
        pa.field("additions", pa.int64(), nullable=True),
        pa.field("deletions", pa.int64(), nullable=True),
        pa.field("changed_files", pa.int64(), nullable=True),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

PR_REVIEW = pa.schema(
    [
        # GitHub GraphQL global node id of the PullRequestReview
        pa.field("review_id", pa.string(), nullable=False),
        pa.field("repo", pa.string(), nullable=False),
        pa.field("pr_number", pa.int64(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("reviewer_identity_id", pa.string(), nullable=True),
        pa.field("reviewer_raw_type", pa.string(), nullable=False),
        pa.field("reviewer_raw_value", pa.string(), nullable=True),
        # state: 'PENDING' | 'COMMENTED' | 'APPROVED' | 'CHANGES_REQUESTED' | 'DISMISSED'
        pa.field("state", pa.string(), nullable=False),
        # null for a still-'PENDING' review that hasn't been submitted yet
        pa.field("submitted_at", TIMESTAMP_UTC, nullable=True),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

PR_COMMENT = pa.schema(
    [
        # GitHub GraphQL global node id of the IssueComment/PullRequestReviewComment
        pa.field("comment_id", pa.string(), nullable=False),
        pa.field("repo", pa.string(), nullable=False),
        pa.field("pr_number", pa.int64(), nullable=False),
        # FK to pr_review.review_id when comment_type='review_comment'; null for
        # a general conversation ('issue_comment') comment
        pa.field("review_id", pa.string(), nullable=True),
        # comment_type: 'issue_comment' | 'review_comment'
        pa.field("comment_type", pa.string(), nullable=False),
        # filled in by identity resolution (#6); null as written by collectors
        pa.field("author_identity_id", pa.string(), nullable=True),
        pa.field("author_raw_type", pa.string(), nullable=False),
        pa.field("author_raw_value", pa.string(), nullable=True),
        pa.field("created_at", TIMESTAMP_UTC, nullable=False),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

# --- Governance compliance engine (issue #36, D14/D15) ---------------------
#
# `COMMIT_COMPLIANCE` is the per-(commit, check) scoring output of
# `governance/engine.py`: one row per commit per scored `check_id` from
# `governance-policy.yaml` (`reviewer-present`, `jira-ticket-referenced`,
# `pre-commit-ci-evidence`, `code-style-checkstyle`). Runs on every commit,
# including merge commits that carry a real reviewer trailer
# (docs/spec/GOVERNANCE.md §3) — `branch`/`commit_date` are the commit's own,
# not a window; `reviewers`/`jira_keys` are the full set found on the commit
# by any evidence source, shown alongside every check_id row for that sha
# (D15: "full per-commit detail, including names"). `evidence` always
# describes what the result is based on (D15); `evidence_url`, when present,
# links straight to that evidence (a JIRA comment, a GitHub check-run).
#
# `COMMIT_FACT` holds the three `scored: false` rules
# (`changes-txt-entry`/`news-txt-entry`/`test-touched`) — plain per-commit
# facts displayed on the commit row, never a pass/fail/unknown verdict
# (governance-policy.yaml's own `result_semantics.display_only: true`), kept
# in a separate table from `COMMIT_COMPLIANCE` so "a scored check result"
# and "a displayed fact" are never confused in the data model.

COMMIT_COMPLIANCE = pa.schema(
    [
        pa.field("sha", pa.string(), nullable=False),
        pa.field("branch", pa.string(), nullable=False),
        pa.field("commit_date", TIMESTAMP_UTC, nullable=False),
        pa.field("author", pa.string(), nullable=False),
        pa.field("committer", pa.string(), nullable=False),
        # True for a merge commit (>=2 parents) — always scored (GOVERNANCE.md
        # §3), but excluded from `governance/metrics.py`'s aggregate-rate
        # denominators, which is why this flag travels with every row rather
        # than being re-derived at aggregation time.
        pa.field("is_merge", pa.bool_(), nullable=False),
        pa.field("reviewers", pa.list_(pa.string()), nullable=False),
        pa.field("jira_keys", pa.list_(pa.string()), nullable=False),
        pa.field("policy_version", pa.int64(), nullable=False),
        # check_id: one of governance-policy.yaml `rules[].id`
        # ('reviewer-present' | 'jira-ticket-referenced' |
        # 'pre-commit-ci-evidence' | 'code-style-checkstyle')
        pa.field("check_id", pa.string(), nullable=False),
        # result: 'pass' | 'fail' | 'unknown' | 'exempt' | 'not_in_force'
        # (governance-policy.yaml top-level `result_states`)
        pa.field("result", pa.string(), nullable=False),
        pa.field("evidence", pa.string(), nullable=False),
        pa.field("evidence_url", pa.string(), nullable=True),
    ]
)

COMMIT_FACT = pa.schema(
    [
        pa.field("sha", pa.string(), nullable=False),
        pa.field("branch", pa.string(), nullable=False),
        pa.field("commit_date", TIMESTAMP_UTC, nullable=False),
        pa.field("changes_txt_touched", pa.bool_(), nullable=False),
        pa.field("news_txt_touched", pa.bool_(), nullable=False),
        pa.field("test_touched", pa.bool_(), nullable=False),
    ]
)

# --- Governance raw evidence (issue #36 fixup cycle 1: incremental collection) --
#
# Everything below is *raw*, append-only, watermarked collector output
# (ARCHITECTURE.md §4.3), the same contract as `contribution_event`/`issue` —
# `governance/engine.py`'s scoring step (`pipeline.py`'s `_collect_governance`)
# always recomputes `COMMIT_COMPLIANCE`/`COMMIT_FACT`/`metric_value` fresh from
# the *entire* accumulated raw cache below (D3), never incrementally; only the
# three collectors that populate these three tables are incremental, each
# against its own external source's actual rate/retention constraints.
#
# `GOVERNANCE_COMMIT_RECORD` is `collectors/governance_git.py`'s walked commit
# output, persisted so the (unbounded, but cheap/local) git walk itself never
# has to re-walk history it already has — watermarked by commit SHA range,
# same mechanism as `collectors/git.py`'s own watermark, but tracked under a
# separate `state/watermarks.json` key (`governance_git`) since this walk
# deliberately includes merge commits `collectors/git.py`'s walk excludes.
GOVERNANCE_COMMIT_RECORD = pa.schema(
    [
        pa.field("sha", pa.string(), nullable=False),
        pa.field("branch", pa.string(), nullable=False),
        pa.field("commit_date", TIMESTAMP_UTC, nullable=False),
        pa.field("message", pa.string(), nullable=False),
        pa.field("author", pa.string(), nullable=False),
        pa.field("author_email", pa.string(), nullable=False),
        pa.field("committer", pa.string(), nullable=False),
        pa.field("committer_email", pa.string(), nullable=False),
        pa.field("is_merge", pa.bool_(), nullable=False),
        pa.field("trailer_reviewers", pa.list_(pa.string()), nullable=False),
        pa.field("issue_keys", pa.list_(pa.string()), nullable=False),
        # null for a merge commit (changed-path collection is a non-merge-only
        # bulk `git log --name-only` pass, `collectors/governance_git.py`);
        # an empty (non-null) list for a real non-merge commit that touched
        # no listed path is never expected in practice but is a valid value.
        pa.field("changed_paths", pa.list_(pa.string()), nullable=True),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

# `GOVERNANCE_CI_EVIDENCE` is one row per (issue_key, check attempt) for
# `pre-commit-ci-evidence`'s JIRA-comment evidence source
# (`collectors/jira_comments.py`) — **comment metadata plus the matched CI
# URL only, never the comment body** (issue #36 scope). `issue_updated_at` is
# this row's watermark value (the JIRA issue's own `updated` field, "reusing
# the JIRA collector's approach" per the fixup-cycle-1 review): a row is
# written for *every* checked issue, `found=False` and the four evidence
# columns null when no comment matched, so "have we already checked this
# issue since it last changed" is answerable by comparing the latest row's
# `issue_updated_at` to the issue's current `updated_at` — without a second,
# separate state file.
GOVERNANCE_CI_EVIDENCE = pa.schema(
    [
        pa.field("issue_key", pa.string(), nullable=False),
        pa.field("issue_updated_at", TIMESTAMP_UTC, nullable=False),
        pa.field("checked_at", TIMESTAMP_UTC, nullable=False),
        pa.field("found", pa.bool_(), nullable=False),
        pa.field("comment_id", pa.string(), nullable=True),
        pa.field("comment_author", pa.string(), nullable=True),
        pa.field("comment_created_at", pa.string(), nullable=True),
        pa.field("matched_term", pa.string(), nullable=True),
        pa.field("matched_url", pa.string(), nullable=True),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

# `GOVERNANCE_CHECK_RUN` is one row per (sha, check attempt) for
# `code-style-checkstyle`'s GitHub-check-runs evidence source
# (`collectors/github_checks.py`). `check_run_name`/`conclusion` are both
# null for a "checked, no checkstyle run recorded at all yet" sentinel row
# (GitHub's Checks API found nothing for that sha at fetch time) — this is
# what lets a later run tell "never checked" (`fetch_checkstyle_evidence_for_shas`
# never called for this sha) apart from "checked and still pending/absent"
# (a sentinel row exists, so the fixup-cycle-1 30-day re-fetch window applies)
# without a second table. `commit_date` is denormalized from
# `GOVERNANCE_COMMIT_RECORD` so eligibility ("is this commit under 30 days
# old") never needs a join back to it.
GOVERNANCE_CHECK_RUN = pa.schema(
    [
        pa.field("sha", pa.string(), nullable=False),
        pa.field("commit_date", TIMESTAMP_UTC, nullable=False),
        pa.field("check_run_name", pa.string(), nullable=True),
        pa.field("conclusion", pa.string(), nullable=True),
        pa.field("html_url", pa.string(), nullable=True),
        pa.field("fetched_at", TIMESTAMP_UTC, nullable=False),
        pa.field("source_snapshot_id", pa.string(), nullable=False),
    ]
)

# --- Phase 2a classification (issue #45, COMMUNITY-HEALTH.md §4.3, D17) -----
#
# One row per classified message, in the exact shape COMMUNITY-HEALTH.md §4.3
# defines. Unlike every table above, this one is genuinely nested JSON by the
# spec's own design (`labels`, `usage`, `tone_intensity`, `sentiment_polarity`
# are all objects) -- rather than flattening those into opaque `_json` string
# columns (the `metric_value.details_json` pattern used elsewhere in this
# file), this table uses pyarrow struct/map types directly, so `validate()`
# actually checks the nested shape column-by-column instead of trusting an
# unstructured blob. `labels` carries exactly the 12 message-level labels
# from COMMUNITY-HEALTH.md §1.2 (`CLASSIFICATION_LABEL_NAMES` below); each is
# nullable at the struct-field level because not every label needs to be
# present on the label object that produced this row (v1 always asks all 12,
# but the schema shouldn't hard-fail if a future revision doesn't).
#
# `src/project_health/classify/classifier.py`'s `ClassificationRecord`
# (pydantic) is the runtime-validated version of this same schema -- this
# module has no import dependency on `classify/` (schema/ stays a leaf
# package), so `CLASSIFICATION_LABEL_NAMES` below is a plain tuple literal
# kept in sync with `classify.questions.MESSAGE_LEVEL_LABELS` by hand (both
# are enforced against COMMUNITY-HEALTH.md §1.2 by their own tests).

CLASSIFICATION_LABEL_NAMES: tuple[str, ...] = (
    "technical_disagreement",
    "constructive_counterargument",
    "evidence_based_argument",
    "compromise_offer",
    "acknowledgment",
    "personal_attack",
    "hostility",
    "dismissiveness",
    "sarcasm",
    "gatekeeping",
    "status_authority_invocation",
    "resolution_marker",
)

_CLASSIFICATION_LABEL = pa.struct([pa.field("probability", pa.float64(), nullable=False)])

_CLASSIFICATION_USAGE = pa.struct(
    [
        pa.field("input_tokens", pa.int64(), nullable=False),
        pa.field("output_tokens", pa.int64(), nullable=False),
    ]
)

_CLASSIFICATION_TONE_INTENSITY = pa.struct(
    [
        pa.field("score", pa.float64(), nullable=True),
        pa.field("confidence", pa.float64(), nullable=True),
        # Per-level probability distribution, keyed by level index as a string
        # (COMMUNITY-HEALTH.md §4.3).
        pa.field("probabilities", pa.map_(pa.string(), pa.float64()), nullable=True),
    ]
)

_CLASSIFICATION_SENTIMENT_POLARITY = pa.struct(
    [
        # value: 'negative' | 'neutral' | 'positive' | 'mixed'
        pa.field("value", pa.string(), nullable=True),
        pa.field("confidence", pa.float64(), nullable=True),
    ]
)

CLASSIFICATION = pa.schema(
    [
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("message_id", pa.string(), nullable=False),
        pa.field("thread_id", pa.string(), nullable=False),
        # source: 'mailing_list' | 'jira_comment' | 'github_pr_comment' | 'slack'
        pa.field("source", pa.string(), nullable=False),
        pa.field("classifier_version", pa.string(), nullable=False),
        # Version of questions_v1.yaml specifically (D17), distinct from
        # classifier_version's broader (question set + schema + post-processing)
        # bundle versioning (COMMUNITY-HEALTH.md §4.2).
        pa.field("question_set_version", pa.string(), nullable=False),
        # The `model` field the provider's response actually reported, e.g.
        # 'jev-1.13.0' -- not just the pinned value requested (D17/§4.4).
        pa.field("model_id", pa.string(), nullable=False),
        # sha256 of the exact normalized text + context window sent to the
        # model (§4.3) -- what the input-hash cache
        # (classify/classifier.py::ClassificationCache) keys on.
        pa.field("input_hash", pa.string(), nullable=False),
        pa.field("classified_at", TIMESTAMP_UTC, nullable=False),
        # Tracked against D10's owner-funded monthly cost cap.
        pa.field("usage", _CLASSIFICATION_USAGE, nullable=False),
        pa.field(
            "labels",
            pa.struct(
                [
                    pa.field(name, _CLASSIFICATION_LABEL, nullable=True)
                    for name in CLASSIFICATION_LABEL_NAMES
                ]
            ),
            nullable=True,
        ),
        pa.field("tone_intensity", _CLASSIFICATION_TONE_INTENSITY, nullable=True),
        pa.field("sentiment_polarity", _CLASSIFICATION_SENTIMENT_POLARITY, nullable=True),
        pa.field("human_reviewed", pa.bool_(), nullable=False),
        pa.field("human_label_id", pa.string(), nullable=True),
        # record_id of a correction, if any (§7.6) -- the original record is
        # never deleted (D2 rule 6 / §4.3 "Notes").
        pa.field("superseded_by", pa.string(), nullable=True),
    ]
)

# --- Contributor leaderboard (D19, issue #56) -------------------------------
#
# One row per (activity_type, rank) per run: the top-N ranking for one
# activity type (never blended across types, D19/D2 rule 7) over a trailing
# 12-completed-month window (`leaderboard.py`). Deliberately not part of
# `metric_value`/`METRIC_IDS` -- see `leaderboard.py`'s module docstring for
# why this is a table, not a registered time-series metric.
CONTRIBUTOR_LEADERBOARD = pa.schema(
    [
        pa.field("run_id", pa.string(), nullable=False),
        # activity_type: 'commits' | 'reviews' | 'jira_issues_resolved'
        # (leaderboard.ACTIVITY_TYPES)
        pa.field("activity_type", pa.string(), nullable=False),
        pa.field("definition_version", pa.string(), nullable=False),
        pa.field("window_start", pa.date32(), nullable=False),
        pa.field("window_end", pa.date32(), nullable=False),
        pa.field("rank", pa.int64(), nullable=False),
        pa.field("identity_id", pa.string(), nullable=False),
        pa.field("display_name", pa.string(), nullable=True),
        # organization: affiliation_period-resolved org as of window_end, or
        # "unknown" (normalize.affiliation.UNKNOWN_ORG) when unresolved --
        # D6/D19: never guessed.
        pa.field("organization", pa.string(), nullable=False),
        pa.field("count", pa.int64(), nullable=False),
        pa.field("computed_at", TIMESTAMP_UTC, nullable=False),
    ]
)

TABLE_SCHEMAS: dict[str, pa.Schema] = {
    "person_identity": PERSON_IDENTITY,
    "identity_link": IDENTITY_LINK,
    "contribution_event": CONTRIBUTION_EVENT,
    "file_change_event": FILE_CHANGE_EVENT,
    "review_event": REVIEW_EVENT,
    "issue": ISSUE,
    "issue_comment": ISSUE_COMMENT,
    "roster_entry": ROSTER_ENTRY,
    "message": MESSAGE,
    "message_thread": MESSAGE_THREAD,
    "scorecard_check": SCORECARD_CHECK,
    "security_advisory": SECURITY_ADVISORY,
    "affiliation_period": AFFILIATION_PERIOD,
    "github_commit_author": GITHUB_COMMIT_AUTHOR,
    "github_profile": GITHUB_PROFILE,
    "source_snapshot": SOURCE_SNAPSHOT,
    "run_manifest": RUN_MANIFEST,
    "metric_definition_version": METRIC_DEFINITION_VERSION,
    "metric_value": METRIC_VALUE,
    "pr": PR,
    "pr_review": PR_REVIEW,
    "pr_comment": PR_COMMENT,
    "commit_compliance": COMMIT_COMPLIANCE,
    "commit_fact": COMMIT_FACT,
    # Registered under `source='governance'`'s own bare table names (matching
    # `contribution_event`'s "source namespaces, table name doesn't repeat
    # it" convention) — `storage.write_partition`/`read_table`'s `table`
    # argument is both the schema-registry lookup key and the `raw/<source>/
    # <table>/` directory segment, so these must be the bare names, not
    # `governance_<name>` (the `GOVERNANCE_` prefix on the Python constants
    # above is just this module's own naming choice).
    "commit_record": GOVERNANCE_COMMIT_RECORD,
    "ci_evidence": GOVERNANCE_CI_EVIDENCE,
    "check_run": GOVERNANCE_CHECK_RUN,
    "classification": CLASSIFICATION,
    "contributor_leaderboard": CONTRIBUTOR_LEADERBOARD,
}
