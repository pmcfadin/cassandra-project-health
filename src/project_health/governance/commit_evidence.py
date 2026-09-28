"""Per-commit CI evidence/artefact facts for the commit-history table
(issue #97, orchestrator review of 127bd5a).

`build_commit_evidence_rows` is the counterpart to `governance/engine.py`'s
`build_commit_compliance_rows` for exactly two columns -- CI evidence and CI
artefacts -- computed directly from raw JIRA evidence via
`checks.describe_ci_evidence`/`checks.describe_ci_artefacts`, **never** from
a scored `commit_compliance` row. The orchestrator's review found the table
still reading the scored row for these two columns: a commit's `result` is
`exempt`/`not_in_force` for policy reasons (a rule's `effective_from`, the
commit-then-review/release-process exemptions) that have nothing to do with
whether the underlying JIRA evidence exists, so that path leaked policy
vocabulary into the cells ("not applicable (before 2026-08-19)") and, worse,
hid real evidence: a pre-2026-08-19 commit whose ticket's attachments *had*
been fetched still showed "not applicable" instead of what was actually
attached.

This module fixes both: every row is either `not_checked` (the ticket's
JIRA evidence -- comments and attachments, fetched together in one pass --
has never been fetched), `no_ticket` (no issue key referenced at all), or a
plain description of whatever the fetched evidence shows, regardless of its
date relative to any policy's effective date.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from project_health.governance.checks import (
    AttachmentEvidence,
    CIEvidence,
    CommitFacts,
    describe_ci_artefacts,
    describe_ci_evidence,
    has_ticket_evidence_been_checked,
)

_NO_TICKET_CI_EVIDENCE = {
    "bucket": "no_ticket",
    "text": "no ticket referenced",
    "url": None,
    "evidence_at": None,
    "lead_time_seconds": None,
}
_NO_TICKET_CI_ARTEFACTS = {
    "bucket": "no_ticket",
    "text": "no ticket referenced",
    "evidence_at": None,
}
_NOT_CHECKED_CI_EVIDENCE = {
    "bucket": "not_checked",
    "text": "not checked",
    "url": None,
    "evidence_at": None,
    "lead_time_seconds": None,
}
_NOT_CHECKED_CI_ARTEFACTS = {"bucket": "not_checked", "text": "not checked", "evidence_at": None}


def build_commit_evidence_rows(
    commits: Sequence[CommitFacts],
    *,
    ci_evidence_by_issue: dict[str, list[CIEvidence]] | None = None,
    attachments_by_issue: dict[str, list[AttachmentEvidence]] | None = None,
    fetched_attachment_issue_keys: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """One row per commit -- shaped exactly like `schema/tables.py`
    `COMMIT_EVIDENCE`'s columns."""
    ci_evidence_by_issue = ci_evidence_by_issue or {}
    attachments_by_issue = attachments_by_issue or {}

    rows: list[dict[str, Any]] = []
    for commit in commits:
        if not commit.issue_keys:
            ci_evidence = _NO_TICKET_CI_EVIDENCE
            ci_artefacts = _NO_TICKET_CI_ARTEFACTS
        elif not has_ticket_evidence_been_checked(
            commit.issue_keys, fetched_attachment_issue_keys
        ):
            ci_evidence = _NOT_CHECKED_CI_EVIDENCE
            ci_artefacts = _NOT_CHECKED_CI_ARTEFACTS
        else:
            evidence = [
                item
                for key in commit.issue_keys
                for item in ci_evidence_by_issue.get(key, [])
            ]
            ci_evidence = describe_ci_evidence(commit.commit_date, evidence)
            attachments = [
                a for key in commit.issue_keys for a in attachments_by_issue.get(key, [])
            ]
            ci_artefacts = describe_ci_artefacts(commit.commit_date, attachments)

        rows.append(
            {
                "sha": commit.sha,
                "ci_evidence_bucket": ci_evidence["bucket"],
                "ci_evidence_text": ci_evidence["text"],
                "ci_evidence_url": ci_evidence["url"],
                "ci_evidence_at": ci_evidence["evidence_at"],
                "ci_evidence_lead_time_seconds": ci_evidence["lead_time_seconds"],
                "ci_artefacts_bucket": ci_artefacts["bucket"],
                "ci_artefacts_text": ci_artefacts["text"],
                "ci_artefacts_at": ci_artefacts["evidence_at"],
            }
        )
    return rows
