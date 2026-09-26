"""Tests for project_health.site.leaderboard_page (D19, issue #56)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.schema import get_schema, validate
from project_health.site.leaderboard_page import (
    build_leaderboard_page_context,
    identity_correction_url,
)
from project_health.site.manifest import RunManifest

RUN_ID = "2026-09-25-abc123"
WINDOW_START = date(2025, 9, 1)
WINDOW_END = date(2026, 8, 31)

# A minimal, all-'ok' manifest -- these tests aren't about staleness badges
# (see test_site.py / TestLeaderboardStalenessBadge for those), so no
# source here should ever produce one (issue #86).
MANIFEST = RunManifest.model_validate(
    {"run_id": RUN_ID, "pipeline_code_sha": "abc1234", "sources": {}}
)


def _leaderboard_row(**overrides) -> dict:
    row = {
        "run_id": RUN_ID,
        "activity_type": "commits",
        "definition_version": "1.0",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "rank": 1,
        "identity_id": "id-1",
        "display_name": "Alice Smith",
        "organization": "unknown",
        "count": 42,
        "computed_at": datetime(2026, 9, 25, 6, 30, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _write_leaderboard_snapshot(data_dir: Path, run_id: str, rows: list[dict]) -> None:
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    table = validate(
        "contributor_leaderboard",
        pa.Table.from_pylist(rows, schema=get_schema("contributor_leaderboard")),
    )
    pq.write_table(table, snapshot_dir / "leaderboard.parquet")


def test_no_snapshot_yields_honest_empty_context(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    data_dir.mkdir()
    out_dir.mkdir()

    ctx = build_leaderboard_page_context(
        data_dir, RUN_ID, out_dir, base_prefix="../", manifest=MANIFEST
    )

    assert ctx.has_data is False
    assert ctx.window_label is None
    for leaderboard_list in ctx.lists:
        assert leaderboard_list.rows == []
        assert leaderboard_list.has_data is False
    assert (out_dir / "data" / "leaderboard.json").is_file()
    assert (out_dir / "data" / "leaderboard.csv").is_file()
    payload = json.loads((out_dir / "data" / "leaderboard.json").read_text())
    assert payload["row_count"] == 0


def test_snapshot_rows_group_by_activity_type_and_rank_ascending(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_leaderboard_snapshot(
        data_dir,
        RUN_ID,
        [
            _leaderboard_row(activity_type="commits", rank=2, identity_id="id-2", count=10),
            _leaderboard_row(activity_type="commits", rank=1, identity_id="id-1", count=42),
            _leaderboard_row(activity_type="reviews", rank=1, identity_id="id-3", count=5),
        ],
    )

    ctx = build_leaderboard_page_context(
        data_dir, RUN_ID, out_dir, base_prefix="../", manifest=MANIFEST
    )

    assert ctx.has_data is True
    assert ctx.window_label == "2025-09-01 to 2026-08-31"
    commits_list = next(lst for lst in ctx.lists if lst.activity_type == "commits")
    assert [r["identity_id"] for r in commits_list.rows] == ["id-1", "id-2"]
    reviews_list = next(lst for lst in ctx.lists if lst.activity_type == "reviews")
    assert [r["identity_id"] for r in reviews_list.rows] == ["id-3"]
    jira_list = next(lst for lst in ctx.lists if lst.activity_type == "jira_issues_resolved")
    assert jira_list.has_data is False


def test_falls_back_to_identity_id_when_display_name_missing(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_leaderboard_snapshot(
        data_dir, RUN_ID, [_leaderboard_row(display_name=None, identity_id="abcdef0123456789")]
    )

    ctx = build_leaderboard_page_context(
        data_dir, RUN_ID, out_dir, base_prefix="../", manifest=MANIFEST
    )

    commits_list = next(lst for lst in ctx.lists if lst.activity_type == "commits")
    assert commits_list.rows[0]["display_name"] == "abcdef0123456789"[:12]


def test_every_row_carries_a_correction_link(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_leaderboard_snapshot(data_dir, RUN_ID, [_leaderboard_row()])

    ctx = build_leaderboard_page_context(
        data_dir, RUN_ID, out_dir, base_prefix="../", manifest=MANIFEST
    )

    commits_list = next(lst for lst in ctx.lists if lst.activity_type == "commits")
    correction_url = commits_list.rows[0]["correction_url"]
    assert correction_url.startswith(
        "https://github.com/pmcfadin/cassandra-project-health/issues/new?"
    )
    assert "template=identity-correction.yml" in correction_url


def test_page_level_corrections_url_and_note_present(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _write_leaderboard_snapshot(data_dir, RUN_ID, [_leaderboard_row()])

    ctx = build_leaderboard_page_context(
        data_dir, RUN_ID, out_dir, base_prefix="../", manifest=MANIFEST
    )

    assert ctx.corrections_url.startswith(
        "https://github.com/pmcfadin/cassandra-project-health/issues/new?"
    )
    assert "identity_overrides.yaml" in ctx.identity_limitations_note


def test_json_and_csv_downloads_contain_all_rows(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    rows = [
        _leaderboard_row(activity_type="commits", identity_id="id-1"),
        _leaderboard_row(activity_type="reviews", identity_id="id-2", rank=1),
    ]
    _write_leaderboard_snapshot(data_dir, RUN_ID, rows)

    build_leaderboard_page_context(data_dir, RUN_ID, out_dir, base_prefix="../", manifest=MANIFEST)

    payload = json.loads((out_dir / "data" / "leaderboard.json").read_text())
    assert payload["row_count"] == 2
    assert "identity_limitations_note" in payload

    csv_text = (out_dir / "data" / "leaderboard.csv").read_text()
    lines = csv_text.strip().splitlines()
    assert lines[0].split(",")[:4] == ["activity_type", "window_start", "window_end", "rank"]
    assert len(lines) == 3  # header + 2 rows


def test_identity_correction_url_includes_identity_id_and_activity_type():
    url = identity_correction_url("abc123def456", "reviews")
    assert "identity_id=abc123def456" in url
    assert "Reviews" in url or "reviews" in url.lower()
