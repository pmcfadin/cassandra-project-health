"""Tests for project_health.governance.fact_metrics (issue #97, D25 amendment).

These metrics are computed directly from raw evidence, never from a scored
`commit_compliance` row -- so, unlike `governance/metrics.py`'s pass-rate
metrics, a commit from before any rule's `effective_from` (e.g. 2015) still
contributes a real value here.
"""

from datetime import date, datetime, timezone

from project_health.governance.checks import (
    AttachmentEvidence,
    CheckstyleEvidence,
    CIEvidence,
    CommitFacts,
)
from project_health.governance.fact_metrics import (
    BOTH_CI_ARTEFACTS,
    CHECKSTYLE_SUCCESS,
    CI_EVIDENCE_BEFORE_COMMIT,
    NAMED_REVIEWER,
    TICKET_REFERENCED,
    compute_monthly_fact_metrics,
)

RUN_ID = "test-run"
COMPUTED_AT = datetime(2026, 9, 28, tzinfo=timezone.utc)


def _commit(**overrides) -> CommitFacts:
    defaults = dict(
        sha="a" * 40,
        branch="trunk",
        commit_date=datetime(2015, 6, 1, tzinfo=timezone.utc),
        message="patch by Alice; reviewed by Bob for CASSANDRA-100",
        author="Alice",
        committer="Alice",
        is_merge=False,
        trailer_reviewers=("Bob",),
        issue_keys=("CASSANDRA-100",),
        changed_paths=None,
    )
    defaults.update(overrides)
    return CommitFacts(**defaults)


def _rows_by_metric(table):
    by_metric: dict[str, list[dict]] = {}
    for row in table.to_pylist():
        by_metric.setdefault(row["metric_id"], []).append(row)
    return by_metric


