#!/usr/bin/env python3
"""One-time `data` branch layout migration (issue #176, epic #168 "Multi-
project 7," the epic's last step).

Before this issue, Cassandra -- the default project -- kept the data
branch's pre-#169 root layout (`raw/`, `snapshots/`, `manifests/`,
`state/watermarks.json` directly at the branch's root), with the issue
#145 peer-context data (apache/kafka, apache/spark, apache/flink, apache/
pulsar, apache/datafusion) nested *inside* that same tree
(`raw/peers/<id>/...`, `snapshots/peers/<run_id>/...`,
`manifests/peers-peers-*.json`, `state/peers/rotation.json`, and `peers/*`
watermark keys mixed into Cassandra's own `state/watermarks.json`). Every
other subproject already lived under `projects/<id>/` (issue #169).

This script moves Cassandra's own data under `projects/cassandra/` (the
same shape every other project already uses) and the peer-context data out
into its own `peers/` root, with its own `peers/state/watermarks.json`
split out of Cassandra's `state/watermarks.json` by key prefix (every
`peers/<id>/...` key moves; everything else stays Cassandra's).

Run against a real `data`-branch checkout (a `cd` there, or `--root`):

    python scripts/migrate_data_layout.py [--root <data-branch-checkout>]

Idempotent: if `root` has neither the pre-#176 legacy top-level dirs
(`raw/`, `snapshots/`, `manifests/`, `state/`) nor any pre-#176 peers-
specific marker nested under them, there is nothing to migrate and the
script exits 0 without touching the checkout (same detection
`data_layout.assert_cassandra_migrated`/`assert_peers_migrated` use to
fail fast at run time). Each individual move below is itself guarded
(source exists, destination doesn't) so re-running this script after a
partially-applied migration only finishes whatever wasn't already moved,
rather than erroring or duplicating anything.

Uses `git mv` for every directory/file move (preserving rename history, so
`git log --follow` still walks a moved partition file's history across the
move) and a plain read/rewrite/`git add` for the one file whose *content*
changes -- `state/watermarks.json`'s `peers/*` keys, split out into their
own `peers/state/watermarks.json` rather than carried wholesale. Commits
once at the end, if (and only if) anything was actually staged.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Mirrors `project_health.data_layout`'s own legacy-layout markers --
# duplicated rather than imported so this one-off migration script has no
# runtime dependency on the installed `project_health` package (it may be
# run against a bare data-branch checkout with no Python environment set
# up for the main package at all, e.g. a minimal `migrate-data-layout.yml`
# runner).
_LEGACY_ROOT_MARKERS = ("raw", "snapshots", "manifests", "state")

_COMMIT_MESSAGE = (
    "data: migrate Cassandra + peer-context data into projects/cassandra/ "
    "and peers/ (issue #176)"
)


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True)


def _git_mv(root: Path, src: Path, dst: Path) -> bool:
    """`git mv src dst`, skipped (returns `False`) if `src` doesn't exist
    or `dst` already does -- the per-step idempotency guard that makes
    re-running this script after a partial prior run safe."""
    if not src.exists() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run_git(root, "mv", str(src.relative_to(root)), str(dst.relative_to(root)))
    return True


def is_migrated(root: Path) -> bool:
    """`True` if `root` has none of the pre-#176 legacy top-level dirs --
    nothing for this script to do (either already migrated, or a branch
    that never had Cassandra's data at its root in the first place)."""
    return not any((root / name).exists() for name in _LEGACY_ROOT_MARKERS)


def _split_watermarks(path: Path) -> tuple[dict, dict]:
    """Partition `path`'s (a `state/watermarks.json`) keys into
    (cassandra_keys, peers_keys) -- every key starting with `"peers/"`
    (`peers.collect.peer_source`'s own key format, e.g. `"peers/kafka/
    git"`) is a peers key; every other key (`"git"`, `"github"`,
    `"release"`, `"github:pr_review"`, ...) stays Cassandra's."""
    watermarks: dict = json.loads(path.read_text())
    cassandra_keys = {k: v for k, v in watermarks.items() if not k.startswith("peers/")}
    peers_keys = {k: v for k, v in watermarks.items() if k.startswith("peers/")}
    return cassandra_keys, peers_keys


def _migrate_watermarks(root: Path, cassandra_dir: Path, peers_dir: Path) -> None:
    """`state/watermarks.json` already moved to `cassandra_dir/state/
    watermarks.json` (the whole-`state`-dir move below) by the time this
    runs -- rewrite it in place with only Cassandra's own keys, and (if
    there are any) write the peers keys to their own fresh `peers_dir/
    state/watermarks.json`."""
    watermarks_path = cassandra_dir / "state" / "watermarks.json"
    if not watermarks_path.is_file():
        return

    cassandra_keys, peers_keys = _split_watermarks(watermarks_path)
    watermarks_path.write_text(json.dumps(cassandra_keys, indent=2, sort_keys=True))
    _run_git(root, "add", str(watermarks_path.relative_to(root)))

    if peers_keys:
        peers_watermarks_path = peers_dir / "state" / "watermarks.json"
        if not peers_watermarks_path.exists():
            peers_watermarks_path.parent.mkdir(parents=True, exist_ok=True)
            peers_watermarks_path.write_text(json.dumps(peers_keys, indent=2, sort_keys=True))
            _run_git(root, "add", str(peers_watermarks_path.relative_to(root)))


def migrate(root: Path) -> bool:
    """Perform the migration against `root` (a real git checkout of the
    `data` branch), committing once if anything changed. Returns `True` if
    a commit was made, `False` if there was nothing to do (already
    migrated)."""
    root = root.resolve()

    if is_migrated(root):
        return False

    cassandra_dir = root / "projects" / "cassandra"
    peers_dir = root / "peers"

    # `raw/`, `snapshots/`, `manifests/`: move the whole directory to
    # Cassandra's side first (safe -- `projects/cassandra/<name>` doesn't
    # exist yet), then pull the peers-specific subtree back out of it.
    # `state/` needs its `watermarks.json` content split rather than moved
    # wholesale, handled separately below.
    _git_mv(root, root / "raw", cassandra_dir / "raw")
    _git_mv(root, cassandra_dir / "raw" / "peers", peers_dir / "raw" / "peers")

    _git_mv(root, root / "snapshots", cassandra_dir / "snapshots")
    _git_mv(root, cassandra_dir / "snapshots" / "peers", peers_dir / "snapshots" / "peers")

    _git_mv(root, root / "manifests", cassandra_dir / "manifests")
    if (cassandra_dir / "manifests").is_dir():
        for manifest_path in sorted(cassandra_dir.glob("manifests/peers-*.json")):
            _git_mv(root, manifest_path, peers_dir / "manifests" / manifest_path.name)

    _git_mv(root, root / "state", cassandra_dir / "state")
    _git_mv(root, cassandra_dir / "state" / "peers", peers_dir / "state" / "peers")
    _migrate_watermarks(root, cassandra_dir, peers_dir)

    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=root, capture_output=True
    )
    if result.returncode == 0:
        # Nothing actually got staged (e.g. every legacy marker was an
        # empty/.gitkeep-only directory with nothing real to move) --
        # `is_migrated` already returned `False` above, but there's still
        # no real content to commit.
        return False

    _run_git(root, "commit", "-m", _COMMIT_MESSAGE)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=".",
        help="The data branch's own checkout root (default: the current directory)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)

    if not (root / ".git").exists():
        # Works for a plain checkout (`.git` dir) and a `git worktree add`
        # checkout (`.git` file pointing at the real gitdir) alike.
        print(f"error: {root} doesn't look like a git checkout (no .git)", file=sys.stderr)
        return 2

    if is_migrated(root):
        print(f"migrate_data_layout: {root} has nothing to migrate (already migrated, or a "
              "branch that never had Cassandra's data at its root)")
        return 0

    committed = migrate(root)
    if committed:
        print(f"migrate_data_layout: migrated {root} and committed the result")
    else:
        print(f"migrate_data_layout: {root} had legacy marker(s) but nothing real to move")
    return 0


if __name__ == "__main__":
    sys.exit(main())
