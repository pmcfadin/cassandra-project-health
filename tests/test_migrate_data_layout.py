"""Tests for scripts/migrate_data_layout.py (issue #176, epic #168).

Exercises the real script (imported directly from `scripts/`, not part of
the installed package) against a real git repository in `tmp_path` --
`git mv`/`git commit` need a real git history to operate on, so this runs
actual `git` subprocess calls, not a mocked filesystem.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migrate_data_layout.py"
_spec = importlib.util.spec_from_file_location("migrate_data_layout", _SCRIPT_PATH)
migrate_data_layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_data_layout)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "data")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "commit.gpgsign", "false")


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_legacy_fixture(root: Path) -> None:
    """A pre-#176 data branch: Cassandra's own raw/snapshots/manifests/
    state at the top level, with the peer-context data nested inside it --
    plus one already-migrated subproject under `projects/`, exactly the
    real data branch's shape after issue #169/#172."""
    _write(root / "raw" / "git" / "contribution_event" / "date=2026-01-01" / "part-run1.parquet")
    _write(root / "raw" / "github" / "pr" / "date=2026-01-01" / "part-run1.parquet")
    _write(
        root / "raw" / "peers" / "kafka" / "git" / "contribution_event" / "date=2026-01-01"
        / "part-peers-run1.parquet"
    )
    _write(root / "snapshots" / "2026-01-01T000000Z-abc123" / "metrics.parquet")
    _write(root / "snapshots" / "conversation_patterns" / "2026-01-01.json", "{}")
    _write(root / "snapshots" / "peers" / "peers-run1" / "kafka" / "metric_value.parquet")
    _write(root / "manifests" / "2026-01-01T000000Z-abc123.json", "{}")
    _write(root / "manifests" / "peers-peers-20260101T000000Z.json", "{}")
    _write(
        root / "state" / "watermarks.json",
        json.dumps(
            {
                "git": "cassandra-git-watermark",
                "github": "cassandra-github-watermark",
                "peers/kafka/git": "kafka-git-watermark",
                "peers/kafka/github": "kafka-github-watermark",
            }
        ),
    )
    _write(root / "state" / "peers" / "rotation.json", json.dumps({"next_start_index": 2}))
    _write(root / "state" / "latest_run.json", json.dumps({"run_id": "2026-01-01T000000Z-abc123"}))
    _write(root / "projects" / "java-driver" / "raw" / "github" / "pr" / "date=2026-01-01" / "x")

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture: pre-#176 legacy layout")


def test_is_migrated_false_on_legacy_fixture(tmp_path):
    root = tmp_path / "data"
    _init_repo(root)
    _write_legacy_fixture(root)
    assert migrate_data_layout.is_migrated(root) is False


def test_is_migrated_true_on_a_fresh_or_already_migrated_root(tmp_path):
    root = tmp_path / "data"
    _init_repo(root)
    _write(root / "projects" / "java-driver" / "raw" / "github" / "pr" / "x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture: subproject-only, no legacy root data")
    assert migrate_data_layout.is_migrated(root) is True


def test_migrate_moves_cassandra_and_peers_data_to_their_new_homes(tmp_path):
    root = tmp_path / "data"
    _init_repo(root)
    _write_legacy_fixture(root)

    committed = migrate_data_layout.migrate(root)
    assert committed is True

    cassandra = root / "projects" / "cassandra"
    peers = root / "peers"

    # Cassandra's own data moved under projects/cassandra/.
    assert (cassandra / "raw" / "git" / "contribution_event").exists()
    assert (cassandra / "raw" / "github" / "pr").exists()
    assert (cassandra / "snapshots" / "2026-01-01T000000Z-abc123" / "metrics.parquet").exists()
    assert (cassandra / "snapshots" / "conversation_patterns" / "2026-01-01.json").exists()
    assert (cassandra / "manifests" / "2026-01-01T000000Z-abc123.json").exists()
    assert (cassandra / "state" / "latest_run.json").exists()

    # Nothing of Cassandra's own data left behind at the legacy top level.
    assert not (root / "raw").exists()
    assert not (root / "snapshots").exists()
    assert not (root / "manifests").exists()
    assert not (root / "state").exists()

    # Peer-context data moved to its own peers/ root.
    assert (peers / "raw" / "peers" / "kafka" / "git" / "contribution_event").exists()
    kafka_snapshot = peers / "snapshots" / "peers" / "peers-run1" / "kafka"
    assert (kafka_snapshot / "metric_value.parquet").exists()
    assert (peers / "manifests" / "peers-peers-20260101T000000Z.json").exists()
    assert (peers / "state" / "peers" / "rotation.json").exists()
    assert json.loads((peers / "state" / "peers" / "rotation.json").read_text()) == {
        "next_start_index": 2
    }
    # ... and never duplicated under Cassandra's own tree.
    assert not (cassandra / "raw" / "peers").exists()
    assert not (cassandra / "snapshots" / "peers").exists()
    assert not list(cassandra.glob("manifests/peers-*.json"))
    assert not (cassandra / "state" / "peers").exists()

    # An already-migrated subproject is left completely untouched.
    java_driver_pr = root / "projects" / "java-driver" / "raw" / "github" / "pr"
    assert (java_driver_pr / "date=2026-01-01" / "x").exists()

    # One commit, with the expected message.
    log = _git(root, "log", "--format=%s")
    assert log.strip().splitlines()[0] == (
        "data: migrate Cassandra + peer-context data into projects/cassandra/ "
        "and peers/ (issue #176)"
    )


