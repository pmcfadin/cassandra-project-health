"""`/releases/` page context for a non-Cassandra subproject (issue #175).

Deliberately a separate, simpler builder from `site/release_lines_page.py`'s
`build_releases_context` (Cassandra's own blocker-ladder/Jenkins/Butler
page, issue #166) -- see `collectors/subproject_releases.py`'s module
docstring for why the two shapes genuinely differ, not just by omission.
Rendered by its own template, `templates/releases_subproject.html`
(`site/generate.py` picks the template by `project_is_cassandra`), so
Cassandra's own `templates/releases.html` output is never touched by this
module at all -- the byte-identical guarantee issue #175 asks for comes
from the two paths sharing no code or template past `_render_pages`'s own
`releases_context = ...` branch.

Three independent facts, same "available" vs. "has data right now" split
every other page-context builder in this project uses:
- GA release history: read straight from the EXISTING `release` table
  (git tags, issue #135) -- grouped by major.minor line, latest first.
- Unreleased JIRA fixVersions with open/total ticket counts: `source=
  "subproject_releases"`'s own `subproject_release_version` table.
  `has_jira=False` (this project has no `issue_tracker:` section at all,
  e.g. easy-stress) is a plain config fact, passed in by the caller
  (`site/generate.py`, from `DiscoveredProject.has_jira` -- this module
  itself reads only raw tables, never project config, ARCHITECTURE.md
  §2.2) -- distinct from "has JIRA but zero currently-ticketed unreleased
  versions," which renders its own honest empty state instead of "no JIRA
  tracker."
- CI: the latest GitHub Actions run per workflow, same source's
  `ci_workflow_run` table. An empty table is itself the real "not
  tracked" fact for a repo verified to have no real CI (module docstring,
  `collectors/subproject_releases.py`) -- no separate flag needed.

D25: plain facts only, no ready/not-ready verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from project_health import storage

SOURCE = "subproject_releases"

SOURCES_AND_LIMITS = (
    "GA release dates come from this repository's own git tags. Unreleased JIRA versions "
    "show the open/total ticket count JIRA itself reports for that fixVersion; click through "
    "to JIRA for the ticket list. Continuous integration shows the most recent GitHub Actions "
    "run per workflow on the repository's default branch, or \"not tracked\" when this "
    "repository has no workflow of its own running there. This page states what the public "
    "record shows; it never judges whether a release is ready."
)


def _dedupe_latest(
    rows: list[dict[str, Any]], key_fields: tuple[str, ...], tiebreak: str
) -> list[dict[str, Any]]:
    """Same dedupe-at-read rule every raw-table-backed page context in this
    project uses (`release_lines_page._dedupe_latest`'s own docstring)."""
    best: dict[Any, dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[f] for f in key_fields)
        current = best.get(key)
        if current is None or row[tiebreak] > current[tiebreak]:
            best[key] = row
    return list(best.values())


def _line_sort_key(line: str) -> tuple[int, ...]:
    """`"4.19"` -> `(4, 19)`, `"9"` (easy-stress's bare-major lines) ->
    `(9,)` -- numeric, not lexicographic, so `"10.0"` sorts after
    `"9.0"`. Non-numeric parts (shouldn't occur in `release.major_minor`,
    `collectors/release.py`'s own `_major_minor`) fall back to `0` rather
    than raising."""
    parts = []
    for part in line.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def build_subproject_releases_context(
    data_dir: str | Path,
    run_id: str | None = None,
    *,
    has_jira: bool = True,
) -> dict[str, Any]:
    """Build a non-Cassandra subproject's `/releases/` page context.

    Unlike `release_lines_page.build_releases_context`, this never returns
    `{"available": False}` outright -- `site/generate.py` force-renders
    every subproject's Releases page regardless (issue #171's own "every
    project emits a Releases page" rule), so `available` here just drives
    which empty-state copy the template shows, same meaning as everywhere
    else in this project.
    """
    data_dir = Path(data_dir)

    release_rows = _dedupe_latest(
        storage.read_table(data_dir, "release", "release").to_pylist(),
        ("release_id",),
        "collected_at",
    )
    by_line: dict[str, list[dict[str, Any]]] = {}
    for row in release_rows:
        by_line.setdefault(row["major_minor"], []).append(row)
    ga_lines = []
    for line in sorted(by_line, key=_line_sort_key, reverse=True):
        versions = sorted(by_line[line], key=lambda r: r["release_date"])
        ga_lines.append(
            {
                "line": line,
                "latest": {
                    "version": versions[-1]["version"],
                    "release_date": versions[-1]["release_date"],
                },
                "history": [
                    {"version": v["version"], "release_date": v["release_date"]}
                    for v in reversed(versions)
                ],
            }
        )
    ga_available = bool(release_rows)

    jira_versions: list[dict[str, Any]] = []
    if has_jira:
        jira_rows = _dedupe_latest(
            storage.read_table(data_dir, SOURCE, "subproject_release_version").to_pylist(),
            ("version_id",),
            "collected_at",
        )
        jira_versions = [
            {
                "name": row["name"],
                "description": row.get("description"),
                "release_date": row.get("release_date"),
                "open_count": row["open_count"],
                "total_count": row["total_count"],
                "issues_url": row["issues_url"],
            }
            for row in sorted(jira_rows, key=lambda r: r["name"])
        ]

    ci_rows = _dedupe_latest(
        storage.read_table(data_dir, SOURCE, "ci_workflow_run").to_pylist(),
        ("workflow_name",),
        "collected_at",
    )
    ci_workflows = [
        {
            "workflow_name": row["workflow_name"],
            "status": row.get("status"),
            "conclusion": row.get("conclusion"),
            "run_number": row.get("run_number"),
            "html_url": row["html_url"],
            "run_started_at": (
                row["run_started_at"].date().isoformat() if row.get("run_started_at") else None
            ),
        }
        for row in sorted(ci_rows, key=lambda r: r["workflow_name"])
    ]
    ci_tracked = bool(ci_workflows)

    available = ga_available or bool(jira_versions) or ci_tracked

    return {
        "available": available,
        "ga_available": ga_available,
        "ga_lines": ga_lines,
        "has_jira": has_jira,
        "jira_versions": jira_versions,
        "ci_tracked": ci_tracked,
        "ci_workflows": ci_workflows,
        "sources_and_limits": SOURCES_AND_LIMITS,
    }
