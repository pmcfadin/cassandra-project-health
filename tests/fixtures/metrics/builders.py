"""Hand-built fixture builders for metrics-engine golden tests (issue #7).

These are plain Python builders, not JSON, so a golden test can express a
scenario as a short list of `(who, when, ...)` tuples and let the builder
fill in every other schema-required column with a deterministic default,
matching the raw-column shapes `schema/README.md` documents for each M0 fact
table.

Identity resolution for a scenario's `identity_link` table should go through
`normalize.identity.resolve_identities`/`extract_raw_identifiers` (see
`identity_link_for` below) rather than being hand-assembled, so golden tests
exercise the real #6 resolver a metric will see in production, not a stand-in.
"""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa

from project_health.normalize.identity import (
    RawIdentifier,
    extract_raw_identifiers,
    resolve_identities,
)
from project_health.schema import CODE_COMMIT, get_schema, validate


def contribution_events(rows: list[dict]) -> pa.Table:
    """Build a `contribution_event` table.

    Required per row: `author_raw_type`, `author_raw_value`, `occurred_at`.
    Optional: `author_display_name`, `event_type` (default
    `schema.CODE_COMMIT`, matching what `collectors/git.py` actually writes
    -- issue #24), `repo`, `source_ref`, `event_id`, `source_snapshot_id`.
    """
    built = [
        {
            "event_id": row.get("event_id", f"event-{i}"),
            "identity_id": None,
            "author_raw_type": row["author_raw_type"],
            "author_raw_value": row["author_raw_value"],
            "author_display_name": row.get("author_display_name"),
            "event_type": row.get("event_type", CODE_COMMIT),
            "occurred_at": row["occurred_at"],
            "repo": row.get("repo", "apache/cassandra"),
            "source_ref": row.get("source_ref", f"sha-{i}"),
            "source_snapshot_id": row.get("source_snapshot_id", "snap-1"),
        }
        for i, row in enumerate(rows)
    ]
    schema = get_schema("contribution_event")
    return validate("contribution_event", pa.Table.from_pylist(built, schema=schema))


def file_change_events(rows: list[dict]) -> pa.Table:
    """Build a `file_change_event` table (issue #53, `truck_factor`).

    Required per row: `author_raw_type`, `author_raw_value`, `file_path`,
    `occurred_at`. Optional: `author_display_name`, `change_type` (default
    `"M"`), `repo`, `source_ref`, `event_id`, `source_snapshot_id`.
    """
    built = [
        {
            "event_id": row.get("event_id", f"file-event-{i}"),
            "identity_id": None,
            "author_raw_type": row["author_raw_type"],
            "author_raw_value": row["author_raw_value"],
            "author_display_name": row.get("author_display_name"),
            "change_type": row.get("change_type", "M"),
            "file_path": row["file_path"],
            "occurred_at": row["occurred_at"],
            "repo": row.get("repo", "apache/cassandra"),
            "source_ref": row.get("source_ref", f"sha-{i}"),
            "source_snapshot_id": row.get("source_snapshot_id", "snap-1"),
        }
        for i, row in enumerate(rows)
    ]
    schema = get_schema("file_change_event")
    return validate("file_change_event", pa.Table.from_pylist(built, schema=schema))


def review_events(rows: list[dict]) -> pa.Table:
    """Build a `review_event` table.

    Required per row: `source` (`"commit_trailer"` | `"jira_field"`),
    `reviewer_raw_type`, `reviewer_raw_value`, `occurred_at`. Optional:
    `author_raw_type`/`author_raw_value`, `issue_key`, `repo`, `evidence`,
    `event_id`, `source_snapshot_id`.
    """
    built = [
        {
            "event_id": row.get("event_id", f"review-{i}"),
            "source": row["source"],
            "reviewer_identity_id": None,
            "reviewer_raw_type": row["reviewer_raw_type"],
            "reviewer_raw_value": row["reviewer_raw_value"],
            "author_identity_id": None,
            "author_raw_type": row.get("author_raw_type"),
            "author_raw_value": row.get("author_raw_value"),
            "issue_key": row.get("issue_key"),
            "repo": row.get("repo", "apache/cassandra"),
            "occurred_at": row["occurred_at"],
            "evidence": row.get("evidence"),
            "source_snapshot_id": row.get("source_snapshot_id", "snap-1"),
        }
        for i, row in enumerate(rows)
    ]
    schema = get_schema("review_event")
    return validate("review_event", pa.Table.from_pylist(built, schema=schema))