def test_migrate_splits_watermarks_by_peers_prefix(tmp_path):
    root = tmp_path / "data"
    _init_repo(root)
    _write_legacy_fixture(root)

    migrate_data_layout.migrate(root)

    cassandra_watermarks = json.loads(
        (root / "projects" / "cassandra" / "state" / "watermarks.json").read_text()
    )
    assert cassandra_watermarks == {
        "git": "cassandra-git-watermark",
        "github": "cassandra-github-watermark",
    }

    peers_watermarks = json.loads((root / "peers" / "state" / "watermarks.json").read_text())
    assert peers_watermarks == {
        "peers/kafka/git": "kafka-git-watermark",
        "peers/kafka/github": "kafka-github-watermark",
    }


def test_migrate_preserves_history_git_log_follow_on_a_moved_partition(tmp_path):
    """Issue #176 acceptance: `git log --follow` still walks a moved raw
    partition file's history across the migration commit."""
    root = tmp_path / "data"
    _init_repo(root)
    _write_legacy_fixture(root)

    migrate_data_layout.migrate(root)

    new_path = (
        root / "projects" / "cassandra" / "raw" / "git" / "contribution_event"
        / "date=2026-01-01" / "part-run1.parquet"
    )
    assert new_path.exists()

    log = _git(root, "log", "--follow", "--format=%s", "--", str(new_path.relative_to(root)))
    subjects = log.strip().splitlines()
    assert "fixture: pre-#176 legacy layout" in subjects
    assert len(subjects) >= 2  # the fixture commit AND the migration commit both show up


def test_migrate_is_idempotent(tmp_path):
    root = tmp_path / "data"
    _init_repo(root)
    _write_legacy_fixture(root)

    first = migrate_data_layout.migrate(root)
    assert first is True

    head_before = _git(root, "rev-parse", "HEAD").strip()
    second = migrate_data_layout.migrate(root)
    assert second is False
    head_after = _git(root, "rev-parse", "HEAD").strip()
    assert head_before == head_after  # no new commit made


def test_migrate_resumes_a_partially_applied_migration(tmp_path):
    """A prior run that moved `raw/` but was interrupted before touching
    `snapshots/`/`manifests/`/`state/` -- re-running must finish the rest
    without erroring or duplicating the already-moved half."""
    root = tmp_path / "data"
    _init_repo(root)
    _write_legacy_fixture(root)

    cassandra = root / "projects" / "cassandra"
    cassandra.mkdir(parents=True, exist_ok=True)
    _git(root, "mv", "raw", str((cassandra / "raw").relative_to(root)))
    _git(root, "commit", "-q", "-m", "partial: moved raw/ only")

    committed = migrate_data_layout.migrate(root)
    assert committed is True

    assert (cassandra / "raw" / "git" / "contribution_event").exists()
    assert not (root / "peers" / "raw" / "peers").exists() or (
        root / "peers" / "raw" / "peers" / "kafka"
    ).exists()
    assert (root / "peers" / "raw" / "peers" / "kafka" / "git").exists()
    assert (cassandra / "snapshots" / "2026-01-01T000000Z-abc123" / "metrics.parquet").exists()
    assert not (root / "snapshots").exists()
    assert not (root / "manifests").exists()
    assert not (root / "state").exists()


def test_main_cli_exits_zero_on_already_migrated_root(tmp_path, capsys):
    root = tmp_path / "data"
    _init_repo(root)
    _write(root / "projects" / "java-driver" / "raw" / "x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")

    exit_code = migrate_data_layout.main(["--root", str(root)])
    assert exit_code == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_main_cli_errors_on_a_non_git_root(tmp_path, capsys):
    root = tmp_path / "not-a-repo"
    root.mkdir()

    exit_code = migrate_data_layout.main(["--root", str(root)])
    assert exit_code == 2
    assert "doesn't look like a git checkout" in capsys.readouterr().err
