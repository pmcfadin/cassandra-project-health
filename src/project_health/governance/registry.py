"""Governance metric registry — `metric_definition_version` rows at v1.0
(issue #36), the governance-engine counterpart of `metrics/registry.py`.

Kept separate from `metrics/registry.py`'s M0 `_VERSIONS`/`_DESCRIPTIONS`
(never edited by this issue) so a future governance-metric version bump
never has to touch the M0 metric registry, and vice versa — same reasoning
as `governance/metrics.py` staying out of `metrics/engine.py`'s
`compute_all`.
"""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa

from project_health.governance.checks import (
    CI_ARTEFACTS_ATTACHED,
    CODE_STYLE_CHECKSTYLE,
    JIRA_TICKET_REFERENCED,
    PRE_COMMIT_CI_EVIDENCE,
    REVIEWER_PRESENT,
)
from project_health.governance.fact_metrics import (
    DEFINITION_VERSION as FACT_METRICS_DEFINITION_VERSION,
)
from project_health.governance.fact_metrics import FACT_METRIC_IDS
from project_health.governance.metrics import DEFINITION_VERSION, metric_id_for_check
from project_health.schema import get_schema, validate

_CHECK_IDS: tuple[str, ...] = (
    REVIEWER_PRESENT,
    JIRA_TICKET_REFERENCED,
    PRE_COMMIT_CI_EVIDENCE,
    CI_ARTEFACTS_ATTACHED,
    CODE_STYLE_CHECKSTYLE,
)

_CHANGELOG_NOTE = (
    "v1.0 (issue #36): monthly pass-rate among scored outcomes (pass+fail+unknown; exempt and "
    "not_in_force excluded from both the numerator and denominator) per governance-policy.yaml "
    "check, computed over non-merge commits only (docs/spec/GOVERNANCE.md §3). Policy v2 (D24, "
    "issue #93) rescored full history under the same v1.0 metric definition -- only the "
    "underlying policy changed (removed unsourced ninja/submodule-repin exemptions, added the "
    "sourced commit-then-review/release-process exemptions, added ci-artefacts-attached), not "
    "how the rate is computed; see CHANGELOG.md for v1 -> v2 counts per check."
)

_DESCRIPTIONS: dict[str, str] = {
    REVIEWER_PRESENT: (
        "Monthly pass rate for governance-policy.yaml's reviewer-present check: share of "
        "non-merge, in-scope commits with a named reviewer (commit trailer or JIRA Reviewers/"
        "Reviewer field), among commits where the check produced pass/fail/unknown (exempt "
        "commit-then-review/release-process commits and not-yet-in-force commits are excluded "
        "from the rate). details_json carries the raw pass/fail/unknown/exempt/not_in_force "
        "counts."
    ),
    JIRA_TICKET_REFERENCED: (
        "Monthly pass rate for governance-policy.yaml's jira-ticket-referenced check "
        "(no fail state -- rate is pass / (pass+unknown))."
    ),
    PRE_COMMIT_CI_EVIDENCE: (
        "Monthly pass rate for governance-policy.yaml's pre-commit-ci-evidence check (JIRA "
        "comment or attachment CI evidence dated at or before the commit; no fail state -- "
        "rate is pass / (pass+unknown))."
    ),
    CI_ARTEFACTS_ATTACHED: (
        "Monthly pass rate for governance-policy.yaml's ci-artefacts-attached check (v2, "
        "effective 2026-08-19): both the ci_summary and results_details JIRA attachments "
        "present on a referenced issue at or before the commit. fail_allowed -- only issues "
        "whose attachment list has actually been fetched can fail; unfetched issues are "
        "unknown."
    ),
    CODE_STYLE_CHECKSTYLE: (
        "Monthly pass rate for governance-policy.yaml's code-style-checkstyle check "
        "(cassandra-4.1+/trunk only), from the GitHub Checks API's ant-check-jdk11/jdk17 "
        "check-runs."
    ),
}


