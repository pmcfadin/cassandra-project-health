"""Tests for project_health.schema (ARCHITECTURE.md §3, §9)."""

from datetime import datetime, timezone

import pyarrow as pa
import pytest

from project_health.schema import TABLE_SCHEMAS, SchemaValidationError, get_schema, validate

ALL_M0_TABLES = [
    "person_identity",
    "identity_link",
    "contribution_event",
    "review_event",
    "issue",
    "source_snapshot",
    "run_manifest",
    "metric_definition_version",
    "metric_value",
]


def test_every_m0_table_has_a_schema():
    for name in ALL_M0_TABLES:
        assert name in TABLE_SCHEMAS
        assert get_schema(name) is TABLE_SCHEMAS[name]


def _valid_person_identity_table() -> pa.Table:
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    return pa.table(
        {
            "identity_id": pa.array(["id-1", "id-2"], type=pa.string()),
            "display_name": pa.array(["Alice", None], type=pa.string()),
            "status": pa.array(["resolved", "provisional"], type=pa.string()),
            "created_at": pa.array([now, now], type=pa.timestamp("us", tz="UTC")),
        }
    )


def test_validate_accepts_a_well_formed_table():
    table = _valid_person_identity_table()
    result = validate("person_identity", table)
    assert result.num_rows == 2
    assert result.column_names == get_schema("person_identity").names


def test_validate_rejects_missing_column():
    table = _valid_person_identity_table().drop(["display_name"])
    with pytest.raises(SchemaValidationError, match="missing column"):
        validate("person_identity", table)


def test_validate_rejects_wrong_type():
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    table = pa.table(
        {
            "identity_id": pa.array(["id-1"], type=pa.string()),
            "display_name": pa.array(["Alice"], type=pa.string()),
            # status should be string, not int64
            "status": pa.array([1], type=pa.int64()),
            "created_at": pa.array([now], type=pa.timestamp("us", tz="UTC")),
        }
    )
    with pytest.raises(SchemaValidationError, match="expected type"):
        validate("person_identity", table)


def test_validate_rejects_null_in_non_nullable_column():
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    table = pa.table(
        {
            # identity_id is non-nullable
            "identity_id": pa.array(["id-1", None], type=pa.string()),
            "display_name": pa.array(["Alice", "Bob"], type=pa.string()),
            "status": pa.array(["resolved", "resolved"], type=pa.string()),
            "created_at": pa.array([now, now], type=pa.timestamp("us", tz="UTC")),
        }
    )
    with pytest.raises(SchemaValidationError, match="non-nullable"):
        validate("person_identity", table)


def test_get_schema_unknown_table_raises_keyerror():
    with pytest.raises(KeyError):
        get_schema("not_a_real_table")


def test_contribution_event_has_raw_author_identifier_columns():
    """Collectors (#4, #5) run before identity resolution (#6), so
    contribution_event must carry the raw identifier the collector observed,
    not just the (nullable, not-yet-known) resolved identity_id.
    """
    schema = get_schema("contribution_event")
    assert schema.field("identity_id").nullable is True
    assert schema.field("author_raw_type").type == pa.string()
    assert schema.field("author_raw_type").nullable is False
    assert schema.field("author_raw_value").type == pa.string()
    assert schema.field("author_raw_value").nullable is False
    assert schema.field("author_display_name").nullable is True


def test_review_event_has_raw_reviewer_and_author_identifier_columns():
    schema = get_schema("review_event")
    assert schema.field("reviewer_identity_id").nullable is True
    assert schema.field("reviewer_raw_type").nullable is False
    assert schema.field("reviewer_raw_value").nullable is False
    assert schema.field("author_identity_id").nullable is True
    # patch-author raw identifier is nullable: not every review_event row has
    # a known patch author at collection time
    assert schema.field("author_raw_type").nullable is True
    assert schema.field("author_raw_value").nullable is True


def test_issue_has_raw_reporter_and_assignee_columns():
    schema = get_schema("issue")
    assert schema.field("reporter_identity_id").nullable is True
    assert schema.field("reporter_raw").nullable is True
    assert schema.field("assignee_identity_id").nullable is True
    assert schema.field("assignee_raw").nullable is True
    assert schema.field("status_category").nullable is True
    assert schema.field("priority").nullable is True


def test_contribution_event_validates_with_identity_id_null_pre_resolution():
    """Collectors write identity_id=null and populate only the raw author
    columns; identity resolution fills identity_id in later, over the same
    immutable raw row (ARCHITECTURE.md §3, D2).
    """
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    table = pa.table(
        {
            "event_id": pa.array(["e1"], type=pa.string()),
            "identity_id": pa.array([None], type=pa.string()),
            "author_raw_type": pa.array(["git_email"], type=pa.string()),
            "author_raw_value": pa.array(["alice@example.org"], type=pa.string()),
            "author_display_name": pa.array(["Alice"], type=pa.string()),
            "event_type": pa.array(["commit"], type=pa.string()),
            "occurred_at": pa.array([now], type=pa.timestamp("us", tz="UTC")),
            "repo": pa.array(["apache/cassandra"], type=pa.string()),
            "source_ref": pa.array(["abc123"], type=pa.string()),
            "source_snapshot_id": pa.array(["snap-1"], type=pa.string()),
        }
    )

    result = validate("contribution_event", table)

    assert result.column("identity_id").to_pylist() == [None]
    assert result.column("author_raw_value").to_pylist() == ["alice@example.org"]


def test_contribution_event_rejects_missing_author_raw_type():
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    table = pa.table(
        {
            "event_id": pa.array(["e1"], type=pa.string()),
            "identity_id": pa.array([None], type=pa.string()),
            "author_raw_value": pa.array(["alice@example.org"], type=pa.string()),
            "author_display_name": pa.array(["Alice"], type=pa.string()),
            "event_type": pa.array(["commit"], type=pa.string()),
            "occurred_at": pa.array([now], type=pa.timestamp("us", tz="UTC")),
            "repo": pa.array(["apache/cassandra"], type=pa.string()),
            "source_ref": pa.array(["abc123"], type=pa.string()),
            "source_snapshot_id": pa.array(["snap-1"], type=pa.string()),
        }
    )
    with pytest.raises(SchemaValidationError, match="missing column"):
        validate("contribution_event", table)


def test_identity_link_source_type_comment_documents_git_name():
    """git_name must be an accepted identity_link.source_type value — commit
    trailer reviewer names aren't email addresses. There's no in-schema enum
    to check at the pyarrow level (source_type is a plain string column), so
    this documents the contract at the field-comment level and via the
    schema/README.md prose.
    """
    schema = get_schema("identity_link")
    field = schema.field("source_type")
    assert field.type == pa.string()
    assert field.nullable is False


def test_metric_value_output_contract_columns():
    """The output contract (issue #2 point 4): exact column set consumed by
    the metrics task (#7) and the site generator (#8).
    """
    schema = get_schema("metric_value")
    assert schema.names == [
        "metric_id",
        "definition_version",
        "window_start",
        "window_end",
        "value",
        "n",
        "flag",
        "run_id",
        "computed_at",
        "details_json",
    ]
    assert schema.field("window_start").type == pa.date32()
    assert schema.field("value").nullable is True
    assert schema.field("n").type == pa.int64()
    assert schema.field("flag").nullable is False
