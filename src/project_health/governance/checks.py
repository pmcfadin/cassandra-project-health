"""Per-check scoring functions (issue #36).

Each `score_*` function is a pure function: `Rule` (the policy's typed view
of one `governance-policy.yaml` rule, `policy.py`) plus this commit's
already-collected evidence in, one `CheckResult` (or `None`, meaning "this
rule doesn't apply to this commit's branch at all — emit no row") out. No
I/O happens here; every evidence source is collected upstream
(`collectors/governance_git.py`, `collectors/jira_comments.py`,
`collectors/github_checks.py`) and passed in already-shaped.

Scoring order, identical across every scored rule (D14/D15):

1. Does the rule apply to this commit's branch at all (`Rule.applies_to_branch`)?
   If not, return `None` — no row, not even `not_in_force`.
2. Is the rule in force on this commit's date (`Rule.in_force_on`)? If not,
   `not_in_force`.
3. Does an exemption match (ninja / release-housekeeping)? If so, `exempt` —
   unconditionally, regardless of any other evidence.
4. Rule-specific pass/fail/unknown logic.

`reviewer-present` is the one rule with a `fail_allowed: true` guarded by the
`review_wording_check` (D15's false-fail protection, docs/spec/GOVERNANCE.md
§8): `fail` requires *all* of (a) an issue key referenced, (b) no reviewer
found by either evidence source, (c) no exemption matched, (d) no review
wording anywhere in the message. `code-style-checkstyle` is the other
`fail_allowed: true` rule, but its fail condition is a direct, unambiguous
red check-run — no wording guard needed (governance-policy.yaml comment:
"this is direct, unambiguous evidence, fail is allowed").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from project_health.governance.policy import Rule

# Rule ids, matching governance-policy.yaml `rules[].id` exactly (used as
# `commit_compliance.check_id`).
REVIEWER_PRESENT = "reviewer-present"
JIRA_TICKET_REFERENCED = "jira-ticket-referenced"
PRE_COMMIT_CI_EVIDENCE = "pre-commit-ci-evidence"
CODE_STYLE_CHECKSTYLE = "code-style-checkstyle"

# `reviewer-present`'s fail condition is anchored specifically to "the
# commit references a CASSANDRA-N issue key" (governance-policy.yaml
# description and result_semantics.fail; docs/spec/GOVERNANCE.md §2 R1) —
# not any project-prefixed key. `commit.issue_keys` (populated from
# `reviewer_trailer.extract_issue_keys`) is intentionally generic, since it
# also feeds `jira-ticket-referenced` (whose own check_method explicitly
# reuses that same generic regex, and which has no fail state to protect)
# and the `commit_compliance.jira_keys` display column (D15: show every key
# actually referenced, CEP included). Found live (issue #36 trunk-since-2023
# acceptance run): two real trunk commits reference only a `CEP-N` key
# ("CEP-7", "CEP-15", e.g. sha `3e38b3d641c076f45ebc108f68218529766c7492`)
# with no reviewer and no exemption match — without this filter they were
# incorrectly scored `fail` (a CEP reference "looks like" an issue key to
# the generic regex, but is not the "CASSANDRA-N" the fail condition
# names), when the policy's own wording puts them in `unknown` case (a)
# instead ("no CASSANDRA-N key referenced and no reviewer found").
_CASSANDRA_ISSUE_KEY_RE = re.compile(r"^CASSANDRA-\d+$")


def _has_cassandra_issue_key(issue_keys: tuple[str, ...]) -> bool:
    return any(_CASSANDRA_ISSUE_KEY_RE.match(key) for key in issue_keys)

# `code-style-checkstyle`'s GitHub Actions check-run names
# (`.github/workflows/code-check.yaml`, governance-policy.yaml check_method).
CHECKSTYLE_RUN_NAMES = frozenset({"ant-check-jdk11", "ant-check-jdk17"})

# GitHub Checks API conclusions that count as a definitive failure (vs.
# `None`/`in_progress`/`queued`, which are "not concluded yet", scored
# `unknown`, never `fail`).
_FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required"})


@dataclass(frozen=True)
class CommitFacts:
    """One commit's collected, evidence-agnostic facts — everything the
    scoring functions need that isn't a separate evidence lookup."""

    sha: str
    branch: str
    commit_date: datetime  # UTC-aware
    message: str
    author: str
    committer: str
    is_merge: bool
    trailer_reviewers: tuple[str, ...] = ()
    issue_keys: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CIEvidence:
    """One JIRA-comment CI-evidence match for an issue key
    (`collectors/jira_comments.py`) — comment metadata plus the matched CI
    URL only, never the comment body (issue #36 scope: "store comment
    metadata plus the matched CI URL only, never comment bodies")."""

    issue_key: str
    comment_id: str
    comment_author: str | None
    comment_created_at: str | None
    matched_term: str
    matched_url: str | None = None


