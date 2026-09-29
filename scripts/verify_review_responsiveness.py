#!/usr/bin/env python3
"""Verification harness for `metrics/review_responsiveness.py` (issue #102).

Feeds a one-off reference data pull -- `cl.jsonl` (JIRA changelog
status/assignee history for every Patch-Available CASSANDRA issue),
`cm.jsonl` (JIRA comment metadata) and `pr.jsonl` (every `apache/cassandra`
GitHub PR with its reviews/comments) -- through this project's *real*
normalization + metric code, and prints a per-year, per-tier table next to
the one-off `blend.py` reference script's own numbers, so the two can be
diffed by eye.

This is a **script**, not a test (same convention as
`scripts/text_fetch_real_check.py`): it reads large local JSONL files by
path rather than a fixture, and is meant to be run by hand against a
one-off data pull, not on every `pytest` run. The JSONL inputs are **never**
committed to this repository (they're a full historical pull, would be
sizeable, and aren't needed once this issue's numbers are verified) --
point `--data-dir` at wherever you unpacked them.

Usage:

    .venv/bin/python scripts/verify_review_responsiveness.py --data-dir /path/to/scratchpad

Expects `<data-dir>/cl.jsonl`, `<data-dir>/cm.jsonl`, `<data-dir>/pr.jsonl`.

## Known, expected differences from `blend.py`'s numbers

1. **Bot list.** This harness uses `projects/cassandra.yaml`'s real,
   maintained `bot_patterns` (jira_username: `^svn-role$|^git-role$`;
   github_login: `-bot$|^dependabot`), not `blend.py`'s ad hoc hardcoded
   name list (`githubbot`, `hudson`, `jenkins`, ... plus "contains 'bot'" /
   "starts with 'github-'"). `blend.py`'s list is broader for JIRA
   (catches more usernames as bots) and for GitHub logins containing "bot"
   anywhere, not just the production regexes' narrower `-bot$`/`^dependabot`
   -- so a real human response `blend.py` miscounts as a bot (or vice
   versa) can shift a submission's first-response event.
2. **Comment cap.** `issue_comment` is capped at each issue's earliest
   `collectors.jira.MAX_COMMENTS_PER_ISSUE_STORED` (20) comments, matching
   what this project's real collector actually stores; `blend.py` read every
   comment `cm.jsonl` has. This harness applies the same 20-comment cap so
   the comparison is apples-to-apples with what production would actually
   see, and reports (via `comment_truncated`) how many issues in the sample
   have more than 20 comments.
3. **Right-censoring denominators.** `blend.py` age-gates its `resp<=7d`,
   `resp<=30d` and `none-ever` columns all off the *same* >=30-day-old
   cohort. This project's metric uses separate age gates per the issue's own
   spec (>=7 days old for the 7d share, >=30 days old for the 30d share and
   the no-visible-response share) -- so this harness's `within_7d_share`
   isn't directly comparable to a hand recomputation of `blend.py`'s
   `resp<=7d` column, but `within_30d_share` and `no_visible_response_share`
   use the identical >=30-day-old cohort and should track closely.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa

from project_health.collectors.github import _parse_gh_timestamp
from project_health.collectors.jira import (
    MAX_COMMENTS_PER_ISSUE_STORED,
    _parse_jira_timestamp,
)
from project_health.collectors.reviewer_trailer import extract_issue_keys
from project_health.config import load_project
from project_health.metrics.review_responsiveness import (
    TIER_2_5,
    TIER_6PLUS,
    TIER_FIRST,
    WINDOW_YEARLY,
    compute_review_responsiveness,
    metric_id,
)
from project_health.schema import get_schema, validate

REPO = "apache/cassandra"
SNAPSHOT_ID = "verify-review-responsiveness-harness"


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


def _load_jsonl(path: Path) -> list[dict]:
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def build_tables(data_dir: Path) -> tuple[dict[str, pa.Table], dict[str, int]]:
    cl_records = _load_jsonl(data_dir / "cl.jsonl")
    cm_by_key = {r["key"]: r for r in _load_jsonl(data_dir / "cm.jsonl")}
    pr_records = _load_jsonl(data_dir / "pr.jsonl")

    issue_rows: list[dict] = []
    changelog_rows: list[dict] = []
    comment_rows: list[dict] = []
    truncated_issues = 0

    for record in cl_records:
        issue_key = record["key"]
        created = _parse_jira_timestamp(record["created"])
        resolved = record.get("resolved")
        issue_rows.append(
            {
                "issue_key": issue_key,
                "summary": None,
                "status": record.get("status"),
                "status_category": None,
                "priority": None,
                "issue_type": record.get("type"),
                "created_at": created,
                # `updated` isn't in this reference pull's field list; not used
                # by review-responsiveness's own logic, so a safe stand-in.
                "updated_at": _parse_jira_timestamp(resolved) if resolved else created,
                "resolved_at": _parse_jira_timestamp(resolved) if resolved else None,
                "reporter_identity_id": None,
                "reporter_raw": record.get("reporter"),
                "assignee_identity_id": None,
                "assignee_raw": record.get("assignee"),
                "resolution": record.get("resolution"),
                "source_snapshot_id": SNAPSHOT_ID,
            }
        )
        for item in record.get("hist", []):
            if item["field"] not in ("status", "assignee"):
                continue
            changelog_rows.append(
                {
                    "event_id": str(uuid.uuid4()),
                    "issue_key": issue_key,
                    "changed_at": _parse_jira_timestamp(item["t"]),
                    "field": item["field"],
                    "from_value": item.get("from"),
                    "to_value": item.get("to"),
                    "actor_identity_id": None,
                    "actor_raw_type": "jira_username",
                    "actor_raw_value": item.get("by"),
                    "source_snapshot_id": SNAPSHOT_ID,
                }
            )

        comment_record = cm_by_key.get(issue_key)
        if comment_record:
            comments = comment_record.get("c", [])
            if len(comments) > MAX_COMMENTS_PER_ISSUE_STORED:
                truncated_issues += 1
            for index, comment in enumerate(comments[:MAX_COMMENTS_PER_ISSUE_STORED]):
                comment_rows.append(
                    {
                        "comment_id": f"{issue_key}-{index}",
                        "issue_key": issue_key,
                        "author_identity_id": None,
                        "author_raw_type": "jira_username",
                        "author_raw_value": comment.get("by"),
                        "created_at": _parse_jira_timestamp(comment["t"]),
                        "source_snapshot_id": SNAPSHOT_ID,
                    }
                )

    pr_rows: list[dict] = []
    pr_review_rows: list[dict] = []
    pr_comment_rows: list[dict] = []
    for record in pr_records:
        number = record["number"]
        title = record.get("title") or ""
        author = (record.get("author") or {}).get("login")
        merged_at = record.get("mergedAt")
        closed_at = record.get("closedAt")
        pr_rows.append(
            {
                "repo": REPO,
                "number": number,
                "state": record.get("state", "CLOSED"),
                "is_draft": False,
                "merged": merged_at is not None,
                "author_identity_id": None,
                "author_raw_type": "github_login",
                "author_raw_value": author,
                "title_hash": "",
                "linked_issue_keys": list(extract_issue_keys(title)),
                "created_at": _parse_gh_timestamp(record["createdAt"]),
                "updated_at": _parse_gh_timestamp(record["createdAt"]),
                "closed_at": _parse_gh_timestamp(closed_at) if closed_at else None,
                "merged_at": _parse_gh_timestamp(merged_at) if merged_at else None,
                "additions": None,
                "deletions": None,
                "changed_files": None,
                "source_snapshot_id": SNAPSHOT_ID,
            }
        )
        for review in (record.get("reviews") or {}).get("nodes", []):
            submitted_at = review.get("submittedAt")
            if not submitted_at:
                continue
            reviewer = (review.get("author") or {}).get("login")
            pr_review_rows.append(
                {
                    "review_id": str(uuid.uuid4()),
                    "repo": REPO,
                    "pr_number": number,
                    "reviewer_identity_id": None,
                    "reviewer_raw_type": "github_login",
                    "reviewer_raw_value": reviewer,
                    "state": "COMMENTED",
                    "submitted_at": _parse_gh_timestamp(submitted_at),
                    "source_snapshot_id": SNAPSHOT_ID,
                }
            )
        for comment in (record.get("comments") or {}).get("nodes", []):
            created_at = comment.get("createdAt")
            if not created_at:
                continue
            commenter = (comment.get("author") or {}).get("login")
            pr_comment_rows.append(
                {
                    "comment_id": str(uuid.uuid4()),
                    "repo": REPO,
                    "pr_number": number,
                    "review_id": None,
                    "comment_type": "issue_comment",
                    "author_identity_id": None,
                    "author_raw_type": "github_login",
                    "author_raw_value": commenter,
                    "created_at": _parse_gh_timestamp(created_at),
                    "source_snapshot_id": SNAPSHOT_ID,
                }
            )

    tables = {
        "issue": validate("issue", _rows_to_table(issue_rows, get_schema("issue"))),
        "jira_changelog": validate(
            "jira_changelog", _rows_to_table(changelog_rows, get_schema("jira_changelog"))
        ),
        "issue_comment": validate(
            "issue_comment", _rows_to_table(comment_rows, get_schema("issue_comment"))
        ),
        "pr": validate("pr", _rows_to_table(pr_rows, get_schema("pr"))),
        "pr_review": validate("pr_review", _rows_to_table(pr_review_rows, get_schema("pr_review"))),
        "pr_comment": validate(
            "pr_comment", _rows_to_table(pr_comment_rows, get_schema("pr_comment"))
        ),
    }
    stats = {
        "n_issues": len(issue_rows),
        "n_changelog_rows": len(changelog_rows),
        "n_comment_rows": len(comment_rows),
        "n_prs": len(pr_rows),
        "comment_truncated_issues": truncated_issues,
    }
    return tables, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", required=True, type=Path, help="Directory with cl.jsonl/cm.jsonl/pr.jsonl"
    )
    parser.add_argument(
        "--project-config",
        default=Path(__file__).resolve().parents[1] / "projects" / "cassandra.yaml",
        type=Path,
        help="ProjectConfig YAML to source bot_patterns from (default: projects/cassandra.yaml)",
    )
    parser.add_argument(
        "--as-of",
        default="2026-09-28",
        help="as_of date (YYYY-MM-DD) -- default matches the reference pull's own 'NOW'.",
    )
    args = parser.parse_args()

    config = load_project(args.project_config)
    tables, stats = build_tables(args.data_dir)
    print(f"Loaded: {stats}")

    as_of = date.fromisoformat(args.as_of)
    run_id = "harness-run"
    computed_at = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)

    result = compute_review_responsiveness(
        tables, as_of=as_of, run_id=run_id, computed_at=computed_at, config=config
    )
    by_key = {
        (row["metric_id"], row["window_start"].isoformat()): row for row in result.to_pylist()
    }

    tier_labels = {TIER_FIRST: "first", TIER_2_5: "2-5", TIER_6PLUS: "6+"}
    for tier in (TIER_FIRST, TIER_2_5, TIER_6PLUS):
        print(f"\n== {tier_labels[tier]} tier (yearly) ==")
        print(f"{'year':>6} {'n':>5} {'median_days':>12} {'<=30d_share':>12} {'no_visible':>11}")
        for year in range(2020, 2026):
            window_start = date(year, 1, 1).isoformat()
            median_row = by_key.get(
                (metric_id("review_first_response_median_days", WINDOW_YEARLY, tier), window_start)
            )
            share30_row = by_key.get(
                (metric_id("review_response_within_30d_share", WINDOW_YEARLY, tier), window_start)
            )
            novis_row = by_key.get(
                (metric_id("review_no_visible_response_share", WINDOW_YEARLY, tier), window_start)
            )
            n = median_row["n"] if median_row else "-"
            median_val = (
                f"{median_row['value']:.1f}"
                if median_row and median_row["value"] is not None
                else "-"
            )
            share30 = (
                f"{share30_row['value'] * 100:.0f}%"
                if share30_row and share30_row["value"] is not None
                else "-"
            )
            novis = (
                f"{novis_row['value'] * 100:.0f}%"
                if novis_row and novis_row["value"] is not None
                else "-"
            )
            print(f"{year:>6} {n!s:>5} {median_val:>12} {share30:>12} {novis:>11}")

    print("\n== first_patch_submissions (yearly, first-tier count) ==")
    for year in range(2020, 2026):
        window_start = date(year, 1, 1).isoformat()
        row = by_key.get((metric_id("first_patch_submissions", WINDOW_YEARLY, None), window_start))
        value = int(row["value"]) if row and row["value"] is not None else "-"
        print(f"{year}: {value}")


if __name__ == "__main__":
    main()
