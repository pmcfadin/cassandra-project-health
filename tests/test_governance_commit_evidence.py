"""Tests for project_health.governance.commit_evidence (issue #97,
orchestrator review of 127bd5a).

These facts must never depend on `governance-policy.yaml` at all -- no
`Rule`, no `Policy`, no `effective_from`, no exemption is ever imported or
referenced here, only raw evidence.
"""

from datetime import datetime, timezone

from project_health.governance.checks import AttachmentEvidence, CIEvidence, CommitFacts
from project_health.governance.commit_evidence import build_commit_evidence_rows


def _commit(**overrides) -> CommitFacts:
    defaults = dict(
        sha="a" * 40,
        branch="trunk",
        commit_date=datetime(2018, 6, 1, tzinfo=timezone.utc),  # well before any rule's
        # effective_from -- deliberately, to prove these facts don't care.
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


def test_no_ticket():
    rows = build_commit_evidence_rows([_commit(issue_keys=())])
    row = rows[0]
    assert row["ci_evidence_bucket"] == "no_ticket"
    assert row["ci_evidence_text"] == "no ticket referenced"
    assert row["ci_artefacts_bucket"] == "no_ticket"
    assert row["ci_artefacts_text"] == "no ticket referenced"


def test_not_checked_when_ticket_never_fetched():
    rows = build_commit_evidence_rows(
        [_commit()], fetched_attachment_issue_keys=frozenset()
    )
    row = rows[0]
    assert row["ci_evidence_bucket"] == "not_checked"
    assert row["ci_evidence_text"] == "not checked"
    assert row["ci_artefacts_bucket"] == "not_checked"
    assert row["ci_artefacts_text"] == "not checked"


def test_pre_2026_08_19_commit_with_fetched_attachments_shows_them():
    """The orchestrator's specific regression case: a commit dated well
    before ci-artefacts-attached's 2026-08-19 effective_from, whose ticket
    *was* checked and has both artefacts attached before the commit, must
    show them -- never 'not applicable'."""
    commit = _commit(commit_date=datetime(2018, 6, 1, tzinfo=timezone.utc))
    attachments_by_issue = {
        "CASSANDRA-100": [
            AttachmentEvidence(
                issue_key="CASSANDRA-100",
                attachment_id="1",
                filename="ci_summary.html",
                created_at=datetime(2018, 5, 31, tzinfo=timezone.utc),
            ),
            AttachmentEvidence(
                issue_key="CASSANDRA-100",
                attachment_id="2",
                filename="results_details.tar.gz",
                created_at=datetime(2018, 5, 31, tzinfo=timezone.utc),
            ),
        ]
    }
    rows = build_commit_evidence_rows(
        [commit],
        attachments_by_issue=attachments_by_issue,
        fetched_attachment_issue_keys={"CASSANDRA-100"},
    )
    row = rows[0]
    assert row["ci_artefacts_bucket"] == "both"
    assert "ci_summary + results_details attached" in row["ci_artefacts_text"]
    assert "not applicable" not in row["ci_artefacts_text"]
    assert "not applicable" not in row["ci_evidence_text"]
    assert row["ci_artefacts_at"] is not None


def test_ci_evidence_before_and_after_commit():
    commit_date = datetime(2015, 1, 10, tzinfo=timezone.utc)
    commit = _commit(commit_date=commit_date)
    ci_evidence_by_issue = {
        "CASSANDRA-100": [
            CIEvidence(
                issue_key="CASSANDRA-100",
                source="jira_comment_ci_mention",
                created_at=datetime(2015, 1, 15, tzinfo=timezone.utc),  # after
                description="JIRA comment 1 matched CI term 'jenkins'",
            )
        ]
    }
    rows = build_commit_evidence_rows(
        [commit],
        ci_evidence_by_issue=ci_evidence_by_issue,
        fetched_attachment_issue_keys={"CASSANDRA-100"},
    )
    row = rows[0]
    assert row["ci_evidence_bucket"] == "after"
    assert "after commit" in row["ci_evidence_text"]
    assert row["ci_evidence_lead_time_seconds"] < 0


def test_ci_artefacts_partial():
    commit_date = datetime(2015, 1, 10, tzinfo=timezone.utc)
    commit = _commit(commit_date=commit_date)
    attachments_by_issue = {
        "CASSANDRA-100": [
            AttachmentEvidence(
                issue_key="CASSANDRA-100",
                attachment_id="1",
                filename="ci_summary.html",
                created_at=datetime(2015, 1, 9, tzinfo=timezone.utc),
            ),
        ]
    }
    rows = build_commit_evidence_rows(
        [commit],
        attachments_by_issue=attachments_by_issue,
        fetched_attachment_issue_keys={"CASSANDRA-100"},
    )
    row = rows[0]
    assert row["ci_artefacts_bucket"] == "partial"
    assert row["ci_artefacts_text"] == "ci_summary only"


def test_checked_but_nothing_found():
    rows = build_commit_evidence_rows(
        [_commit()],
        ci_evidence_by_issue={},
        attachments_by_issue={},
        fetched_attachment_issue_keys={"CASSANDRA-100"},
    )
    row = rows[0]
    assert row["ci_evidence_bucket"] == "none"
    assert row["ci_evidence_text"] == "none found"
    assert row["ci_artefacts_bucket"] == "none"
    assert row["ci_artefacts_text"] == "none found"


def test_no_policy_words_ever_appear():
    """Never 'not applicable', 'exempt', 'in force', 'required', regardless
    of the commit's date."""
    banned = ("not applicable", "exempt", "in force", "required")
    for commit_date in (
        datetime(2009, 1, 1, tzinfo=timezone.utc),
        datetime(2020, 6, 24, tzinfo=timezone.utc),
        datetime(2026, 8, 18, tzinfo=timezone.utc),
        datetime(2026, 9, 1, tzinfo=timezone.utc),
    ):
        for fetched in (set(), {"CASSANDRA-100"}):
            rows = build_commit_evidence_rows(
                [_commit(commit_date=commit_date)],
                fetched_attachment_issue_keys=fetched,
            )
            row = rows[0]
            for text in (row["ci_evidence_text"], row["ci_artefacts_text"]):
                for word in banned:
                    assert word not in text.lower(), (commit_date, fetched, text)
