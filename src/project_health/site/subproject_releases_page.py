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
  Only the `GA_LINES_VISIBLE` most recent lines render inline; the rest
  go under an "Older release lines (N)" `<details>` (issue #175 review,
  PR #185 round 1: java-driver alone has 37 lines).
- Unreleased JIRA fixVersions with open/total ticket counts: `source=
  "subproject_releases"`'s own `subproject_release_version` table.
  `has_jira=False` (this project has no `issue_tracker:` section at all,
  e.g. easy-stress) is a plain config fact, passed in by the caller
  (`site/generate.py`, from `DiscoveredProject.has_jira` -- this module
  itself reads only raw tables, never project config, ARCHITECTURE.md
  §2.2) -- distinct from "has JIRA but zero currently-ticketed unreleased
  versions," which renders its own honest empty state instead of "no JIRA
  tracker." Each one is cross-referenced against the SAME `release` table
  above by exact version-string match: a JIRA version JIRA itself still
  calls unreleased, but whose own git tag has already shipped (issue #175
  review, PR #185 round 1 -- verified live for java-driver's
  3.13.0/4.18.0/4.19.4 and nodejs-driver's 4.10.0), shows that tag instead
  of reading as a contradiction next to the GA table.
- CI: the latest GitHub Actions run per workflow, same source's
  `ci_workflow_run` table. An empty table means no run of any of this
  repo's OWN workflows (`.github/workflows/...`) was found on the default
  branch -- not necessarily that no such workflow file exists at all
  (issue #175 review, PR #185 round 1: java-driver's own
  `publish-docs-gh-pages` workflow exists but has 0 runs, ever, on any
  branch -- verified live) -- the template's copy says exactly that, not
  "no workflow of its own," and never guesses this project's CI runs
  nowhere; it may simply run elsewhere (e.g. ASF Jenkins), untracked here.

D25: plain facts only, no ready/not-ready verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from project_health import storage

SOURCE = "subproject_releases"

# issue #175 review (PR #185, round 1): a repo's GA line history can run
# long (java-driver: 37 lines) -- the template shows only the most recent
# `GA_LINES_VISIBLE` lines inline, with the rest behind a `<details>`
# "Older release lines (N)" (same plain, unstyled `<details>`/`<summary>`
# pattern `templates/releases.html`'s own "Patch backlog" section already
# uses -- not the big `.page-section` accordion box).
GA_LINES_VISIBLE = 8

SOURCES_AND_LIMITS = (
    "GA release dates come from this repository's own git tags. Unreleased JIRA versions "
    "show the open/total ticket count JIRA itself reports for that fixVersion; click through "
    "to JIRA for the ticket list. A version already covered by a git tag shows that tag "
    "instead, sorted after the versions with no tag -- JIRA's own \"released\" flag simply "
    "wasn't set on it. Continuous integration shows the most recent run of each GitHub Actions "
    "workflow defined in this repository's own .github/workflows/ directory, on the default "
    "branch; GitHub's own auto-generated workflows (Dependabot updates, Copilot code review) "
    "are never counted as this project's CI. \"No GitHub Actions CI run found\" means none of "
    "this repository's own workflows has a recorded run there -- this project's real CI may "
    "run elsewhere (e.g. ASF Jenkins), which this page does not track. This page states what "
    "the public record shows; it never judges whether a release is ready."
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
    # issue #175 review (PR #185, round 1): verified live 2026-10-12 --
    # java-driver's "3.13.0"/"4.18.0"/"4.19.4" and nodejs-driver's "4.10.0"
    # are all JIRA fixVersions JIRA itself still lists as unreleased even
    # though each one's own git tag has already shipped (JIRA's own
    # "released" flag just was never flipped) -- showing them as plain
    # "unreleased" next to the GA table directly above reads as
    # contradictory. `tag_by_version` cross-references every unreleased
    # JIRA version's own `name` against this SAME `release` table's
    # `version` column (exact string match -- both are the repo's own
    # normalized version string, e.g. "4.19.4") so the JIRA table can show
    # the real git tag fact instead of silently repeating the
    # contradiction.
    tag_by_version = {row["version"]: row for row in release_rows}

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
                "git_tag": (
                    {
                        "version": tag_by_version[row["name"]]["version"],
                        "release_date": tag_by_version[row["name"]]["release_date"],
                    }
                    if row["name"] in tag_by_version
                    else None
                ),
            }
            for row in jira_rows
        ]
        # Versions with no git tag yet (still genuinely upcoming) sort
        # first; the already-tagged ones (JIRA just hasn't been updated)
        # sort after, each group alphabetically by name.
        jira_versions.sort(key=lambda v: (v["git_tag"] is not None, v["name"]))

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
        # issue #175 review (PR #185, round 1): split once here (rather
        # than slicing in the template) so this split is itself directly
        # testable. `ga_lines` (above, unchanged) stays the FULL set for
        # any other caller; the template renders `ga_lines_recent` inline
        # and `ga_lines_older` inside the "Older release lines (N)"
        # `<details>`, only when there is at least one.
        "ga_lines_recent": ga_lines[:GA_LINES_VISIBLE],
        "ga_lines_older": ga_lines[GA_LINES_VISIBLE:],
        "has_jira": has_jira,
        "jira_versions": jira_versions,
        "ci_tracked": ci_tracked,
        "ci_workflows": ci_workflows,
        "sources_and_limits": SOURCES_AND_LIMITS,
    }