@dataclass(frozen=True)
class CheckstyleEvidence:
    """One GitHub check-run relevant to `code-style-checkstyle`
    (`collectors/github_checks.py`)."""

    sha: str
    check_run_name: str
    conclusion: str | None  # 'success' | 'failure' | 'neutral' | ... | None (not concluded)
    html_url: str | None = None


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    result: str
    evidence: str
    evidence_url: str | None = None


def _not_in_force(rule: Rule, commit: CommitFacts) -> CheckResult:
    return CheckResult(
        check_id=rule.id,
        result="not_in_force",
        evidence=(
            f"commit date {commit.commit_date.isoformat()} is before rule effective_from "
            f"{rule.effective_from.isoformat() if rule.effective_from else '?'}"
        ),
    )


def score_reviewer_present(
    rule: Rule, commit: CommitFacts, jira_reviewers: tuple[str, ...] = ()
) -> CheckResult | None:
    """`reviewer-present` (governance-policy.yaml, D15 §8).

    `jira_reviewers` is the union of reviewer names found on any of
    `commit.issue_keys` via the JIRA Reviewers/Reviewer custom fields
    (already-collected `review_event` rows with `source == 'jira_field'`) —
    a second, independent evidence source alongside the commit trailer.
    """
    if not rule.applies_to_branch(commit.branch):
        return None
    if not rule.in_force_on(commit.commit_date.date()):
        return _not_in_force(rule, commit)

    exemption = rule.matching_exemption(commit.message)
    if exemption is not None:
        return CheckResult(rule.id, "exempt", f"matched exemption: {exemption.id}")

    trailer_found = bool(commit.trailer_reviewers)
    jira_found = bool(jira_reviewers)
    if trailer_found or jira_found:
        parts = []
        if trailer_found:
            parts.append(f"commit trailer reviewer(s): {', '.join(commit.trailer_reviewers)}")
        if jira_found:
            parts.append(f"JIRA reviewer field: {', '.join(jira_reviewers)}")
        return CheckResult(rule.id, "pass", "; ".join(parts))

    has_issue_key = _has_cassandra_issue_key(commit.issue_keys)
    review_wording = rule.review_wording_present(commit.message)
    if has_issue_key and not review_wording:
        return CheckResult(
            rule.id,
            "fail",
            "CASSANDRA-N key referenced; no reviewer found in commit trailer or JIRA reviewer "
            "field(s); no exemption matched; no review wording present",
        )
    if review_wording:
        return CheckResult(rule.id, "unknown", "review text present but unparsed")
    return CheckResult(rule.id, "unknown", "no issue key referenced and no reviewer found")


def score_jira_ticket_referenced(rule: Rule, commit: CommitFacts) -> CheckResult | None:
    """`jira-ticket-referenced` — no fail state (`fail_allowed: false`)."""
    if not rule.applies_to_branch(commit.branch):
        return None
    if not rule.in_force_on(commit.commit_date.date()):
        return _not_in_force(rule, commit)

    exemption = rule.matching_exemption(commit.message)
    if exemption is not None:
        return CheckResult(rule.id, "exempt", f"matched exemption: {exemption.id}")

    if commit.issue_keys:
        return CheckResult(rule.id, "pass", f"issue key(s) found: {', '.join(commit.issue_keys)}")
    return CheckResult(rule.id, "unknown", "no issue key found and no exemption matched")


def score_pre_commit_ci_evidence(
    rule: Rule, commit: CommitFacts, ci_evidence_by_issue: dict[str, CIEvidence] | None = None
) -> CheckResult | None:
    """`pre-commit-ci-evidence` — pass/unknown only (`fail_allowed: false`
    in v1; issue #36 live-tests whether a v2 could add `fail_allowed` via
    the ci-cassandra.apache.org Jenkins API, see `jenkins_probe.py` — this
    function only ever implements the *shipped* v1 scoring, never the
    unverified Jenkins path).

    Only the JIRA-comment CI-evidence source is used for scoring — the
    GitHub-check-runs evidence source documented in the policy is
    explicitly "NOT a substitute" for this rule (it measures the GitHub
    Actions checkstyle surface, not pre-commit Jenkins), so it is never
    consulted here.
    """
    if not rule.applies_to_branch(commit.branch):
        return None
    if not rule.in_force_on(commit.commit_date.date()):
        return _not_in_force(rule, commit)

    ci_evidence_by_issue = ci_evidence_by_issue or {}
    for issue_key in commit.issue_keys:
        evidence = ci_evidence_by_issue.get(issue_key)
        if evidence is not None:
            return CheckResult(
                rule.id,
                "pass",
                f"JIRA comment {evidence.comment_id} on {issue_key} matched CI term "
                f"{evidence.matched_term!r}",
                evidence.matched_url,
            )

    if commit.issue_keys:
        return CheckResult(
            rule.id,
            "unknown",
            f"no JIRA-comment CI evidence found on {', '.join(commit.issue_keys)}",
        )
    return CheckResult(
        rule.id, "unknown", "no issue key referenced; cannot check JIRA comments for CI evidence"
    )