def build_governance_registry(changed_at: datetime) -> pa.Table:
    """One `metric_definition_version` row per scored governance check, all
    at `DEFINITION_VERSION` ("1.0"), stamped with the given `changed_at`.

    Issue #97 (D25 amendment): these five metric_ids are no longer
    registered in `metrics_meta.GOVERNANCE_METRICS`, so `generate.py` never
    renders or exports them -- the scoring engine still computes and records
    them here for its own internal provenance. See
    `build_governance_fact_metrics_registry` for the metric_ids the
    Governance page's trend cards actually use.
    """
    rows = [
        {
            "metric_id": metric_id_for_check(check_id),
            "version": DEFINITION_VERSION,
            "description": _DESCRIPTIONS[check_id],
            "changed_at": changed_at,
            "changelog_note": _CHANGELOG_NOTE,
        }
        for check_id in _CHECK_IDS
    ]
    schema = get_schema("metric_definition_version")
    return validate("metric_definition_version", pa.Table.from_pylist(rows, schema=schema))


# --- Fact-based trend metrics (issue #97, D25 amendment) --------------------

_FACT_CHANGELOG_NOTE = (
    "v1.0 (issue #97, D25 amendment): a monthly share computed directly from raw evidence "
    "(commit trailer/JIRA reviewer field, commit message issue keys, JIRA comment/attachment "
    "CI evidence, GitHub checkstyle check-runs) -- never from governance-policy.yaml's scored "
    "pass/fail/unknown/exempt/not_in_force result, and never gated by a rule's effective_from. "
    "Descriptive only: this project does not judge these shares good or bad "
    "(direction_of_good is null in metrics_meta.py)."
)

_FACT_DESCRIPTIONS: dict[str, str] = {
    "governance_commits_with_named_reviewer_share": (
        "Monthly share of non-merge trunk commits with a reviewer named in the commit trailer "
        "or a JIRA Reviewers/Reviewer field. Denominator: all non-merge trunk commits that "
        "month, back to the start of the repository."
    ),
    "governance_commits_with_ticket_share": (
        "Monthly share of non-merge trunk commits whose message references a CASSANDRA-N issue "
        "key. Denominator: all non-merge trunk commits that month."
    ),
    "governance_commits_with_ci_evidence_before_commit_share": (
        "Monthly share of ticketed non-merge trunk commits with JIRA comment or attachment CI "
        "evidence dated at or before the commit. Denominator: ticketed commits whose ticket's "
        "JIRA evidence has actually been checked -- a not-yet-checked ticket (backfill) is "
        "excluded from the rate and reported separately as details_json.n_not_checked."
    ),
    "governance_commits_with_both_ci_artefacts_share": (
        "Monthly share of ticketed non-merge trunk commits with both the ci_summary and "
        "results_details JIRA attachments present at or before the commit. Same "
        "checked/not-yet-checked denominator rule as commits_with_ci_evidence_before_commit_share."
    ),
    "governance_commits_with_checkstyle_success_share": (
        "Monthly share of non-merge trunk commits whose recorded GitHub check-run(s) all "
        "concluded success. Denominator: commits with at least one recorded check-run."
    ),
}


def build_governance_fact_metrics_registry(changed_at: datetime) -> pa.Table:
    """One `metric_definition_version` row per fact-based trend metric
    (`fact_metrics.FACT_METRIC_IDS`), all at `FACT_METRICS_DEFINITION_VERSION`
    ("1.0")."""
    rows = [
        {
            "metric_id": metric_id,
            "version": FACT_METRICS_DEFINITION_VERSION,
            "description": _FACT_DESCRIPTIONS[metric_id],
            "changed_at": changed_at,
            "changelog_note": _FACT_CHANGELOG_NOTE,
        }
        for metric_id in FACT_METRIC_IDS
    ]
    schema = get_schema("metric_definition_version")
    return validate("metric_definition_version", pa.Table.from_pylist(rows, schema=schema))
