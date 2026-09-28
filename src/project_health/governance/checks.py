"""Per-check scoring functions (issue #36; v2, D24, issue #93).

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
3. Does an exemption match (`commit-then-review` / `release-process`, v2)? If
   so, `exempt` — unconditionally, regardless of any other evidence. No
   check here special-cases any exemption id: exemptions are entirely
   policy-driven (`Rule.matching_exemption`, `policy.py`), which is what
   guarantees no code hard-codes the old, now-removed `ninja` exemption
   (D24, issue #93) — v2's `governance-policy.yaml` simply no longer lists
   it, so `matching_exemption` can never return it.
4. Rule-specific pass/fail/unknown logic.

`reviewer-present` is the one rule with a `fail_allowed: true` guarded by the
`review_wording_check` (D15's false-fail protection, docs/spec/GOVERNANCE.md
§8): `fail` requires *all* of (a) an issue key referenced, (b) no reviewer
found by either evidence source, (c) no exemption matched, (d) no review
wording anywhere in the message. `code-style-checkstyle` and (v2)
`ci-artefacts-attached` are the other `fail_allowed: true` rules; both have
direct, unambiguous fail evidence (a red check-run; a JIRA attachment list
that was actually fetched and is missing an artefact) so neither needs a
wording guard.

## v2 evidence changes (D24, issue #93)

- `pre-commit-ci-evidence` now scores from **both** JIRA-comment CI mentions
  and JIRA attachments (`ci_summary*`/`results_details*`), and requires the
  matching evidence to be dated **at or before the commit** — evidence dated
  only after the commit gets its own, distinct `unknown` evidence string
  rather than being indistinguishable from "no evidence found at all".
  MEASUREMENT (ours, D24): "at or before" compares two already-UTC-aware
  timestamps directly (`CommitFacts.commit_date` and `CIEvidence.created_at`)
  — no separate timezone normalization is needed because both this module's
  callers (`collectors/jira.py`'s `_parse_jira_timestamp`,
  `collectors/governance_git.py`) already produce UTC-aware datetimes.
- `ci-artefacts-attached` (new rule) requires **both** artefacts attached to
  the same referenced issue at or before the commit; it is `fail_allowed`
  because a JIRA issue's attachment list, once fetched, is complete — an
  absence there is real evidence, not a `pre-commit-ci-evidence`-style gap.
  It only ever fails an issue whose attachment list has actually been
  fetched this run or a prior one (`fetched_issue_keys`) — an unfetched
  issue is `unknown`, never `fail` (the JIRA attachment backfill is
  budgeted and incremental, `pipeline.py`).
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
CI_ARTEFACTS_ATTACHED = "ci-artefacts-attached"
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

# `ci-artefacts-attached.check_method` (governance-policy.yaml, v2, issue
# #93): the exact two filename patterns the official docs (patches.html,
# ci.html) name — `(?i)^ci_summary` and `(?i)^results_details`. Kept as code
# constants for the same reason `pre-commit-ci-evidence`'s CI_EVIDENCE_TERMS
# (`collectors/jira_comments.py`) is: the policy's `check_method.detail` is
# prose, not a structured list, and a filename-pattern change here is a
# scoring-behavior change that should be reviewed as one.
_CI_SUMMARY_RE = re.compile(r"(?i)^ci_summary")
_RESULTS_DETAILS_RE = re.compile(r"(?i)^results_details")


def is_ci_artefact_filename(filename: str) -> bool:
    """True if `filename` matches either of `ci-artefacts-attached`'s two
    artefact patterns (`ci_summary*` or `results_details*`). Used both by
    `score_ci_artefacts_attached` (which requires *both* patterns matched,
    each on its own attachment) and by `pipeline.py`'s evidence-building for
    `pre-commit-ci-evidence` (where *either* one, as an attachment, counts
    as CI evidence alongside a JIRA comment mention)."""
    return bool(_CI_SUMMARY_RE.match(filename) or _RESULTS_DETAILS_RE.match(filename))


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
    """One CI-evidence match for `pre-commit-ci-evidence` (v2, issue #93):
    either a JIRA-comment CI mention or a JIRA attachment matching
    `ci_summary*`/`results_details*` on one of the commit's referenced
    issues. Never carries the comment body or attachment content — only
    metadata (issue #36 scope, extended to attachments by issue #93: "never
    content").

    `source` is `'jira_comment_ci_mention'` or `'jira_attachment_ci_artefact'`
    (`governance-policy.yaml pre-commit-ci-evidence.check_method`'s two
    `evidence_source` values). `created_at` is this evidence's own UTC-aware
    timestamp (the comment's or attachment's `created` field, already
    parsed) — required so scoring can implement "dated at or before the
    commit" (D24).
    """

    issue_key: str
    source: str
    created_at: datetime
    description: str
    url: str | None = None


@dataclass(frozen=True)
class AttachmentEvidence:
    """One JIRA attachment's metadata (v2, issue #93) — filename, created
    timestamp and attachment id **only, never content** (issue scope: "never
    content"). Feeds `ci-artefacts-attached`'s scoring, and (filtered to
    `ci_summary*`/`results_details*`) `pre-commit-ci-evidence`'s attachment
    evidence source."""

    issue_key: str
    attachment_id: str
    filename: str
    created_at: datetime  # UTC-aware


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

    exemption = rule.matching_exemption(commit.message, commit.changed_paths)
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

    exemption = rule.matching_exemption(commit.message, commit.changed_paths)
    if exemption is not None:
        return CheckResult(rule.id, "exempt", f"matched exemption: {exemption.id}")

    if commit.issue_keys:
        return CheckResult(rule.id, "pass", f"issue key(s) found: {', '.join(commit.issue_keys)}")
    return CheckResult(rule.id, "unknown", "no issue key found and no exemption matched")


def score_pre_commit_ci_evidence(
    rule: Rule, commit: CommitFacts, ci_evidence_by_issue: dict[str, list[CIEvidence]] | None = None
) -> CheckResult | None:
    """`pre-commit-ci-evidence` (v2, D24, issue #93) — pass/unknown only
    (`fail_allowed: false`; CI results may legitimately be provided
    somewhere this check cannot see, so their absence is never read as a
    fail).

    `ci_evidence_by_issue` is `issue_key -> [CIEvidence, ...]`, covering both
    evidence sources the policy names (`jira_comment_ci_mention`,
    `jira_attachment_ci_artefact`; `collectors/jira_comments.py`). A match is
    only `pass` evidence when it is dated at or before the commit
    (`CIEvidence.created_at <= commit.commit_date`, both UTC-aware); a match
    that exists only *after* the commit is real evidence CI happened
    eventually, but not evidence it happened *before* commit, so it produces
    its own distinct `unknown` text rather than being folded into "no
    evidence found at all" (issue #93 scope item 2).
    """
    if not rule.applies_to_branch(commit.branch):
        return None
    if not rule.in_force_on(commit.commit_date.date()):
        return _not_in_force(rule, commit)

    exemption = rule.matching_exemption(commit.message, commit.changed_paths)
    if exemption is not None:
        return CheckResult(rule.id, "exempt", f"matched exemption: {exemption.id}")

    ci_evidence_by_issue = ci_evidence_by_issue or {}
    at_or_before: list[CIEvidence] = []
    after_commit: list[CIEvidence] = []
    for issue_key in commit.issue_keys:
        for item in ci_evidence_by_issue.get(issue_key, []):
            if item.created_at <= commit.commit_date:
                at_or_before.append(item)
            else:
                after_commit.append(item)

    if at_or_before:
        earliest = min(at_or_before, key=lambda i: i.created_at)
        return CheckResult(
            rule.id,
            "pass",
            f"{earliest.source} on {earliest.issue_key}: {earliest.description} "
            f"(dated {earliest.created_at.isoformat()}, at or before the commit)",
            earliest.url,
        )

    if after_commit:
        earliest_after = min(after_commit, key=lambda i: i.created_at)
        return CheckResult(
            rule.id,
            "unknown",
            f"CI evidence found on {earliest_after.issue_key} ({earliest_after.description}) but "
            f"dated {earliest_after.created_at.isoformat()}, AFTER the commit -- not evidence CI "
            "ran before commit",
        )

    if commit.issue_keys:
        return CheckResult(
            rule.id,
            "unknown",
            f"no JIRA comment or attachment CI evidence found on {', '.join(commit.issue_keys)}",
        )
    return CheckResult(
        rule.id, "unknown", "no issue key referenced; cannot check JIRA for CI evidence"
    )


def score_ci_artefacts_attached(
    rule: Rule,
    commit: CommitFacts,
    attachments_by_issue: dict[str, list[AttachmentEvidence]] | None = None,
    fetched_issue_keys: frozenset[str] | set[str] = frozenset(),
) -> CheckResult | None:
    """`ci-artefacts-attached` (new in v2, D24, issue #93) — `fail_allowed:
    true`. Requires *both* `ci_summary*` and `results_details*` attachments
    on the same referenced issue, each dated at or before the commit.

    `fetched_issue_keys` is the set of issue keys whose full attachment list
    has actually been fetched (this run or a prior one) — this is what lets
    "not fetched yet" (`unknown`, backfill budget) stay distinct from
    "fetched, and it's missing" (`fail`): a JIRA issue's attachment list is
    complete once fetched (governance-policy.yaml: "the attachment list of
    an issue is complete... which is what makes a fail state honest here"),
    so only a *fetched* issue can ever produce `fail`. MEASUREMENT (ours):
    when a commit references more than one issue key, this scores against
    the first referenced key whose attachments have been fetched (in
    `commit.issue_keys` order) — see docs/spec/GOVERNANCE.md for the
    documented choice.
    """
    if not rule.applies_to_branch(commit.branch):
        return None
    if not rule.in_force_on(commit.commit_date.date()):
        return _not_in_force(rule, commit)

    exemption = rule.matching_exemption(commit.message, commit.changed_paths)
    if exemption is not None:
        return CheckResult(rule.id, "exempt", f"matched exemption: {exemption.id}")

    if not commit.issue_keys:
        return CheckResult(rule.id, "unknown", "no issue key referenced")

    attachments_by_issue = attachments_by_issue or {}
    fetched_keys = [key for key in commit.issue_keys if key in fetched_issue_keys]
    if not fetched_keys:
        return CheckResult(
            rule.id,
            "unknown",
            f"attachment list not yet fetched for {', '.join(commit.issue_keys)} "
            "(backfill budget)",
        )

    for key in fetched_keys:
        attachments = attachments_by_issue.get(key, [])
        ci_summary_hits = [
            a
            for a in attachments
            if _CI_SUMMARY_RE.match(a.filename) and a.created_at <= commit.commit_date
        ]
        results_details_hits = [
            a
            for a in attachments
            if _RESULTS_DETAILS_RE.match(a.filename) and a.created_at <= commit.commit_date
        ]
        if ci_summary_hits and results_details_hits:
            return CheckResult(
                rule.id,
                "pass",
                f"{key}: {ci_summary_hits[0].filename!r} and {results_details_hits[0].filename!r} "
                "both attached at or before the commit",
            )

    # No fetched key had both artefacts at or before the commit -- fail,
    # naming what's missing (and whether it was attached later) on the
    # first fetched key.
    key = fetched_keys[0]
    attachments = attachments_by_issue.get(key, [])
    ci_summary_any = [a for a in attachments if _CI_SUMMARY_RE.match(a.filename)]
    results_details_any = [a for a in attachments if _RESULTS_DETAILS_RE.match(a.filename)]
    ci_summary_ok = any(a.created_at <= commit.commit_date for a in ci_summary_any)
    results_details_ok = any(a.created_at <= commit.commit_date for a in results_details_any)

    parts = []
    if not ci_summary_ok:
        if ci_summary_any:
            attached_at = min(a.created_at for a in ci_summary_any).isoformat()
            parts.append(f"ci_summary missing at commit time (attached later, {attached_at})")
        else:
            parts.append("ci_summary missing")
    if not results_details_ok:
        if results_details_any:
            attached_at = min(a.created_at for a in results_details_any).isoformat()
            parts.append(f"results_details missing at commit time (attached later, {attached_at})")
        else:
            parts.append("results_details missing")
    return CheckResult(rule.id, "fail", f"{key}: " + "; ".join(parts))


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
    # v2 (issue #93): feeds the descriptive-only ninja-count-trend (see
    # `is_ninja_declared` below) -- computed here, alongside the other
    # `scored: false` per-commit facts, since it's the same kind of thing
    # (a plain fact about the commit, never a pass/fail/unknown verdict) and
    # `commit_fact` is already the table the site reads to build display-only
    # per-commit signals.
    ninja_declared: bool


def build_commit_facts_row(commit: CommitFacts) -> CommitFactsRow:
    """The three `scored: false` facts (governance-policy.yaml
    `changes-txt-entry`, `news-txt-entry`, `test-touched`), derived from
    `commit.changed_paths` (`git show --name-only`, per each rule's
    `check_method`), plus the descriptive `ninja_declared` fact (v2, issue
    #93) derived from `commit.message`. `changed_paths=None` (not collected)
    reads as "no" for the three path-based facts rather than raising --
    these are advisory display facts, never a scored result.
    """
    paths = commit.changed_paths or ()
    return CommitFactsRow(
        sha=commit.sha,
        branch=commit.branch,
        commit_date=commit.commit_date,
        changes_txt_touched="CHANGES.txt" in paths,
        news_txt_touched="NEWS.txt" in paths,
        test_touched=any(p == "test" or p.startswith("test/") for p in paths),
        ninja_declared=is_ninja_declared(commit.message),
    )


# --- Descriptive, unscored ninja-count trend (governance-policy.yaml
# `reviewer-present.descriptive_signal[0]`, id `ninja-count-trend`) ---------
#
# v2 (D24, issue #93) removes `ninja` as an *exemption* -- it has no official
# source, so a self-declared ninja commit is now scored like any other
# commit. The policy nonetheless keeps a purely descriptive count "of
# commits whose message self-declares 'ninja'" next to the rule, never fed
# into scoring. Since v2's `governance-policy.yaml` no longer carries this
# pattern anywhere (the old `ninja` exemption entry, pattern included, only
# still exists in git history -- see `removed_in_v2`), this constant is the
# one place in this codebase that keeps it, so the trend can still be
# computed: MEASUREMENT (ours) -- this is a display-only signal with no
# official source, not a rule or exemption, so D24's "no source, no
# pass/fail" restriction doesn't apply to it (the policy file's own
# `descriptive_signal.scored: false` says so explicitly); reusing the
# retired v1 pattern verbatim, rather than inventing a new one, keeps the
# trend comparable across the v1/v2 boundary.
NINJA_COUNT_TREND_PATTERN = re.compile(r"(?i)\bninja(fix)?\b")


def is_ninja_declared(message: str) -> bool:
    """True if `message` self-declares "ninja"/"ninjafix" by the retired v1
    exemption pattern (see `NINJA_COUNT_TREND_PATTERN`) -- descriptive only,
    never used for scoring in v2."""
    return NINJA_COUNT_TREND_PATTERN.search(message) is not None
