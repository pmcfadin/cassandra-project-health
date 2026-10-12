"""Per-project data directory resolution (issue #169, epic #168).

Epic #168's data-branch plan moves every *subproject's* raw/snapshots/
manifests/state layout under `projects/<id>/` on the `data` branch, while
Cassandra -- the default project, staying at the site root -- keeps its
existing layout rooted directly at the data branch's root (unchanged, so
today's production `data` branch needs no migration for this issue).

`storage.py`'s `raw_table_dir`/`watermarks_path`/etc. all take a `data_dir`
root and lay out `raw/`, `snapshots/`, `manifests/`, `state/` beneath it;
this module only resolves WHICH root a given project should pass as that
`data_dir` -- it doesn't change what `storage.py` does once it has one.
"""

from __future__ import annotations

from pathlib import Path

# The one project that keeps the data branch's root layout -- every other
# project id gets a `projects/<id>/` subdirectory instead (epic #168).
_ROOT_PROJECT_ID = "cassandra"


def project_data_dir(root: str | Path, project_id: str) -> Path:
    """Resolve `project_id`'s data directory root under `root`.

    `root` is the data branch's own checkout root (what today's single-
    project pipeline already calls `data_dir`). `project_id == "cassandra"`
    resolves to `root` itself, unchanged; any other project id resolves to
    `root/projects/<project_id>`.
    """
    root = Path(root)
    if project_id == _ROOT_PROJECT_ID:
        return root
    return root / "projects" / project_id
