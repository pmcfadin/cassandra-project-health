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
    CODE_STYLE_CHECKSTYLE,
    JIRA_TICKET_REFERENCED,
    PRE_COMMIT_CI_EVIDENCE,
    REVIEWER_PRESENT,
)
from project_health.governance.metrics import DEFINITION_VERSION, metric_id_for_check
from project_health.schema import get_schema, validate

_CHECK_IDS: tuple[str, ...] = (
    REVIEWER_PRESENT,
    JIRA_TICKET_REFERENCED,
    PRE_COMMIT_CI_EVIDENCE,
    CODE_STYLE_CHECKSTYLE,
)

_CHANGELOG_NOTE = (
    "Initial v1.0 (issue #36): monthly pass-rate among scored outcomes "
    "(pass+fail+unknown; exempt and not_in_force excluded from both the "
    "numerator and denominator) per governance-policy.yaml check, computed "
    "over non-merge commits only (docs/spec/GOVERNANCE.md §3)."
)

_DESCRIPTIONS: dict[str, str] = {
    REVIEWER_PRESENT: (
        "Monthly pass rate for governance-policy.yaml's reviewer-present check: share of "
        "non-merge, in-scope commits with a named reviewer (commit trailer or JIRA Reviewers/"
        "Reviewer field), among commits where the check produced pass/fail/unknown (exempt "
        "ninja/release-housekeeping commits and not-yet-in-force commits are excluded from the "
        "rate). details_json carries the raw pass/fail/unknown/exempt/not_in_force counts."
    ),
    JIRA_TICKET_REFERENCED: (
        "Monthly pass rate for governance-policy.yaml's jira-ticket-referenced check "
        "(no fail state -- rate is pass / (pass+unknown))."
    ),
    PRE_COMMIT_CI_EVIDENCE: (
        "Monthly pass rate for governance-policy.yaml's pre-commit-ci-evidence check "
        "(JIRA-comment CI evidence found; no fail state in v1 -- rate is pass / (pass+unknown))."
    ),
    CODE_STYLE_CHECKSTYLE: (
        "Monthly pass rate for governance-policy.yaml's code-style-checkstyle check "
        "(cassandra-4.1+/trunk only), from the GitHub Checks API's ant-check-jdk11/jdk17 "
        "check-runs."
    ),
}


def build_governance_registry(changed_at: datetime) -> pa.Table:
    """One `metric_definition_version` row per scored governance check, all
    at `DEFINITION_VERSION` ("1.0"), stamped with the given `changed_at`."""
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