def issues(rows: list[dict]) -> pa.Table:
    """Build an `issue` table.

    Required per row: `issue_key`, `created_at`, `updated_at`. Optional:
    `resolved_at`, `summary`, `status`, `status_category`, `priority`,
    `issue_type`, `reporter_raw`, `assignee_raw`, `source_snapshot_id`.
    """
    built = [
        {
            "issue_key": row["issue_key"],
            "summary": row.get("summary"),
            "status": row.get("status", "Open" if row.get("resolved_at") is None else "Resolved"),
            "status_category": row.get("status_category"),
            "priority": row.get("priority"),
            "issue_type": row.get("issue_type", "Bug"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row.get("resolved_at"),
            "reporter_identity_id": None,
            "reporter_raw": row.get("reporter_raw"),
            "assignee_identity_id": None,
            "assignee_raw": row.get("assignee_raw"),
            "source_snapshot_id": row.get("source_snapshot_id", "snap-1"),
        }
        for row in rows
    ]
    schema = get_schema("issue")
    return validate("issue", pa.Table.from_pylist(built, schema=schema))


def roster_entries(rows: list[dict]) -> pa.Table:
    """Build a `roster_entry` table.

    Required per row: `asf_id`, `role`, `project`. Optional: `display_name`,
    `effective_from`, `effective_from_raw`, `source_snapshot_id`.
    """
    built = [
        {
            "entry_id": row.get("entry_id", f"entry-{i}"),
            "identity_id": None,  # Filled by identity resolution, not collectors
            "asf_id": row["asf_id"],
            "display_name": row.get("display_name"),
            "role": row["role"],
            "project": row["project"],
            "effective_from": row.get("effective_from"),
            "effective_from_raw": row.get("effective_from_raw"),
            "source_snapshot_id": row.get("source_snapshot_id", "snap-1"),
        }
        for i, row in enumerate(rows)
    ]
    schema = get_schema("roster_entry")
    return validate("roster_entry", pa.Table.from_pylist(built, schema=schema))


def affiliation_periods(rows: list[dict]) -> pa.Table:
    """Build an `affiliation_period` table (issue #52, D6).

    Required per row: `identity_id`, `organization`. Optional:
    `effective_from`, `effective_to` (default `None`, meaning "covers all
    time"), `source` (default `"curated"`), `evidence`, `entry_id`.
    """
    built = [
        {
            "entry_id": row.get("entry_id", f"affiliation-{i}"),
            "identity_id": row["identity_id"],
            "organization": row["organization"],
            "effective_from": row.get("effective_from"),
            "effective_to": row.get("effective_to"),
            "source": row.get("source", "curated"),
            "evidence": row.get("evidence"),
        }
        for i, row in enumerate(rows)
    ]
    schema = get_schema("affiliation_period")
    return validate("affiliation_period", pa.Table.from_pylist(built, schema=schema))


def identity_link_for(
    *,
    contribution_events: pa.Table | None = None,
    review_events: pa.Table | None = None,
    issues: pa.Table | None = None,
    file_change_events: pa.Table | None = None,
    now: datetime,
) -> pa.Table:
    """Resolve `identity_link` for a scenario via the real #6 naive resolver.

    Mirrors what the pipeline does in production: pull every raw identifier
    out of the fact tables collectors would have written, then run them
    through `resolve_identities` -- a golden test's `identity_link` table is
    never hand-assembled independently of the raw columns it's supposed to
    resolve.

    `file_change_events` (issue #53) is accepted for symmetry/completeness
    but its authors are always a subset of `contribution_events`' authors in
    real collection (same commit, same email) -- `extract_raw_identifiers`
    itself has no `file_change_events` parameter, so this only matters for a
    golden test that builds `file_change_event` rows without a matching
    `contribution_event` row for the same author.
    """
    raws = extract_raw_identifiers(
        contribution_events=contribution_events,
        review_events=review_events,
        issues=issues,
    )
    if file_change_events is not None:
        types = file_change_events.column("author_raw_type").to_pylist()
        values = file_change_events.column("author_raw_value").to_pylist()
        names = file_change_events.column("author_display_name").to_pylist()
        raws.extend(
            RawIdentifier(source_type, source_value, display_name)
            for source_type, source_value, display_name in zip(types, values, names, strict=True)
        )
    return resolve_identities(raws, now=now).identity_link
