"""Per-project data directory resolution (issue #169, #176; epic #168).

Issue #169's original plan kept Cassandra -- the default project -- rooted
directly at the data branch's own root, with every other subproject under
`projects/<id>/`. Issue #176 ("Multi-project 7," the epic's last step) moves
Cassandra under `projects/cassandra/` too, so every project (Cassandra
included) now lives at a uniform `projects/<id>/` path, and moves the
issue #145 peer-context data (`apache/kafka`, `apache/spark`, ...) out from
underneath Cassandra's own tree into its own `peers/` root.

`storage.py`'s `raw_table_dir`/`watermarks_path`/etc. all take a `data_dir`
root and lay out `raw/`, `snapshots/`, `manifests/`, `state/` beneath it;
this module only resolves WHICH root a given project (or the peer-context
data) should pass as that `data_dir` -- it doesn't change what `storage.py`
does once it has one.

This is a *breaking* change to the data branch's on-disk layout: a `data`
branch written before issue #176 shipped has Cassandra's own raw/snapshots/
manifests/state at its top level, not under `projects/cassandra/`.
`scripts/migrate_data_layout.py` (dispatched via `.github/workflows/
migrate-data-layout.yml`) performs that one-time move. `assert_cassandra_
migrated`/`assert_peers_migrated` below are the fail-fast guard that keeps
a nightly/peers run from ever silently treating an unmigrated data branch
as "Cassandra/peers has no data yet" and starting a fresh, empty directory
-- which would trigger a full, wasteful re-backfill instead of surfacing
the real problem (the migration hasn't run yet).
"""

from __future__ import annotations

from pathlib import Path

# The one project that used to keep the data branch's root layout before
# issue #176 -- every project id (including this one, as of issue #176)
# now resolves to a `projects/<id>/` subdirectory instead.
_ROOT_PROJECT_ID = "cassandra"

# Issue #176: a data branch written before this issue shipped has
# Cassandra's own data (and, nested inside it, the peer-context data) at
# these four top-level names. Their presence -- without a migrated
# `projects/cassandra/` alongside them -- is exactly "this data branch
# hasn't been migrated yet," never "Cassandra has no data yet."
_LEGACY_ROOT_MARKERS: tuple[str, ...] = ("raw", "snapshots", "manifests", "state")

# Issue #176: the peer-context-specific legacy markers nested inside the
# pre-migration layout above -- `raw/peers/`, `snapshots/peers/`, a
# `manifests/peers-*.json` file, or `state/peers/`. Checked independently of
# `_LEGACY_ROOT_MARKERS` so a data branch that already has `projects/
# cassandra/` (Cassandra's own half of the migration done) but not yet
# `peers/` (this half not done, or never run for peers specifically) still
# fails fast rather than silently starting a fresh, empty `peers/`.
_LEGACY_PEERS_MARKERS: tuple[str, ...] = (
    "raw/peers",
    "snapshots/peers",
    "state/peers",
)


def project_data_dir(root: str | Path, project_id: str) -> Path:
    """Resolve `project_id`'s data directory root under `root`.

    `root` is the data branch's own checkout root (what today's pipeline
    already calls `data_dir`). Every project id, `"cassandra"` included
    (issue #176), resolves to `root/projects/<project_id>`.
    """
    return Path(root) / "projects" / project_id


def peers_data_dir(root: str | Path) -> Path:
    """Resolve the issue #145 peer-context data's own root under `root`
    (issue #176: moved out from underneath Cassandra's own tree into its
    own `peers/` directory, sibling to `projects/`)."""
    return Path(root) / "peers"


class LegacyDataLayoutError(RuntimeError):
    """Raised when a data-branch root still has pre-#176 data at a legacy
    path, without the matching post-#176 directory it should have been
    migrated into -- `scripts/migrate_data_layout.py` (via `.github/
    workflows/migrate-data-layout.yml`) is the fix, never a fresh run
    against an empty `projects/cassandra/`/`peers/`."""


def _has_legacy_root_data(root: Path) -> bool:
    return any((root / name).exists() for name in _LEGACY_ROOT_MARKERS)


def _has_legacy_peers_data(root: Path) -> bool:
    return any((root / name).exists() for name in _LEGACY_PEERS_MARKERS) or any(
        root.glob("manifests/peers-*.json")
    )


def assert_cassandra_migrated(root: str | Path) -> Path:
    """Resolve Cassandra's data directory under `root`, raising
    `LegacyDataLayoutError` if `root` still carries pre-#176 top-level data
    (`raw/`, `snapshots/`, `manifests/`, and/or `state/`) without a migrated
    `projects/cassandra/` alongside it.

    A data branch with NEITHER the legacy markers NOR `projects/cassandra/`
    yet (a genuinely brand-new branch, `nightly.yml`'s own first-run
    bootstrap) is not a migration failure -- returned normally, same as
    any already-migrated branch.
    """
    root = Path(root)
    target = project_data_dir(root, _ROOT_PROJECT_ID)
    if not target.exists() and _has_legacy_root_data(root):
        raise LegacyDataLayoutError(
            f"{root} still has pre-migration Cassandra data at its top level "
            "(raw/, snapshots/, manifests/, and/or state/) but "
            f"{target} doesn't exist yet. Run scripts/migrate_data_layout.py "
            "(via the migrate-data-layout workflow) before the next nightly/peers "
            "run -- never start a fresh projects/cassandra/ here, that would "
            "silently re-backfill Cassandra's entire history."
        )
    return target


def assert_peers_migrated(root: str | Path) -> Path:
    """Resolve the peer-context data's directory under `root`, raising
    `LegacyDataLayoutError` if `root` still carries pre-#176 peer-context
    data nested inside Cassandra's own legacy tree (`raw/peers/`,
    `snapshots/peers/`, `manifests/peers-*.json`, and/or `state/peers/`)
    without a migrated `peers/` alongside it.

    A data branch with NEITHER the legacy peers markers NOR `peers/` yet
    (no peer-context collection has ever run) is not a migration failure --
    returned normally.
    """
    root = Path(root)
    target = peers_data_dir(root)
    if not target.exists() and _has_legacy_peers_data(root):
        raise LegacyDataLayoutError(
            f"{root} still has pre-migration peer-context data nested under "
            "raw/peers/, snapshots/peers/, manifests/peers-*.json, and/or "
            f"state/peers/ but {target} doesn't exist yet. Run "
            "scripts/migrate_data_layout.py (via the migrate-data-layout workflow) "
            "before the next peers run -- never start a fresh peers/ here, that "
            "would silently re-backfill every peer's entire history."
        )
    return target
