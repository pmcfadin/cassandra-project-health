"""Contributor leaderboard page context builder (D19, issue #56).

`build_leaderboard_page_context` turns one run's `snapshots/<run_id>/
leaderboard.parquet` (the `contributor_leaderboard` table, `leaderboard.py`)
into everything `templates/community.html`'s leaderboard section needs: the
three top-N lists, the identity-resolution-limits note and corrections link
D19 requires, and the section's downloadable JSON/CSV files.

Kept out of `generate.py` (mirrors `governance_page.py`'s own reasoning,
restated here since issue #56 is explicitly landing while #35/#54 are
editing `generate.py` in parallel): this module and `leaderboard.py` are the
only things that read/write the leaderboard's own data, so `generate.py`'s
wiring stays a small, additive call plus one extra template variable.

An honest no-data state (no leaderboard snapshot for this run, or the engine
produced zero rows -- e.g. a project whose `git`/`jira` sources haven't run
yet) still returns a valid context, matching every other optional section on
this site (`governance_page.py`'s own "never ran" case).
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.leaderboard import ACTIVITY_LABELS, ACTIVITY_TYPES, IDENTITY_LIMITATIONS_NOTE
from project_health.schema import get_schema, validate
from project_health.site.manifest import RunManifest
from project_health.site.metrics_meta import LEADERBOARD_SOURCES
from project_health.site.staleness import source_staleness_badges

REPO_URL = "https://github.com/pmcfadin/cassandra-project-health"
IDENTITY_OVERRIDES_FILE_URL = f"{REPO_URL}/blob/main/identity_overrides.yaml"
CORRECTION_ISSUE_TEMPLATE = "identity-correction.yml"
CORRECTION_NEW_ISSUE_URL = f"{REPO_URL}/issues/new"


def identity_correction_url(
    identity_id: str | None = None, activity_type: str | None = None
) -> str:
    """A pre-filled "request an identity correction" GitHub issue-form link
    (D19: "include a link to the corrections process")."""
    params = {"template": CORRECTION_ISSUE_TEMPLATE, "labels": "identity-correction"}
    title = "Identity correction"
    if identity_id:
        title += f": {identity_id[:8]}"
        params["identity_id"] = identity_id
    if activity_type:
        title += f" ({ACTIVITY_LABELS.get(activity_type, activity_type)})"
    params["title"] = title
    return f"{CORRECTION_NEW_ISSUE_URL}?{urlencode(params)}"


def _read_optional_leaderboard_table(data_dir: Path, run_id: str) -> pa.Table:
    path = Path(data_dir) / "snapshots" / run_id / "leaderboard.parquet"
    if not path.is_file():
        return get_schema("contributor_leaderboard").empty_table()
    return validate("contributor_leaderboard", pq.read_table(path))


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


@dataclass(frozen=True)
class LeaderboardListContext:
    activity_type: str
    label: str
    window_start: str | None
    window_end: str | None
    rows: list[dict[str, Any]]
    has_data: bool


@dataclass(frozen=True)
class LeaderboardPageContext:
    has_data: bool
    lists: list[LeaderboardListContext]
    identity_limitations_note: str
    corrections_url: str
    json_href: str
    csv_href: str
    window_label: str | None
    # issue #86, ARCHITECTURE.md §7.3: the leaderboard has no `metric_id` of
    # its own (it's a ranked table, not a `metric_value` series), so its
    # staleness badge(s) are computed straight from `LEADERBOARD_SOURCES`
    # rather than a `MetricMeta.sources` lookup.
    staleness_badges: list[dict[str, Any]]


def _rows_for(table_rows: list[dict[str, Any]], activity_type: str) -> list[dict[str, Any]]:
    rows = [r for r in table_rows if r["activity_type"] == activity_type]
    rows.sort(key=lambda r: r["rank"])
    out = []
    for row in rows:
        out.append(
            {
                "rank": row["rank"],
                "identity_id": row["identity_id"],
                "display_name": row["display_name"] or row["identity_id"][:12],
                "organization": row["organization"],
                "count": row["count"],
                "correction_url": identity_correction_url(row["identity_id"], activity_type),
            }
        )
    return out


def _write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "activity_types": list(ACTIVITY_TYPES),
        "identity_limitations_note": IDENTITY_LIMITATIONS_NOTE,
        "row_count": len(rows),
        "rows": rows,
    }
    path.write_text(json.dumps(payload, default=_json_default, indent=2) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "activity_type",
            "window_start",
            "window_end",
            "rank",
            "identity_id",
            "display_name",
            "organization",
            "count",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["activity_type"],
                row["window_start"].isoformat(),
                row["window_end"].isoformat(),
                row["rank"],
                row["identity_id"],
                row["display_name"] or "",
                row["organization"],
                row["count"],
            ]
        )
    path.write_text(buffer.getvalue())


def build_leaderboard_page_context(
    data_dir: str | Path,
    run_id: str,
    out_dir: Path,
    *,
    base_prefix: str,
    manifest: RunManifest,
) -> LeaderboardPageContext:
    """Build the Community page's leaderboard section context, and write its
    downloadable `data/leaderboard.json` / `.csv` files into `out_dir`."""
    data_dir = Path(data_dir)
    table = _read_optional_leaderboard_table(data_dir, run_id)
    table_rows = table.to_pylist()

    data_out = Path(out_dir) / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    json_href = f"{base_prefix}data/leaderboard.json"
    csv_href = f"{base_prefix}data/leaderboard.csv"

    _write_json(data_out / "leaderboard.json", table_rows)
    _write_csv(data_out / "leaderboard.csv", table_rows)

    has_data = bool(table_rows)
    window_label = None
    if has_data:
        first = table_rows[0]
        window_label = f"{first['window_start'].isoformat()} to {first['window_end'].isoformat()}"

    lists = [
        LeaderboardListContext(
            activity_type=activity_type,
            label=ACTIVITY_LABELS[activity_type],
            window_start=table_rows[0]["window_start"].isoformat() if has_data else None,
            window_end=table_rows[0]["window_end"].isoformat() if has_data else None,
            rows=_rows_for(table_rows, activity_type),
            has_data=any(r["activity_type"] == activity_type for r in table_rows),
        )
        for activity_type in ACTIVITY_TYPES
    ]

    return LeaderboardPageContext(
        has_data=has_data,
        lists=lists,
        identity_limitations_note=IDENTITY_LIMITATIONS_NOTE,
        corrections_url=identity_correction_url(),
        json_href=json_href,
        csv_href=csv_href,
        window_label=window_label,
        staleness_badges=source_staleness_badges(LEADERBOARD_SOURCES, manifest),
    )
