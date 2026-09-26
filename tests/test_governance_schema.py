"""Schema tests for the governance compliance engine's tables (issue #36)."""

from datetime import datetime, timezone

import pyarrow as pa
import pytest

from project_health.schema import SchemaValidationError, get_schema, validate

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def test_commit_compliance_schema_registered():
    schema = get_schema("commit_compliance")
    assert schema.names == [
        "sha",
        "branch",
        "commit_date",
        "author",
        "committer",
        "is_merge",
        "reviewers",
        "jira_keys",
        "policy_version",
        "check_id",
        "result",
        "evidence",
        "evidence_url",
    ]
    assert schema.field("evidence_url").nullable is True
    assert schema.field("reviewers").type == pa.list_(pa.string())


def test_commit_fact_schema_registered():
    schema = get_schema("commit_fact")
    assert schema.names == [
        "sha",
        "branch",
        "commit_date",
        "changes_txt_touched",
        "news_txt_touched",
        "test_touched",
    ]


def _valid_compliance_row() -> dict:
    return {
        "sha": "a" * 40,
        "branch": "trunk",
        "commit_date": NOW,
        "author": "Alice",
        "committer": "Alice",
        "is_merge": False,
        "reviewers": ["Bob"],
        "jira_keys": ["CASSANDRA-100"],
        "policy_version": 1,
        "check_id": "reviewer-present",
        "result": "pass",
        "evidence": "commit trailer reviewer(s): Bob",
        "evidence_url": None,
    }


def test_validate_accepts_well_formed_commit_compliance_row():
    table = pa.Table.from_pylist([_valid_compliance_row()], schema=get_schema("commit_compliance"))
    result = validate("commit_compliance", table)
    assert result.num_rows == 1


def test_validate_rejects_missing_evidence_column():
    row = _valid_compliance_row()
    table = pa.Table.from_pylist([row], schema=get_schema("commit_compliance")).drop(["evidence"])
    with pytest.raises(SchemaValidationError, match="missing column"):
        validate("commit_compliance", table)


def test_validate_rejects_null_result():
    row = _valid_compliance_row()
    row["result"] = None
    with pytest.raises(Exception):  # pyarrow raises on constructing the table with a null in a
        # non-nullable-typed field, or validate() raises SchemaValidationError -- either way this
        # must not silently succeed.
        table = pa.Table.from_pylist([row], schema=get_schema("commit_compliance"))
        validate("commit_compliance", table)
