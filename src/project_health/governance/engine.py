"""Orchestration: commits + policy + evidence -> `commit_compliance` rows
(issue #36).

`score_commit` runs every scored check against one commit and returns
whatever `CheckResult`s apply (a check that doesn't apply to the commit's
branch contributes no result at all, per `checks.py`'s scoring order).
`build_commit_compliance_rows` runs that over a whole commit list, joins in
the cross-referenced evidence (JIRA reviewer fields keyed by issue key,
JIRA-comment CI evidence keyed by issue key, GitHub check-runs keyed by
sha), applies `governance_overrides.yaml` corrections last (D15: "a
correction is a PR-reviewed override with the reason recorded"), and
returns plain dicts shaped exactly like the `commit_compliance` schema
(`schema/tables.py`) — ready for `pa.Table.from_pylist`.

Per docs/spec/GOVERNANCE.md §3 ("Per-commit checks (R1-R5) must run against
every commit, merge or not... Reserve `--no-merges` for *aggregate*
rate/denominator calculations only"), this module never filters merge
commits out of `commits` itself — that's the caller's job for the *raw
commit list* it builds via `collectors/governance_git.py` (which
deliberately walks every commit, `--no-merges` not applied); this module
just scores whatever it's given. `metrics.py`'s aggregate-rate computation
is what excludes merges from its denominators.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from project_health.governance.checks import (
    CIEvidence,
    CheckResult,
    CheckstyleEvidence,
    CODE_STYLE_CHECKSTYLE,
    CommitFacts,
    JIRA_TICKET_REFERENCED,
    PRE_COMMIT_CI_EVIDENCE,
    REVIEWER_PRESENT,
    build_commit_facts_row,
    score_code_style_checkstyle,
    score_jira_ticket_referenced,
    score_pre_commit_ci_evidence,
    score_reviewer_present,
)
from project_health.governance.overrides import Override, index_overrides
from project_health.governance.policy import Policy

SCORED_CHECK_IDS: tuple[str, ...] = (
    REVIEWER_PRESENT,
    JIRA_TICKET_REFERENCED,
    PRE_COMMIT_CI_EVIDENCE,
    CODE_STYLE_CHECKSTYLE,
)


def score_commit(
    policy: Policy,
    commit: CommitFacts,
    *,
    jira_reviewers: tuple[str, ...] = (),
    ci_evidence_by_issue: dict[str, CIEvidence] | None = None,
    checkstyle_runs: tuple[CheckstyleEvidence, ...] = (),
    checkstyle_retention_cutoff: datetime | None = None,
) -> list[CheckResult]:
    """Every scored check's result for one commit (omitting checks that
    don't apply to this commit's branch). `checkstyle_retention_cutoff`
    (issue #36 fixup cycle 2) is passed straight through to
    `score_code_style_checkstyle` — see that function's docstring."""
    results: list[CheckResult] = []

    reviewer_result = score_reviewer_present(
        policy.rule(REVIEWER_PRESENT), commit, jira_reviewers
    )
    if reviewer_result is not None:
        results.append(reviewer_result)

    jira_result = score_jira_ticket_referenced(policy.rule(JIRA_TICKET_REFERENCED), commit)
    if jira_result is not None:
        results.append(jira_result)

    ci_result = score_pre_commit_ci_evidence(
        policy.rule(PRE_COMMIT_CI_EVIDENCE), commit, ci_evidence_by_issue
    )
    if ci_result is not None:
        results.append(ci_result)

    checkstyle_result = score_code_style_checkstyle(
        policy.rule(CODE_STYLE_CHECKSTYLE),
        commit,
        checkstyle_runs,
        retention_cutoff=checkstyle_retention_cutoff,
    )
    if checkstyle_result is not None:
        results.append(checkstyle_result)

    return results


def _union_reviewers(commit: CommitFacts, jira_reviewers: tuple[str, ...]) -> tuple[str, ...]:
    """Trailer reviewers, then JIRA-field reviewers, de-duplicated,
    order-preserved (D15: the `reviewers` column shown alongside every
    commit row is the full set of named reviewers found by *either*
    evidence source, not just the one the passing check happened to use)."""
    seen: dict[str, None] = {}
    for name in (*commit.trailer_reviewers, *jira_reviewers):
        if name not in seen:
            seen[name] = None
    return tuple(seen)


def _apply_override(row: dict, override: Override | None) -> dict:
    if override is None:
        return row
    row = dict(row)
    row["evidence"] = (
        f"OVERRIDDEN by {override.reviewer}: {override.reason} "
        f"(original result: {row['result']!r}, original evidence: {row['evidence']!r})"
    )
    row["result"] = override.result
    return row


def build_commit_compliance_rows(
    policy: Policy,
    commits: Sequence[CommitFacts],
    *,
    jira_reviewers_by_issue: dict[str, tuple[str, ...]] | None = None,
    ci_evidence_by_issue: dict[str, CIEvidence] | None = None,
    checkstyle_runs_by_sha: dict[str, tuple[CheckstyleEvidence, ...]] | None = None,
    checkstyle_retention_cutoff: datetime | None = None,
    overrides: Sequence[Override] | None = None,
) -> list[dict]:
    """Score every commit in `commits` against every rule in `policy`.

    Returns one dict per (commit, scored check_id) that applies — shaped
    exactly like `schema/tables.py` `COMMIT_COMPLIANCE`'s columns. Overrides
    (`governance_overrides.yaml`) are looked up by `(sha, check_id)` and
    applied last, per-row, after all real scoring. `checkstyle_retention_cutoff`
    (issue #36 fixup cycle 2) is passed straight through to
    `score_commit`/`score_code_style_checkstyle`.
    """
    jira_reviewers_by_issue = jira_reviewers_by_issue or {}
    ci_evidence_by_issue = ci_evidence_by_issue or {}
    checkstyle_runs_by_sha = checkstyle_runs_by_sha or {}
    overrides_index = index_overrides(list(overrides) if overrides else [])

    rows: list[dict] = []
    for commit in commits:
        jira_reviewers: tuple[str, ...] = tuple(
            dict.fromkeys(
                name
                for issue_key in commit.issue_keys
                for name in jira_reviewers_by_issue.get(issue_key, ())
            )
        )
        checkstyle_runs = checkstyle_runs_by_sha.get(commit.sha, ())
        reviewers = _union_reviewers(commit, jira_reviewers)

        for result in score_commit(
            policy,
            commit,
            jira_reviewers=jira_reviewers,
            ci_evidence_by_issue=ci_evidence_by_issue,
            checkstyle_runs=checkstyle_runs,
            checkstyle_retention_cutoff=checkstyle_retention_cutoff,
        ):
            row = {
                "sha": commit.sha,
                "branch": commit.branch,
                "commit_date": commit.commit_date,
                "author": commit.author,
                "committer": commit.committer,
                "is_merge": commit.is_merge,
                "reviewers": list(reviewers),
                "jira_keys": list(commit.issue_keys),
                "policy_version": policy.version,
                "check_id": result.check_id,
                "result": result.result,
                "evidence": result.evidence,
                "evidence_url": result.evidence_url,
            }
            override = overrides_index.get((commit.sha, result.check_id))
            rows.append(_apply_override(row, override))

    return rows


def build_commit_facts_rows(commits: Sequence[CommitFacts]) -> list[dict]:
    """The `scored: false` display facts (CHANGES.txt/NEWS.txt/test/ touched)
    for every commit, one dict per commit, shaped like `schema/tables.py`
    `COMMIT_FACT`'s columns."""
    rows = []
    for commit in commits:
        fact = build_commit_facts_row(commit)
        rows.append(
            {
                "sha": fact.sha,
                "branch": fact.branch,
                "commit_date": fact.commit_date,
                "changes_txt_touched": fact.changes_txt_touched,
                "news_txt_touched": fact.news_txt_touched,
                "test_touched": fact.test_touched,
            }
        )
    return rows
