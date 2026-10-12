"""Tests for project_health.seed_subproject (issue #172, epic #168)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

from project_health import storage
from project_health.config import load_project
from project_health.seed_subproject import seed_subproject

_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _write_subproject_config(path: Path, project_id: str, repos: list[str]) -> Path:
    repos_yaml = ", ".join(f'"{r}"' for r in repos)
    path.write_text(
        f"""
project:
  id: {project_id}
  display_name: {project_id}
pull_requests:
  type: github
  repos: [{repos_yaml}]
"""
    )
    return path


def _pr_table(*rows: tuple[str, int]) -> pa.Table:
    n = len(rows)
    return pa.table(
        {
            "repo": pa.array([r[0] for r in rows], type=pa.string()),
            "number": pa.array([r[1] for r in rows], type=pa.int64()),
            "state": pa.array(["MERGED"] * n, type=pa.string()),
            "is_draft": pa.array([False] * n, type=pa.bool_()),
            "merged": pa.array([True] * n, type=pa.bool_()),
            "author_identity_id": pa.array([None] * n, type=pa.string()),
            "author_raw_type": pa.array(["github_login"] * n, type=pa.string()),
            "author_raw_value": pa.array(["alice"] * n, type=pa.string()),
            "title_hash": pa.array(["deadbeef"] * n, type=pa.string()),
            "linked_issue_keys": pa.array([[]] * n, type=pa.list_(pa.string())),
            "created_at": pa.array([_NOW] * n, type=pa.timestamp("us", tz="UTC")),
            "updated_at": pa.array([_NOW] * n, type=pa.timestamp("us", tz="UTC")),
            "closed_at": pa.array([_NOW] * n, type=pa.timestamp("us", tz="UTC")),
            "merged_at": pa.array([_NOW] * n, type=pa.timestamp("us", tz="UTC")),
            "additions": pa.array([10] * n, type=pa.int64()),
            "deletions": pa.array([2] * n, type=pa.int64()),
            "changed_files": pa.array([1] * n, type=pa.int64()),
            "source_snapshot_id": pa.array(["root-run:github"] * n, type=pa.string()),
        }
    )


def _pr_review_table(*rows: tuple[str, int]) -> pa.Table:
    n = len(rows)
    return pa.table(
        {
            "review_id": pa.array([f"rev-{i}" for i in range(n)], type=pa.string()),
            "repo": pa.array([r[0] for r in rows], type=pa.string()),
            "pr_number": pa.array([r[1] for r in rows], type=pa.int64()),
            "reviewer_identity_id": pa.array([None] * n, type=pa.string()),
            "reviewer_raw_type": pa.array(["github_login"] * n, type=pa.string()),
            "reviewer_raw_value": pa.array(["bob"] * n, type=pa.string()),
            "state": pa.array(["APPROVED"] * n, type=pa.string()),
            "submitted_at": pa.array([_NOW] * n, type=pa.timestamp("us", tz="UTC")),
            "source_snapshot_id": pa.array(["root-run:github"] * n, type=pa.string()),
        }
    )


def _pr_comment_table(*rows: tuple[str, int]) -> pa.Table:
    n = len(rows)
    return pa.table(
        {
            "comment_id": pa.array([f"cmt-{i}" for i in range(n)], type=pa.string()),
            "repo": pa.array([r[0] for r in rows], type=pa.string()),
            "pr_number": pa.array([r[1] for r in rows], type=pa.int64()),
            "review_id": pa.array([None] * n, type=pa.string()),
            "comment_type": pa.array(["issue_comment"] * n, type=pa.string()),
            "author_identity_id": pa.array([None] * n, type=pa.string()),
            "author_raw_type": pa.array(["github_login"] * n, type=pa.string()),
            "author_raw_value": pa.array(["carol"] * n, type=pa.string()),
            "created_at": pa.array([_NOW] * n, type=pa.timestamp("us", tz="UTC")),
            "source_snapshot_id": pa.array(["root-run:github"] * n, type=pa.string()),
        }
    )


def _write_root_fixture(root: Path) -> None:
    """A root data dir with PR/review/comment rows for two repos, plus a
    per-repo cursor watermark covering both -- mirrors Cassandra's own
    `pull_requests.repos` already having collected a subproject's repo."""
    storage.write_partition(
        root,
        "github",
        "pr",
        "2026-09-01",
        "run-1",
        _pr_table(
            ("apache/cassandra-java-driver", 1),
            ("apache/cassandra-java-driver", 2),
            ("apache/cassandra-sidecar", 10),
        ),
    )
    storage.write_partition(
        root,
        "github",
        "pr_review",
        "2026-09-01",
        "run-1",
        _pr_review_table(("apache/cassandra-java-driver", 1)),
    )
    storage.write_partition(
        root,
        "github",
        "pr_comment",
        "2026-09-01",
        "run-1",
        _pr_comment_table(("apache/cassandra-java-driver", 1)),
    )
    storage.write_watermark(
        root,
        "github",
        json.dumps(
            {
                "apache/cassandra": "root-cassandra-cursor",
                "apache/cassandra-java-driver": "root-java-driver-cursor",
                "apache/cassandra-sidecar": "root-sidecar-cursor",
            },
            sort_keys=True,
        ),
    )


