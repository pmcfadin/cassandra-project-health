"""Fact-based monthly governance trend metrics (issue #97, D25 amendment).

Orchestrator review of #97 found the original governance trend cards still
policy-derived: `governance/metrics.py`'s pass-rate metrics exclude `exempt`
commits from the denominator and blank every month before a rule's
`effective_from` as `not_in_force` -- so a card labeled "Commits with a
named reviewer" was actually showing a *verdict* rate (every month before
2020 reads `insufficient_data`, even though the raw fact -- was a reviewer
named -- is knowable back to the start of the repository).

This module computes five metrics directly from the same raw evidence
`governance/checks.py`'s policy-free fact helpers expose
(`ci_evidence_at_or_before`, `both_ci_artefacts_at_or_before`,
`checkstyle_all_succeeded`, `has_cassandra_issue_key`) -- never from a scored
`commit_compliance` row, and never gated by a rule's `effective_from` or any
exemption. Denominators:

- `governance_commits_with_named_reviewer_share`,
  `governance_commits_with_ticket_share`: every non-merge trunk commit that
  month.
- `governance_commits_with_ci_evidence_before_commit_share`,
  `governance_commits_with_both_ci_artefacts_share`: non-merge trunk commits
  that reference a CASSANDRA ticket *and* whose ticket's JIRA evidence has
  actually been checked (`fetched_issue_keys`) -- a ticket not yet checked
  (backfill) is excluded from the rate, not counted as a miss, and reported
  separately as `n_not_checked` in `details_json` so the backfill is never
  read as absence.
- `governance_commits_with_checkstyle_success_share`: non-merge trunk
  commits with at least one recorded check-run.

`direction_of_good` is deliberately not modeled here -- callers
(`metrics_meta.py`) set it to `None` (neutral) for all five, per D25: these
are descriptive facts, not a rate this project judges "higher is better".
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime

import pyarrow as pa

from project_health.governance.checks import (
    AttachmentEvidence,
    CheckstyleEvidence,
    CIEvidence,
    CommitFacts,
    both_ci_artefacts_at_or_before,
    checkstyle_all_succeeded,
    ci_evidence_at_or_before,
    has_cassandra_issue_key,
)
from project_health.metrics.windows import is_completed_month, month_end
from project_health.schema import get_schema, validate

DEFINITION_VERSION = "1.0"

NAMED_REVIEWER = "governance_commits_with_named_reviewer_share"
TICKET_REFERENCED = "governance_commits_with_ticket_share"
CI_EVIDENCE_BEFORE_COMMIT = "governance_commits_with_ci_evidence_before_commit_share"
BOTH_CI_ARTEFACTS = "governance_commits_with_both_ci_artefacts_share"
CHECKSTYLE_SUCCESS = "governance_commits_with_checkstyle_success_share"

FACT_METRIC_IDS: tuple[str, ...] = (
    NAMED_REVIEWER,
    TICKET_REFERENCED,
    CI_EVIDENCE_BEFORE_COMMIT,
    BOTH_CI_ARTEFACTS,
    CHECKSTYLE_SUCCESS,
)


def _month_key(commit_date: datetime) -> date:
    return date(commit_date.year, commit_date.month, 1)


def compute_monthly_fact_metrics(
    commits: list[CommitFacts],
    *,
    jira_reviewers_by_issue: dict[str, tuple[str, ...]],
    ci_evidence_by_issue: dict[str, list[CIEvidence]],
    attachments_by_issue: dict[str, list[AttachmentEvidence]],
    fetched_attachment_issue_keys: frozenset[str] | set[str],
    checkstyle_runs_by_sha: dict[str, tuple[CheckstyleEvidence, ...]],
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> pa.Table:
    """One `metric_value` row per `(metric_id, completed month)`, computed
    directly from raw evidence -- trunk, non-merge commits only (same
    forward-merge exclusion the commit-history table applies by default).
    Returns an empty (but schema-valid) `metric_value` table if `commits` is
    empty.
    """
    # Per-month accumulators: reviewer/ticket count over ALL trunk non-merge
    # commits; ci_evidence/ci_artefacts count only over commits whose ticket
    # was actually checked; checkstyle count only over commits with a
    # recorded run.
    reviewer_total: dict[date, int] = defaultdict(int)
    reviewer_hit: dict[date, int] = defaultdict(int)
    ticket_total: dict[date, int] = defaultdict(int)
    ticket_hit: dict[date, int] = defaultdict(int)
    ci_checked: dict[date, int] = defaultdict(int)
    ci_before: dict[date, int] = defaultdict(int)
    ci_not_checked: dict[date, int] = defaultdict(int)
    art_checked: dict[date, int] = defaultdict(int)
    art_both: dict[date, int] = defaultdict(int)
    art_not_checked: dict[date, int] = defaultdict(int)
    cs_with_run: dict[date, int] = defaultdict(int)
    cs_success: dict[date, int] = defaultdict(int)

    for commit in commits:
        if commit.is_merge or commit.branch != "trunk":
            continue
        month = _month_key(commit.commit_date)

        reviewer_total[month] += 1
        jira_reviewers = tuple(
            name
            for key in commit.issue_keys
            for name in jira_reviewers_by_issue.get(key, ())
        )
        if commit.trailer_reviewers or jira_reviewers:
            reviewer_hit[month] += 1

        ticket_total[month] += 1
        has_ticket = has_cassandra_issue_key(commit.issue_keys)
        if has_ticket:
            ticket_hit[month] += 1

            checked = any(key in fetched_attachment_issue_keys for key in commit.issue_keys)
            if checked:
                evidence = [
                    item
                    for key in commit.issue_keys
                    for item in ci_evidence_by_issue.get(key, [])
                ]
                ci_checked[month] += 1
                if ci_evidence_at_or_before(commit.commit_date, evidence):
                    ci_before[month] += 1

                attachments = [
                    a for key in commit.issue_keys for a in attachments_by_issue.get(key, [])
                ]
                art_checked[month] += 1
                if both_ci_artefacts_at_or_before(commit.commit_date, attachments):
                    art_both[month] += 1
            else:
                ci_not_checked[month] += 1
                art_not_checked[month] += 1

        runs = checkstyle_runs_by_sha.get(commit.sha, ())
        if runs:
            cs_with_run[month] += 1
            if checkstyle_all_succeeded(runs):
                cs_success[month] += 1

    def _rows(
        metric_id: str,
        hit: dict[date, int],
        total: dict[date, int],
        months: set[date],
        detail_key: str,
        extra: dict[date, dict[str, int]] | None = None,
    ) -> list[dict]:
        rows = []
        for month in sorted(months):
            if not is_completed_month(month, as_of):
                continue
            n = total.get(month, 0)
            h = hit.get(month, 0)
            value = (h / n) if n else None
            details = {detail_key: h, "n_total": n}
            if extra and month in extra:
                details.update(extra[month])
            rows.append(
                {
                    "metric_id": metric_id,
                    "definition_version": DEFINITION_VERSION,
                    "window_start": month,
                    "window_end": month_end(month),
                    "value": value,
                    "n": n,
                    "flag": "ok" if n else "insufficient_data",
                    "run_id": run_id,
                    "computed_at": computed_at,
                    "details_json": json.dumps(details, sort_keys=True),
                }
            )
        return rows

    # Every month with at least one non-merge trunk commit -- the master set
    # for reviewer/ticket (denominator is "all such commits").
    all_months = set(reviewer_total)
    # Every month with at least one CASSANDRA-ticketed commit -- the master
    # set for the two "checked" metrics, so a month where every ticketed
    # commit is still backfill-pending still gets a row (n=0, `n_not_checked`
    # > 0) rather than silently vanishing.
    ticket_months = {m for m, n in ticket_hit.items() if n}

    rows: list[dict] = []
    rows += _rows(NAMED_REVIEWER, reviewer_hit, reviewer_total, all_months, "n_with_named_reviewer")
    rows += _rows(TICKET_REFERENCED, ticket_hit, ticket_total, all_months, "n_with_ticket")
    rows += _rows(
        CI_EVIDENCE_BEFORE_COMMIT,
        ci_before,
        ci_checked,
        ticket_months,
        "n_before_commit",
        extra={m: {"n_not_checked": ci_not_checked.get(m, 0)} for m in ticket_months},
    )
    rows += _rows(
        BOTH_CI_ARTEFACTS,
        art_both,
        art_checked,
        ticket_months,
        "n_both_attached",
        extra={m: {"n_not_checked": art_not_checked.get(m, 0)} for m in ticket_months},
    )
    rows += _rows(CHECKSTYLE_SUCCESS, cs_success, cs_with_run, set(cs_with_run), "n_success")

    schema = get_schema("metric_value")
    table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    return validate("metric_value", table)