def score_code_style_checkstyle(
    rule: Rule,
    commit: CommitFacts,
    checkstyle_runs: tuple[CheckstyleEvidence, ...] = (),
    *,
    retention_cutoff: datetime | None = None,
) -> CheckResult | None:
    """`code-style-checkstyle` — `fail_allowed: true`; a red check-run is
    direct evidence, no wording guard needed (unlike `reviewer-present`).

    `retention_cutoff` (issue #36 fixup cycle 2), when given, is the oldest
    commit date the collector was willing to even *attempt* a GitHub
    check-runs fetch for this run (`pipeline.py`'s
    `_governance_checkstyle_retention_days`) — a commit older than that was
    never fetched at all (docs/spec/GOVERNANCE.md §10's live finding: GitHub
    Actions check-run history has almost certainly already rolled off for
    it), so an empty `checkstyle_runs` there means "structurally
    unknowable", not "not yet checked", and gets its own, more informative
    evidence string rather than being indistinguishable from a commit that
    might still resolve on a later run.
    """
    if not rule.applies_to_branch(commit.branch):
        return None
    if not rule.in_force_on(commit.commit_date.date()):
        return _not_in_force(rule, commit)

    if not checkstyle_runs:
        if retention_cutoff is not None and commit.commit_date < retention_cutoff:
            return CheckResult(
                rule.id, "unknown", "check-run history outside GitHub retention"
            )
        return CheckResult(rule.id, "unknown", "no GitHub check-run recorded for this commit sha")

    failing = [r for r in checkstyle_runs if r.conclusion in _FAILING_CONCLUSIONS]
    if failing:
        run = failing[0]
        return CheckResult(
            rule.id,
            "fail",
            f"check-run {run.check_run_name!r} concluded {run.conclusion!r}",
            run.html_url,
        )

    succeeded = [r for r in checkstyle_runs if r.conclusion == "success"]
    if succeeded and len(succeeded) == len(checkstyle_runs):
        run = succeeded[0]
        names = ", ".join(sorted({r.check_run_name for r in checkstyle_runs}))
        return CheckResult(rule.id, "pass", f"check-run(s) {names} all succeeded", run.html_url)

    conclusions = sorted({str(r.conclusion) for r in checkstyle_runs})
    return CheckResult(
        rule.id,
        "unknown",
        f"check-run(s) present but not concluded success/failure: {conclusions}",
    )


# --- Unscored facts (changes-txt-entry, news-txt-entry, test-touched) -------
#
# governance-policy.yaml sets all three to `scored: false` — "displayed as a
# fact on the commit row, never evaluated as a rule". These never produce a
# `commit_compliance` row (no check_id/result/evidence to score); instead
# they're plain per-commit facts, kept in a separate small structure so the
# distinction between "a check result" and "a displayed fact" stays visible
# in the data model, not just in a docstring.


@dataclass(frozen=True)
class CommitFactsRow:
    sha: str
    branch: str
    commit_date: datetime
    changes_txt_touched: bool
    news_txt_touched: bool
    test_touched: bool


def build_commit_facts_row(commit: CommitFacts) -> CommitFactsRow:
    """The three `scored: false` facts (governance-policy.yaml
    `changes-txt-entry`, `news-txt-entry`, `test-touched`), derived from
    `commit.changed_paths` (`git show --name-only`, per each rule's
    `check_method`). `changed_paths=None` (not collected) reads as "no"
    for all three rather than raising -- these are advisory display facts,
    never a scored result.
    """
    paths = commit.changed_paths or ()
    return CommitFactsRow(
        sha=commit.sha,
        branch=commit.branch,
        commit_date=commit.commit_date,
        changes_txt_touched="CHANGES.txt" in paths,
        news_txt_touched="NEWS.txt" in paths,
        test_touched=any(p == "test" or p.startswith("test/") for p in paths),
    )