def test_pre_2020_commit_produces_a_real_value_not_not_in_force():
    """The whole point of this module: a rule's effective_from never gates
    these metrics."""
    commit = _commit(commit_date=datetime(2015, 6, 1, tzinfo=timezone.utc))
    table = compute_monthly_fact_metrics(
        [commit],
        jira_reviewers_by_issue={},
        ci_evidence_by_issue={},
        attachments_by_issue={},
        fetched_attachment_issue_keys=set(),
        checkstyle_runs_by_sha={},
        as_of=date(2026, 9, 1),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    by_metric = _rows_by_metric(table)
    reviewer_row = by_metric[NAMED_REVIEWER][0]
    assert reviewer_row["window_start"] == date(2015, 6, 1)
    assert reviewer_row["value"] == 1.0
    assert reviewer_row["n"] == 1
    assert reviewer_row["flag"] == "ok"


def test_named_reviewer_counts_trailer_or_jira_field():
    commits = [
        _commit(sha="a" * 40, trailer_reviewers=("Bob",)),
        _commit(sha="b" * 40, trailer_reviewers=(), issue_keys=("CASSANDRA-101",)),
        _commit(sha="c" * 40, trailer_reviewers=(), issue_keys=()),
    ]
    table = compute_monthly_fact_metrics(
        commits,
        jira_reviewers_by_issue={"CASSANDRA-101": ("Carol",)},
        ci_evidence_by_issue={},
        attachments_by_issue={},
        fetched_attachment_issue_keys=set(),
        checkstyle_runs_by_sha={},
        as_of=date(2026, 9, 1),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    row = _rows_by_metric(table)[NAMED_REVIEWER][0]
    assert row["n"] == 3
    assert row["value"] == 2 / 3


def test_ticket_referenced_requires_cassandra_key_specifically():
    commits = [
        _commit(sha="a" * 40, issue_keys=("CASSANDRA-100",)),
        _commit(sha="b" * 40, issue_keys=("CEP-7",)),
    ]
    table = compute_monthly_fact_metrics(
        commits,
        jira_reviewers_by_issue={},
        ci_evidence_by_issue={},
        attachments_by_issue={},
        fetched_attachment_issue_keys=set(),
        checkstyle_runs_by_sha={},
        as_of=date(2026, 9, 1),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    row = _rows_by_metric(table)[TICKET_REFERENCED][0]
    assert row["n"] == 2
    assert row["value"] == 1 / 2


def test_ci_evidence_excludes_not_yet_checked_from_denominator():
    commit_date = datetime(2026, 8, 20, tzinfo=timezone.utc)
    commits = [
        _commit(sha="a" * 40, commit_date=commit_date, issue_keys=("CASSANDRA-1",)),
        _commit(sha="b" * 40, commit_date=commit_date, issue_keys=("CASSANDRA-2",)),
        _commit(sha="c" * 40, commit_date=commit_date, issue_keys=("CASSANDRA-3",)),
    ]
    ci_evidence_by_issue = {
        "CASSANDRA-1": [
            CIEvidence(
                issue_key="CASSANDRA-1",
                source="jira_attachment_ci_artefact",
                created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
                description="attachment",
            )
        ],
        # CASSANDRA-2 checked, nothing found
    }
    table = compute_monthly_fact_metrics(
        commits,
        jira_reviewers_by_issue={},
        ci_evidence_by_issue=ci_evidence_by_issue,
        attachments_by_issue={},
        # CASSANDRA-3 is never fetched -- must not count against the rate.
        fetched_attachment_issue_keys={"CASSANDRA-1", "CASSANDRA-2"},
        checkstyle_runs_by_sha={},
        as_of=date(2026, 9, 1),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    row = _rows_by_metric(table)[CI_EVIDENCE_BEFORE_COMMIT][0]
    assert row["n"] == 2  # only the two checked tickets
    assert row["value"] == 1 / 2
    import json

    details = json.loads(row["details_json"])
    assert details["n_not_checked"] == 1


def test_both_ci_artefacts_share():
    commit_date = datetime(2026, 8, 20, tzinfo=timezone.utc)
    commits = [
        _commit(sha="a" * 40, commit_date=commit_date, issue_keys=("CASSANDRA-1",)),
        _commit(sha="b" * 40, commit_date=commit_date, issue_keys=("CASSANDRA-2",)),
    ]
    attachments_by_issue = {
        "CASSANDRA-1": [
            AttachmentEvidence(
                issue_key="CASSANDRA-1",
                attachment_id="1",
                filename="ci_summary.html",
                created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
            ),
            AttachmentEvidence(
                issue_key="CASSANDRA-1",
                attachment_id="2",
                filename="results_details.tar.gz",
                created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
            ),
        ],
        "CASSANDRA-2": [
            AttachmentEvidence(
                issue_key="CASSANDRA-2",
                attachment_id="3",
                filename="ci_summary.html",
                created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
            ),
        ],
    }
    table = compute_monthly_fact_metrics(
        commits,
        jira_reviewers_by_issue={},
        ci_evidence_by_issue={},
        attachments_by_issue=attachments_by_issue,
        fetched_attachment_issue_keys={"CASSANDRA-1", "CASSANDRA-2"},
        checkstyle_runs_by_sha={},
        as_of=date(2026, 9, 1),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    row = _rows_by_metric(table)[BOTH_CI_ARTEFACTS][0]
    assert row["n"] == 2
    assert row["value"] == 1 / 2


def test_checkstyle_success_share_only_over_commits_with_a_run():
    commit_date = datetime(2026, 8, 20, tzinfo=timezone.utc)
    commits = [
        _commit(sha="a" * 40, commit_date=commit_date),
        _commit(sha="b" * 40, commit_date=commit_date),
        _commit(sha="c" * 40, commit_date=commit_date),
    ]
    checkstyle_runs_by_sha = {
        "a" * 40: (
            CheckstyleEvidence(
                sha="a" * 40, check_run_name="ant-check-jdk11", conclusion="success"
            ),
        ),
        "b" * 40: (
            CheckstyleEvidence(
                sha="b" * 40, check_run_name="ant-check-jdk11", conclusion="failure"
            ),
        ),
        # "c" has no recorded run at all -- excluded from the denominator.
    }
    table = compute_monthly_fact_metrics(
        commits,
        jira_reviewers_by_issue={},
        ci_evidence_by_issue={},
        attachments_by_issue={},
        fetched_attachment_issue_keys=set(),
        checkstyle_runs_by_sha=checkstyle_runs_by_sha,
        as_of=date(2026, 9, 1),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    row = _rows_by_metric(table)[CHECKSTYLE_SUCCESS][0]
    assert row["n"] == 2
    assert row["value"] == 1 / 2


def test_merge_and_non_trunk_commits_excluded():
    commits = [
        _commit(sha="a" * 40, is_merge=True),
        _commit(sha="b" * 40, branch="cassandra-5.0"),
        _commit(sha="c" * 40),
    ]
    table = compute_monthly_fact_metrics(
        commits,
        jira_reviewers_by_issue={},
        ci_evidence_by_issue={},
        attachments_by_issue={},
        fetched_attachment_issue_keys=set(),
        checkstyle_runs_by_sha={},
        as_of=date(2026, 9, 1),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    row = _rows_by_metric(table)[NAMED_REVIEWER][0]
    assert row["n"] == 1


def test_incomplete_current_month_excluded():
    commit = _commit(commit_date=datetime(2026, 9, 15, tzinfo=timezone.utc))
    table = compute_monthly_fact_metrics(
        [commit],
        jira_reviewers_by_issue={},
        ci_evidence_by_issue={},
        attachments_by_issue={},
        fetched_attachment_issue_keys=set(),
        checkstyle_runs_by_sha={},
        as_of=date(2026, 9, 20),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    assert table.num_rows == 0


def test_schema_valid():
    from project_health.schema import get_schema

    commit = _commit()
    table = compute_monthly_fact_metrics(
        [commit],
        jira_reviewers_by_issue={},
        ci_evidence_by_issue={},
        attachments_by_issue={},
        fetched_attachment_issue_keys=set(),
        checkstyle_runs_by_sha={},
        as_of=date(2026, 9, 1),
        run_id=RUN_ID,
        computed_at=COMPUTED_AT,
    )
    assert table.schema.equals(get_schema("metric_value"))
