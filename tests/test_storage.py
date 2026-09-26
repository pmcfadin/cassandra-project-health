"""Tests for project_health.storage (ARCHITECTURE.md §4.2, §4.3)."""

import json
from datetime import datetime, timezone

import pyarrow as pa
import pytest

from project_health import storage
from project_health.schema import SchemaValidationError


def _person_identity_table(*identity_ids: str) -> pa.Table:
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    n = len(identity_ids)
    return pa.table(
        {
            "identity_id": pa.array(list(identity_ids), type=pa.string()),
            "display_name": pa.array([f"Person {i}" for i in range(n)], type=pa.string()),
            "status": pa.array(["resolved"] * n, type=pa.string()),
            "created_at": pa.array([now] * n, type=pa.timestamp("us", tz="UTC")),
        }
    )


def test_write_partition_lays_out_path_per_architecture_4_2(tmp_path):
    table = _person_identity_table("id-1")
    path = storage.write_partition(
        tmp_path, "git", "person_identity", "2026-09-25", "run-abc", table
    )

    assert path == (
        tmp_path / "raw" / "git" / "person_identity" / "date=2026-09-25" / "part-run-abc.parquet"
    )
    assert path.exists()


def test_write_partition_validates_schema(tmp_path):
    bad_table = pa.table({"nope": pa.array([1], type=pa.int64())})
    with pytest.raises(SchemaValidationError):
        storage.write_partition(
            tmp_path, "git", "person_identity", "2026-09-25", "run-abc", bad_table
        )


def test_write_partition_rejects_rewriting_same_path(tmp_path):
    table = _person_identity_table("id-1")
    storage.write_partition(tmp_path, "git", "person_identity", "2026-09-25", "run-abc", table)

    with pytest.raises(FileExistsError):
        storage.write_partition(
            tmp_path, "git", "person_identity", "2026-09-25", "run-abc", table
        )


def test_read_table_returns_union_of_partitions(tmp_path):
    storage.write_partition(
        tmp_path,
        "git",
        "person_identity",
        "2026-09-24",
        "run-1",
        _person_identity_table("id-1"),
    )
    storage.write_partition(
        tmp_path,
        "git",
        "person_identity",
        "2026-09-25",
        "run-2",
        _person_identity_table("id-2", "id-3"),
    )

    result = storage.read_table(tmp_path, "git", "person_identity")

    assert result.num_rows == 3
    assert sorted(result.column("identity_id").to_pylist()) == ["id-1", "id-2", "id-3"]


def test_read_table_with_no_partitions_returns_empty_validated_table(tmp_path):
    result = storage.read_table(tmp_path, "git", "person_identity")
    assert result.num_rows == 0
    from project_health.schema import get_schema

    assert result.schema.equals(get_schema("person_identity"))


def test_watermark_round_trip(tmp_path):
    assert storage.read_watermark(tmp_path, "git") is None

    storage.write_watermark(tmp_path, "git", "sha:abc123")
    assert storage.read_watermark(tmp_path, "git") == "sha:abc123"

    # writing a second source's watermark doesn't disturb the first's
    storage.write_watermark(tmp_path, "jira", "2026-09-25T04:00:00Z")
    assert storage.read_watermark(tmp_path, "git") == "sha:abc123"
    assert storage.read_watermark(tmp_path, "jira") == "2026-09-25T04:00:00Z"

    # overwriting an existing source's watermark is allowed (watermarks
    # advance in place; they aren't append-only partitions)
    storage.write_watermark(tmp_path, "git", "sha:def456")
    assert storage.read_watermark(tmp_path, "git") == "sha:def456"


def test_per_table_watermark_is_independent_of_the_source_watermark(tmp_path):
    """Issue #53 fixup cycle 1 (the "backfill gap"): a raw table added to an
    existing source after that source already has a watermark must get its
    own key, so reading it back before it's ever been written gives `None`
    -- "never collected" -- not the source's own, possibly-already-caught-up
    position.
    """
    storage.write_watermark(tmp_path, "git", "sha:head")

    # A table-scoped watermark that's never been written reads back None,
    # even though the source's own (untabled) watermark is already set.
    assert storage.read_watermark(tmp_path, "git", table="file_change_event") is None
    assert storage.read_watermark(tmp_path, "git") == "sha:head"

    storage.write_watermark(tmp_path, "git", "sha:head", table="file_change_event")
    assert storage.read_watermark(tmp_path, "git", table="file_change_event") == "sha:head"
    # Writing the table-scoped watermark never disturbs the source's own.
    assert storage.read_watermark(tmp_path, "git") == "sha:head"

    # A different table on the same source gets its own, independent slot.
    assert storage.read_watermark(tmp_path, "git", table="other_table") is None

    # The two keys are visible, distinct entries in state/watermarks.json.
    raw = json.loads(storage.watermarks_path(tmp_path).read_text())
    assert raw["git"] == "sha:head"
    assert raw["git:file_change_event"] == "sha:head"