def test_copies_only_this_projects_repos_rows_and_cursor(tmp_path):
    root = tmp_path / "root"
    dst = tmp_path / "dst" / "java-driver"
    _write_root_fixture(root)
    config_path = _write_subproject_config(
        tmp_path / "java-driver.yaml", "java-driver", ["apache/cassandra-java-driver"]
    )
    config = load_project(config_path)

    report = seed_subproject(root, config, dst)

    assert report.status == "seeded"
    assert report.repos == [
        type(report.repos[0])(
            repo="apache/cassandra-java-driver",
            rows_copied={"pr": 2, "pr_review": 1, "pr_comment": 1},
        )
    ]

    pr_table = storage.read_table(dst, "github", "pr")
    assert pr_table.num_rows == 2
    assert set(pr_table.column("repo").to_pylist()) == {"apache/cassandra-java-driver"}

    # Sidecar's rows must never leak into java-driver's own data dir.
    assert "apache/cassandra-sidecar" not in pr_table.column("repo").to_pylist()

    watermark = json.loads(storage.read_watermark(dst, "github"))
    assert watermark == {"apache/cassandra-java-driver": "root-java-driver-cursor"}


def test_is_idempotent_against_a_destination_that_already_has_a_github_watermark(tmp_path):
    root = tmp_path / "root"
    dst = tmp_path / "dst" / "sidecar"
    _write_root_fixture(root)
    config_path = _write_subproject_config(
        tmp_path / "sidecar.yaml", "sidecar", ["apache/cassandra-sidecar"]
    )
    config = load_project(config_path)

    # Simulate a real collection run that has already progressed past the
    # seeded cursor -- seeding must never regress this.
    storage.write_watermark(
        dst, "github", json.dumps({"apache/cassandra-sidecar": "already-advanced-cursor"})
    )

    report = seed_subproject(root, config, dst)

    assert report.status == "skipped_already_present"
    assert report.repos == []
    watermark = json.loads(storage.read_watermark(dst, "github"))
    assert watermark == {"apache/cassandra-sidecar": "already-advanced-cursor"}
    # No partitions written -- the real run's own data is the only data here.
    assert storage.read_table(dst, "github", "pr").num_rows == 0


def test_re_running_after_a_successful_seed_is_a_no_op(tmp_path):
    root = tmp_path / "root"
    dst = tmp_path / "dst" / "java-driver"
    _write_root_fixture(root)
    config_path = _write_subproject_config(
        tmp_path / "java-driver.yaml", "java-driver", ["apache/cassandra-java-driver"]
    )
    config = load_project(config_path)

    first = seed_subproject(root, config, dst)
    assert first.status == "seeded"

    second = seed_subproject(root, config, dst)
    assert second.status == "skipped_already_present"

    # Still exactly the rows from the first seed -- not duplicated.
    assert storage.read_table(dst, "github", "pr").num_rows == 2


def test_a_repo_with_no_matching_rows_in_root_copies_nothing(tmp_path):
    root = tmp_path / "root"
    dst = tmp_path / "dst" / "accord"
    _write_root_fixture(root)
    config_path = _write_subproject_config(
        tmp_path / "accord.yaml", "accord", ["apache/cassandra-accord"]
    )
    config = load_project(config_path)

    report = seed_subproject(root, config, dst)

    assert report.status == "seeded"
    assert report.repos[0].rows_copied == {"pr": 0, "pr_review": 0, "pr_comment": 0}
    assert storage.read_table(dst, "github", "pr").num_rows == 0
    # apache/cassandra-accord has no root cursor in the fixture watermark,
    # so no `github` watermark should be written at all for it.
    assert storage.read_watermark(dst, "github") is None
