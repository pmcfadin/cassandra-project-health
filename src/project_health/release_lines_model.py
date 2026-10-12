"""Release-line view model for the Releases tab (issue #166).

Pure Python, no HTTP, no metrics-engine dependency (matching `security`'s
own "this source registers no metric_value rows" precedent) -- it just
reshapes the four raw tables `collectors/release_lines.py` produces, plus
the existing `release` table (GA git tags, issue #135), into one
`ReleaseLineView` per tracked line for `site/generate.py` to render.

Every field here is a plain, neutral fact (D25): a count, a date, a
verbatim-quoted description, a CI system's own reported string. Nothing
here computes or renders a ready/not-ready verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# Lines with a published branch CI workflow but no JIRA-tracked release
# line of their own (module docstring, issue #166: "+ trunk (CI only)").
CI_ONLY_LINES: tuple[str, ...] = ("trunk",)


@dataclass(frozen=True)
class BlockerIssueView:
    issue_key: str
    summary: str | None
    status: str | None
    priority: str | None
    assignee_raw: str | None  # None means unassigned
    updated_at: datetime | None

    @property
    def unassigned(self) -> bool:
        return not self.assignee_raw


@dataclass(frozen=True)
class LadderStepView:
    """One blocker-ladder step, the series backlog, or the bare GA target --
    all three share this shape (a version + its open issues)."""

    version_name: str
    description: str | None
    stage_label: str | None  # e.g. "alpha", "beta", "rc", None for series/GA
    # Issue #166 orchestrator review of PR #167: a short label for what
    # this step's own open issues block (e.g. "beta1", "rc1", "GA"),
    # carried straight from `collectors.release_lines.LadderStep.
    # target_label` -- `None` for the series backlog / GA target / next-
    # release steps, which don't have a "points to" target of their own.
    target_label: str | None = None
    issues: tuple[BlockerIssueView, ...] = ()

    @property
    def open_count(self) -> int:
        return len(self.issues)

    @property
    def unassigned_count(self) -> int:
        return sum(1 for i in self.issues if i.unassigned)

    @property
    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for i in self.issues:
            counts[i.status or "unknown"] = counts.get(i.status or "unknown", 0) + 1
        return counts


@dataclass(frozen=True)
class CiTrendPointView:
    build_label: str
    build_timestamp: datetime
    test_failures: int | None
    test_runs: int | None
    duration_minutes: float | None


@dataclass(frozen=True)
class CiView:
    """CI state for one branch line. `None` fields mean that sub-source was
    unavailable (collector outage with no prior partition, or this line has
    no matching Jenkins job / Butler workflow at all) -- the site renders
    'unavailable' for whichever half is `None`, never fabricating a value."""

    job_name: str | None
    color: str | None
    last_build_number: int | None
    last_build_result: str | None
    last_build_timestamp: datetime | None
    trend: tuple[CiTrendPointView, ...] = ()

    @property
    def latest_trend_point(self) -> CiTrendPointView | None:
        return self.trend[-1] if self.trend else None

    @property
    def jenkins_available(self) -> bool:
        return self.job_name is not None

    @property
    def butler_available(self) -> bool:
        return len(self.trend) > 0


@dataclass(frozen=True)
class ReleasedVersionView:
    version: str
    release_date: object  # datetime.date | None


@dataclass(frozen=True)
class ReleaseLineView:
    line: str  # e.g. "6.0", or "trunk" for the CI-only pseudo-line
    latest_release: ReleasedVersionView | None
    next_target: str | None
    # Every unreleased, undescribed, numbered version under this line
    # (`classify_versions`'s "next_versions") -- `next_target` above is
    # just this tuple's first (lowest-named) entry, kept as its own field
    # since in practice there is normally exactly one; this tuple is for
    # callers (the /releases/ page) that want to show every one, not just
    # the first.
    next_versions: tuple[str, ...] = ()
    # issue #166 orchestrator review of PR #167: the next-release version's
    # own name + open-issue count, same shape as a ladder step, for the
    # summary table's "Next release" column and the section's own listing
    # of its open tickets. `None` when there's no unreleased next version
    # tracked for this line. (`next_target`/`next_versions` above are kept
    # unchanged for any other caller that just wants the name(s).)
    next_target_step: LadderStepView | None = None
    ladder: tuple[LadderStepView, ...] = ()
    series: LadderStepView | None = None
    ga_target: LadderStepView | None = None
    timeline: tuple[ReleasedVersionView, ...] = ()
    ci: CiView = field(default_factory=lambda: CiView(None, None, None, None, None))
    jira_available: bool = True

    @property
    def total_open_blockers(self) -> int:
        """Sum of the ladder's own open-issue counts only (issue #166
        orchestrator review of PR #167): only a ladder placeholder's
        tickets are "blockers" in the project's own sense (its description
        literally says "Placeholder for blockers to <X>") -- a ticket
        merely targeted at the bare GA version (`ga_target`) is a planned
        change for that release, not something blocking reaching it, and
        is kept out of this count (shown separately, as "Targeted at
        <line>")."""
        return sum(step.open_count for step in self.ladder)


def _group_blocker_issues(
    blocker_rows: list[dict], version_name: str
) -> tuple[BlockerIssueView, ...]:
    return tuple(
        BlockerIssueView(
            issue_key=row["issue_key"],
            summary=row.get("summary"),
            status=row.get("status"),
            priority=row.get("priority"),
            assignee_raw=row.get("assignee_raw"),
            updated_at=row.get("updated_at"),
        )
        for row in blocker_rows
        if row["version_name"] == version_name
    )


def build_release_line_model(
    *,
    active_lines: list[str],
    jira_version_rows: list[dict],
    blocker_issue_rows: list[dict],
    ci_job_rows: list[dict],
    ci_trend_rows: list[dict],
    release_rows: list[dict],
    jira_available: bool,
) -> list[ReleaseLineView]:
    """Build one `ReleaseLineView` per line in `active_lines`, plus one more
    for each of `CI_ONLY_LINES` (trunk: CI state only, no JIRA line).

    `jira_available` is `False` only when `jira_version_rows` is empty
    because the JIRA source has *never* successfully collected (module
    docstring in `collectors/release_lines.py`/`pipeline._collect_release_
    lines`) -- the caller passes this through rather than this function
    inferring it from row counts, since an empty result can also mean "this
    line genuinely has no tracked versions yet," which is a different, true
    fact, not an outage.
    """
    from project_health.collectors.release_lines import classify_versions, order_ladder

    def _to_version_info(row: dict):
        from project_health.collectors.release_lines import JiraVersionInfo

        return JiraVersionInfo(
            version_id=row["version_id"],
            name=row["name"],
            description=row.get("description"),
            released=row.get("released", False),
            release_date=row.get("release_date").isoformat() if row.get("release_date") else None,
        )

    all_version_infos = [_to_version_info(row) for row in jira_version_rows]

    ci_by_branch = {row["branch_line"]: row for row in ci_job_rows}
    trend_by_branch: dict[str, list[dict]] = {}
    for row in ci_trend_rows:
        trend_by_branch.setdefault(row["branch_line"], []).append(row)

    def _ci_view(branch_line: str) -> CiView:
        job = ci_by_branch.get(branch_line)
        points = sorted(trend_by_branch.get(branch_line, []), key=lambda r: r["build_timestamp"])
        trend = tuple(
            CiTrendPointView(
                build_label=p["build_label"],
                build_timestamp=p["build_timestamp"],
                test_failures=p.get("test_failures"),
                test_runs=p.get("test_runs"),
                duration_minutes=p.get("duration_minutes"),
            )
            for p in points
        )
        if job is None:
            return CiView(None, None, None, None, None, trend=trend)
        return CiView(
            job_name=job["job_name"],
            color=job.get("color"),
            last_build_number=job.get("last_build_number"),
            last_build_result=job.get("last_build_result"),
            last_build_timestamp=job.get("last_build_timestamp"),
            trend=trend,
        )

    releases_by_line: dict[str, list[dict]] = {}
    for row in release_rows:
        releases_by_line.setdefault(row["major_minor"], []).append(row)

    def _timeline(line: str, released_prereleases: list) -> tuple[ReleasedVersionView, ...]:
        """GA history (`release`, git tags) unioned with pre-GA release
        history (`released_prereleases`, JIRA's own "released_versions" --
        issue #166 orchestrator review of PR #167: a line that hasn't
        reached GA yet has no git-tag row at all). The two are disjoint by
        construction (`classify_versions`' own docstring), so no dedup is
        needed beyond dropping a row with no date at all (can't be placed
        on the timeline -- omitted rather than fabricated, same "collect
        imperfectly but honestly" rule `collectors/release.py` documents
        for its own archive cross-check)."""
        rows = [
            ReleasedVersionView(version=r["version"], release_date=r["release_date"])
            for r in releases_by_line.get(line, [])
        ]
        rows += [
            # `v.release_date` is JIRA's own ISO date *string*; the
            # `release` rows above carry a `datetime.date` (from the
            # table's `pa.date32()` column) -- normalize to `date` here so
            # the two are comparable in the single `sort()` below.
            ReleasedVersionView(version=v.name, release_date=date.fromisoformat(v.release_date))
            for v in released_prereleases
            if v.release_date
        ]
        rows.sort(key=lambda r: r.release_date)
        return tuple(rows)

    empty_classified = {
        "ladder_placeholders": [],
        "series_placeholder": None,
        "ga_target": None,
        "next_versions": [],
        "released_versions": [],
    }

    views: list[ReleaseLineView] = []
    for line in active_lines:
        classified = classify_versions(all_version_infos, line) if jira_available else dict(
            empty_classified
        )
        ladder_steps = order_ladder(classified["ladder_placeholders"])  # type: ignore[arg-type]
        ladder_views = tuple(
            LadderStepView(
                version_name=step.version.name,
                description=step.version.description,
                stage_label=step.stage,
                target_label=step.target_label,
                issues=_group_blocker_issues(blocker_issue_rows, step.version.name),
            )
            for step in ladder_steps
        )
        series_view = None
        if classified["series_placeholder"] is not None:
            sv = classified["series_placeholder"]
            series_view = LadderStepView(
                version_name=sv.name,
                description=sv.description,
                stage_label="series",
                issues=_group_blocker_issues(blocker_issue_rows, sv.name),
            )
        ga_view = None
        if classified["ga_target"] is not None:
            gv = classified["ga_target"]
            ga_view = LadderStepView(
                version_name=gv.name,
                description=gv.description,
                stage_label="ga_target",
                issues=_group_blocker_issues(blocker_issue_rows, gv.name),
            )
        next_version_infos = classified["next_versions"]  # type: ignore[assignment]
        next_target = next_version_infos[0].name if next_version_infos else None
        next_target_step = None
        if next_version_infos:
            nv = next_version_infos[0]
            next_target_step = LadderStepView(
                version_name=nv.name,
                description=nv.description,
                stage_label="next",
                issues=_group_blocker_issues(blocker_issue_rows, nv.name),
            )

        timeline = _timeline(line, classified["released_versions"])  # type: ignore[arg-type]
        latest_release = timeline[-1] if timeline else None

        views.append(
            ReleaseLineView(
                line=line,
                latest_release=latest_release,
                next_target=next_target,
                next_versions=tuple(v.name for v in next_version_infos),
                next_target_step=next_target_step,
                ladder=ladder_views,
                series=series_view,
                ga_target=ga_view,
                timeline=timeline,
                ci=_ci_view(line),
                jira_available=jira_available,
            )
        )

    for line in CI_ONLY_LINES:
        views.append(
            ReleaseLineView(
                line=line,
                latest_release=None,
                next_target=None,
                ci=_ci_view(line),
                jira_available=False,
            )
        )

    return views
