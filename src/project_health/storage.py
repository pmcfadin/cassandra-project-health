"""Data-directory layout and read/write helpers (ARCHITECTURE.md §4.2, §4.3).

Layout, rooted at `data_dir` (the checked-out `data` branch in production;
any directory in tests):

    raw/<source>/<table>/date=YYYY-MM-DD/part-<run_id>.parquet   # append-only
    snapshots/<run_id>/metrics.parquet         # full computed-metric output for a run
    manifests/<run_id>.json                    # run manifest (§5)
    state/watermarks.json                      # per-source watermark (§4.3)

This module owns the `raw/` partitions and `state/watermarks.json`.
`snapshots/` and `manifests/` are established here as part of the layout but
written by the metrics/provenance tasks (#7) that produce their content.

Writes are append-only: `write_partition` raises if the partition path it
would write to already exists, matching D2/D3's "nothing changes silently" —
a correction is a new, differently-keyed partition, never an overwrite.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.schema import get_schema, validate

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_str(value: str | date) -> str:
    """Normalize a partition date to its `YYYY-MM-DD` string form."""
    if isinstance(value, date):
        return value.isoformat()
    if not _DATE_RE.match(value):
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    return value


def raw_table_dir(data_dir: str | Path, source: str, table: str) -> Path:
    """Directory holding all partitions of `<source>/<table>` under `raw/`."""
    return Path(data_dir) / "raw" / source / table


def raw_partition_path(
    data_dir: str | Path, source: str, table: str, partition_date: str | date, run_id: str
) -> Path:
    """Path of one partition's Parquet part-file."""
    return (
        raw_table_dir(data_dir, source, table)
        / f"date={_date_str(partition_date)}"
        / f"part-{run_id}.parquet"
    )


def watermarks_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "state" / "watermarks.json"


def write_partition(
    data_dir: str | Path,
    source: str,
    table: str,
    partition_date: str | date,
    run_id: str,
    data: pa.Table,
) -> Path:
    """Validate and append-write one partition of `<source>/<table>`.

    Raises `project_health.schema.SchemaValidationError` if `data` doesn't
    match `table`'s declared schema, and `FileExistsError` if this exact
    partition path (same source, table, date, run_id) was already written —
    partitions are append-only, never overwritten.
    """
    validated = validate(table, data)
    path = raw_partition_path(data_dir, source, table, partition_date, run_id)
    if path.exists():
        raise FileExistsError(f"partition already written, refusing to overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(validated, path)
    return path


def read_table(data_dir: str | Path, source: str, table: str) -> pa.Table:
    """Read and concatenate every partition of `<source>/<table>`.

    Returns a validated `pa.Table` covering the union of all partitions. If
    no partitions have been written yet, returns an empty table with the
    table's declared schema.
    """
    table_dir = raw_table_dir(data_dir, source, table)
    part_files = sorted(table_dir.glob("date=*/part-*.parquet"))

    if not part_files:
        return get_schema(table).empty_table()

    combined = pa.concat_tables([pq.read_table(path) for path in part_files])
    return validate(table, combined)


def read_watermark(data_dir: str | Path, source: str) -> str | None:
    """Return the last-recorded watermark value for `source`, or `None`."""
    path = watermarks_path(data_dir)
    if not path.exists():
        return None
    watermarks = json.loads(path.read_text())
    return watermarks.get(source)


def write_watermark(data_dir: str | Path, source: str, value: str) -> None:
    """Record `value` as the current watermark for `source`.

    Read-modify-write against `state/watermarks.json` so writing one
    source's watermark never disturbs another's.
    """
    path = watermarks_path(data_dir)
    watermarks: dict[str, str] = {}
    if path.exists():
        watermarks = json.loads(path.read_text())
    watermarks[source] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(watermarks, indent=2, sort_keys=True))
