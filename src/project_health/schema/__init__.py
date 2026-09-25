"""Normalized-table schema definitions and validation (ARCHITECTURE.md §3, §9).

Every normalized table has an explicit pyarrow schema here, used by both
collectors and tests to fail fast on a source's shape changing unexpectedly
(ARCHITECTURE.md §9) rather than silently propagating a bad column into a
metric.
"""

from __future__ import annotations

import pyarrow as pa

from project_health.schema.tables import TABLE_SCHEMAS

__all__ = ["TABLE_SCHEMAS", "SchemaValidationError", "get_schema", "validate"]


class SchemaValidationError(ValueError):
    """Raised when a table's data doesn't match its declared schema."""


def get_schema(table_name: str) -> pa.Schema:
    """Return the declared pyarrow schema for `table_name`.

    Raises ``KeyError`` if `table_name` isn't one of the known M0 tables.
    """
    try:
        return TABLE_SCHEMAS[table_name]
    except KeyError as exc:
        known = ", ".join(sorted(TABLE_SCHEMAS))
        raise KeyError(f"unknown table {table_name!r}; known tables: {known}") from exc


def validate(table_name: str, table: pa.Table) -> pa.Table:
    """Validate `table` against the declared schema for `table_name`.

    Fails loudly (`SchemaValidationError`) on:
    - a column declared in the schema but missing from `table`
    - a column whose type doesn't match the declared type
    - a non-nullable column containing a null value

    Returns `table`, reordered/selected to the schema's declared column
    order, on success.
    """
    schema = get_schema(table_name)

    missing = [field.name for field in schema if field.name not in table.column_names]
    if missing:
        raise SchemaValidationError(
            f"{table_name}: missing column(s) {missing!r}; "
            f"expected columns {schema.names!r}"
        )

    for field in schema:
        column = table.column(field.name)
        if not column.type.equals(field.type):
            raise SchemaValidationError(
                f"{table_name}.{field.name}: expected type {field.type!r}, "
                f"got {column.type!r}"
            )
        if not field.nullable and column.null_count > 0:
            raise SchemaValidationError(
                f"{table_name}.{field.name}: column is non-nullable but "
                f"contains {column.null_count} null value(s)"
            )

    return table.select(schema.names)
