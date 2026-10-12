"""Multi-project site context (epic #168, issue #171).

`generate.generate()` renders one project's data into one output directory
at a time (issue #8/#34's original single-project contract, unchanged for
`project-health run --site-out` and this module's own default `project=None`
path). This module adds the pieces a *multi*-project render needs on top of
that, without touching `generate()`'s per-page HTML-building logic:

- `discover_projects` resolves `projects/index.yaml`'s enabled entries to a
  `DiscoveredProject` each -- its own data directory (`data_layout.
  project_data_dir`, issue #169) and the latest run_id actually available
  there. An enabled project with no resolvable run yet is left out
  entirely (`project-health site`'s own "renders every enabled project
  *with data*" contract) rather than failing the whole command.
- `ProjectSiteContext` bundles one page's own project (`current`) with the
  full available-projects list (`available`, always including `current`)
  that the header switcher (issue #171) offers.
- `project_path_depth` / `switcher_entries` are the pure string-math behind
  two things every rendered page needs: the extra `../` hops from a
  project's own root up to the *site* root (where `static/`, `methodology/`
  and `corrections/` live, copied/rendered exactly once -- never per
  project), and the switcher's own per-project, per-tab target hrefs.

Cassandra is the one project that stays at the site root (`root_path =
""`); every other project renders under `p/<id>/`. Resolving this way (the
SAME math for the real build and for `generate()`'s default `project=None`
single-project path, where `root_path` is always `""`) is what keeps
Cassandra's own output byte-identical regardless of which path renders it
-- see `generate.py`'s own module docstring addendum.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_health import data_layout
from project_health.config import load_project, load_project_index, resolve_index_config_path
from project_health.site.release_lines_page import build_releases_context

# The one project that keeps the site root (data_layout._ROOT_PROJECT_ID,
# not imported since that name is private to that module -- this is the
# one other place epic #168 pins the same id down).
ROOT_PROJECT_ID = "cassandra"

# Peers (issue #145, D30) and the thread explorer (issue #122, D27) are a
# deliberate Cassandra-only feature (epic #168's own plan for issue #171),
# not something every subproject will eventually grow its own version of --
# unlike Conversations/Governance's per-project "shared" notice (driven by
# each project's own `shared_sources:`, config.py), there's no subproject
# config flag for this; it's a fixed product decision.
CASSANDRA_ONLY_TABS = frozenset({"peers", "conversations/threads"})
# A switcher link to a tab a target project doesn't emit falls back to that
# tab's own parent tab first (issue #171: "fallback parent tab -> project
# home if that tab isn't emitted for the target") -- only the thread
# explorer has one (Conversations); Peers has no parent tab of its own, so
# it falls straight through to "home" below.
_TAB_PARENT: dict[str, str] = {"conversations/threads": "conversations"}

# The main pipeline's own `run_id` shape (`pipeline.make_run_id`, issue #9):
# `YYYY-MM-DDTHHMMSSZ-<shortsha>`. `manifests/` can also hold manifests from
# an entirely different run family on the same shared filename convention
# (e.g. `peers/collect.py`'s own `peers-peers-<timestamp>.json`, issue
# #145) -- a plain lexicographic sort over every `*.json` stem would pick
# one of those instead of the main pipeline's own latest run (`"p" >
# "2"`), so the fallback below only ever considers a filename that matches
# this exact shape.
_RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z-[0-9a-f]+$")


def project_path_depth(root_path: str) -> int:
    """Number of path segments in a project's own `root_path` ("" -> 0,
    "p/<id>/" -> 2) -- the extra `../` hops a page under that project needs
    to reach the *site* root on top of however many it already needs to
    reach that *project's* own root (`generate.py`'s existing `base_prefix`
    convention)."""
    return len([part for part in root_path.split("/") if part])


def resolve_switcher_target_tab(tab: str, target: "DiscoveredProject") -> str:
    """Which tab a switcher link to `target` should point at, given the tab
    the *current* page is on. A non-Cassandra target never emits a
    `CASSANDRA_ONLY_TABS` tab, so a link to one of those instead targets
    its parent tab (`_TAB_PARENT`), or that project's own home page if it
    has none. Every other project always emits Home/Community/
    Conversations/Governance/Releases (issue #171), *except* Cassandra's
    own Releases tab, which keeps its pre-#171 "no data -> no page" gate --
    a switcher link to it falls back to home too when `target.
    releases_available` is `False`."""
    if tab in CASSANDRA_ONLY_TABS and not target.is_cassandra:
        return _TAB_PARENT.get(tab, "home")
    if tab == "releases" and target.is_cassandra and not target.releases_available:
        return "home"
    return tab


@dataclass(frozen=True)
class DiscoveredProject:
    """One `projects/index.yaml` entry, resolved to a renderable project
    (issue #171): its own data directory, the latest run_id found there,
    where it lands in the output tree, and which of its tabs are shared
    with (not independently computed for) the rest of the PMC -- read
    straight from that project's own `shared_sources:` (config.py, added by
    issue #169 for exactly this)."""

    id: str
    display_name: str
    root_path: str  # "" for cassandra, "p/<id>/" otherwise
    is_cassandra: bool
    # True when this project's own `shared_sources:` says the dev@/user@
    # mailing lists (`ponymail`) / governance+roster (`governance`,
    # `asf_roster`) aren't independently attributable to it -- the
    # Conversations/Governance tab then renders a short notice instead of
    # that tab's ordinary content (issue #171).
    conversations_shared: bool
    governance_shared: bool
    # Whether THIS project's own Releases tab actually has data (issue
    # #171). Irrelevant for the page itself on a non-Cassandra project
    # (always force-rendered either way, `generate.py`), but the switcher
    # (`resolve_switcher_target_tab`) needs it to avoid ever linking to
    # Cassandra's own Releases page on the rare build where Cassandra
    # itself has no release-line data collected yet.
    releases_available: bool
    data_dir: Path
    run_id: str


@dataclass(frozen=True)
class ProjectSiteContext:
    """Everything one page's render needs to know about multi-project
    identity (issue #171): its own project (`current`) and the full list of
    projects the header switcher should offer (`available`, always
    including `current` -- `generate.py` hides the switcher itself when
    this list has only one entry)."""

    current: DiscoveredProject
    available: tuple[DiscoveredProject, ...]


def resolve_latest_run_id(data_dir: str | Path) -> str | None:
    """The run_id a project's site should render, or `None` if it has no
    resolvable run yet (`project-health site`'s "every enabled project
    *with data*" contract, issue #171).

    Prefers `state/latest_run.json` (written per-project by issue #170's
    pipeline work: `run_id`/project identity/`finished_at`) when present --
    read permissively (any non-empty string `run_id` key), since that
    file's exact shape is still issue #170's own, actively-landing contract,
    not this module's. Falls back to the latest `manifests/<run_id>.json`
    file present (lexicographic `run_id` sort is chronological, issue #9's
    own `YYYY-MM-DDTHHMMSSZ-<shortsha>` format) -- the same "latest
    completed run" a project's data directory has always been able to
    answer, so Cassandra (predating issue #170's `latest_run.json`) renders
    today without it.
    """
    data_dir = Path(data_dir)
    latest_run_path = data_dir / "state" / "latest_run.json"
    if latest_run_path.is_file():
        try:
            payload: Any = json.loads(latest_run_path.read_text())
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            run_id = payload.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id

    manifests_dir = data_dir / "manifests"
    if not manifests_dir.is_dir():
        return None
    run_ids = sorted(
        path.stem for path in manifests_dir.glob("*.json") if _RUN_ID_RE.match(path.stem)
    )
    return run_ids[-1] if run_ids else None


def discover_projects(
    index_path: str | Path, data_dir_root: str | Path
) -> list[DiscoveredProject]:
    """Resolve every *enabled* entry in `projects/index.yaml` to a
    `DiscoveredProject`, skipping any that has no resolvable run yet."""
    index_path = Path(index_path)
    data_dir_root = Path(data_dir_root)
    discovered: list[DiscoveredProject] = []
    for entry in load_project_index(index_path):
        if not entry.enabled:
            continue
        project_config = load_project(resolve_index_config_path(index_path, entry))
        project_data_dir = data_layout.project_data_dir(data_dir_root, entry.id)
        run_id = resolve_latest_run_id(project_data_dir)
        if run_id is None:
            continue
        is_cassandra = entry.id == ROOT_PROJECT_ID
        shared = set(project_config.shared_sources)
        releases_available = bool(
            build_releases_context(project_data_dir, run_id).get("available")
        )
        discovered.append(
            DiscoveredProject(
                id=entry.id,
                display_name=project_config.project.display_name,
                root_path="" if is_cassandra else f"p/{entry.id}/",
                is_cassandra=is_cassandra,
                conversations_shared=(not is_cassandra) and "ponymail" in shared,
                governance_shared=(
                    (not is_cassandra)
                    and ("governance" in shared or "asf_roster" in shared)
                ),
                releases_available=releases_available,
                data_dir=project_data_dir,
                run_id=run_id,
            )
        )
    return discovered


def switcher_entries(
    available: tuple[DiscoveredProject, ...],
    current_id: str,
    tab: str,
    site_root_prefix: str,
) -> list[dict[str, Any]]:
    """The header switcher's own link list for one rendered page (issue
    #171): one entry per `available` project (including the current one,
    marked `current=True` for `aria-current`), each linking to the *same*
    tab in that project -- or its fallback (`resolve_switcher_target_tab`)
    when that project doesn't emit it. Empty when there's only one
    available project (`generate.py` hides the switcher entirely in that
    case, per the issue's own "hidden when only one project available")."""
    if len(available) <= 1:
        return []
    entries = []
    for project in available:
        target_tab = resolve_switcher_target_tab(tab, project)
        path = "index.html" if target_tab == "home" else f"{target_tab}/"
        entries.append(
            {
                "id": project.id,
                "display_name": project.display_name,
                "href": f"{site_root_prefix}{project.root_path}{path}",
                "current": project.id == current_id,
            }
        )
    return entries
