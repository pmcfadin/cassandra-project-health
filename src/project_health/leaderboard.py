"""Contributor leaderboard (D19, issue #56).

D19 amends D2 rule 7 ("no ranking people") to allow a ranked top-N
contributor leaderboard, with explicit safeguards:

- **One activity type at a time.** Three separate top-N lists --
  `commits` (by author), `reviews` (commit-trailer reviewers, the same
  primary source `metrics.engine._reviewer_hhi` uses -- METRICS.md §0.4's
  denser, non-double-counted signal), and `jira_issues_resolved` (by
  assignee). They are never blended into a single "contribution score"
  (D19, D2 rule 7) and never rank or list anyone by a Phase 2 *classified*
  label (D19's last bullet; this module only ever reads Phase 1
  deterministic fact tables -- `contribution_event`, `review_event`,
  `issue` -- never `classification`).
- **A stated window.** Trailing 12 *completed* calendar months ending at
  the last completed month before `as_of` (D5/METRICS.md's own "completed
  months only" rule, reused here via `metrics.windows` -- the same public
  helpers every M0 metric's trailing-12m window uses).
- **Same identity resolution as the metrics** (D19): this module takes the
  same `identity_link` (`normalize/identity.py`, `identity_overrides.yaml`
  folded in by the pipeline before this is ever called) and
  `affiliation_period` (D6, issue #52) tables `metrics/engine.py`
  consumes, and applies the same bot-exclusion population rule
  (METRICS.md §0.5) and non-merge-commit-only counting (METRICS.md §0.3 --
  `contribution_event` rows are already `git log --no-merges` at the
  collector, so nothing extra is needed here for that).
- **Known identity-resolution limits, shown.** `LeaderboardResult` and the
  site page built from it always carry a fixed, human-readable note about
  the same handles-vs-full-names / unmerged-alias limitations
  `metrics/registry.py`'s `unique_reviewers_monthly` description already
  discloses for the underlying data (D2 rule 5), plus a link to request a
  correction via `identity_overrides.yaml` (D19's third bullet).

Deliberately **not** a `metrics.registry`/`METRIC_IDS` metric (issue #56's
own instruction, overriding this epic's generic per-issue boilerplate about
"metrics registered v1.0"): a top-N ranking is a table, not a windowed
rate/ratio/concentration statistic, and D19 explicitly forbids collapsing
the three lists into one blended, single-number score the way every other
`metric_value` row is one number. Keeping it out of `METRIC_IDS` means a
project with no leaderboard data yet (or a run where this computation
fails) can never trip `pipeline.py`'s "a *registered* metric produced zero
rows" `metrics_missing` check (issue #24) and mark the whole M0 run
`degraded` over what is, by design, an optional, additive table -- the same
scope boundary `governance/metrics.py`'s own module docstring documents for
governance's compliance metrics, applied here for the same reason.

This module is deliberately self-contained (a small, local duplicate of
`metrics.engine`'s bot-identifier and organization-resolution SQL, rather
than an import from it) so it has no coupling to `metrics/engine.py`,
`pipeline.py`, or `site/generate.py` while other work concurrently edits
those three files (issue #56's own instruction) -- the same "duplicate a
few lines rather than import across a module boundary that might move"
precedent `normalize/affiliation.py`'s `_add_months` already sets for this
project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import duckdb
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.metrics.windows import add_months, month_end, month_start, trailing_12m_window
from project_health.normalize.affiliation import UNKNOWN_ORG
from project_health.schema import CODE_COMMIT, get_schema, validate

DEFINITION_VERSION = "1.0"

ACTIVITY_COMMITS = "commits"
ACTIVITY_REVIEWS = "reviews"
ACTIVITY_JIRA_RESOLVED = "jira_issues_resolved"

# Order matters: this is the order lists render on the site and appear in
# `LeaderboardResult.lists`.
ACTIVITY_TYPES: tuple[str, ...] = (ACTIVITY_COMMITS, ACTIVITY_REVIEWS, ACTIVITY_JIRA_RESOLVED)

ACTIVITY_LABELS: dict[str, str] = {
    ACTIVITY_COMMITS: "Commits",
    ACTIVITY_REVIEWS: "Reviews",
    ACTIVITY_JIRA_RESOLVED: "JIRA issues resolved",
}

# How many people each activity type's list shows (D19: "top-N"). Chosen to
# be generous enough to be useful without turning into a full roster --
# consistent with issue #56's "complexity: S" scope; revisit alongside a
# future config knob if a project ever wants a different N.
TOP_N = 25

# D2 rule 5 / D19's own "known identity-resolution limits ... shown next to
# the list": the same class of caveat `metrics/registry.py` already
# discloses for `unique_reviewers_monthly` (naive M0 identity resolution
# never cross-type-merges a git_name/commit-trailer identity with the same
# person's git_email/jira_username identity), reused verbatim here since the
# leaderboard draws on exactly those same raw identifier types.
IDENTITY_LIMITATIONS_NOTE = (
    "Counts are grouped by resolved identity (normalize/identity.py), not by human being. "
    "M0's identity resolution only merges an exact-matching git email or JIRA username; a "
    "commit-trailer reviewer name (a bare handle or display name, not an email) is never "
    "automatically linked to that same person's git email or JIRA username, so one "
    "contributor active under more than one raw identifier can appear more than once, or "
    "under a handle rather than their full name. Manual merges are reviewed, evidenced "
    "entries in identity_overrides.yaml -- see the corrections link below to request one."
)

# `config.bot_patterns.field` -> the `identity_link.source_type` it screens
# (see `normalize/identity.py` RAW_TYPES). A local, minimal duplicate of
# `metrics.engine._FIELD_TO_RAW_TYPE` (module docstring: intentionally not
# imported).
_FIELD_TO_RAW_TYPE = {
    "git_author_email": "git_email",
    "jira_username": "jira_username",
}

_BOT_IDENTIFIER_SCHEMA = pa.schema(
    [
        pa.field("raw_type", pa.string(), nullable=False),
        pa.field("raw_value", pa.string(), nullable=False),
    ]
)


@dataclass(frozen=True)
class LeaderboardEntry:
    """One ranked row in one activity type's top-N list."""

    rank: int
    identity_id: str
    display_name: str | None
    organization: str
    count: int


@dataclass(frozen=True)
class LeaderboardList:
    """One activity type's full top-N list plus its own audit trail."""

    activity_type: str
    label: str
    window_start: date
    window_end: date
    entries: list[LeaderboardEntry]
    # Total qualifying population size in the window (distinct identities
    # with >= 1 qualifying event), regardless of how many made the top N --
    # so a reader can see e.g. "top 25 of 40" rather than mistaking the list
    # for the whole population (D2 rule 3, auditability).
    population_size: int
    total_count: int

    @property
    def has_data(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True)
class LeaderboardResult:
    run_id: str
    definition_version: str
    computed_at: datetime
    lists: dict[str, LeaderboardList]
    identity_limitations_note: str
    table: pa.Table


def _table_or_empty(tables: dict[str, pa.Table], name: str) -> pa.Table:
    return tables[name] if name in tables else get_schema(name).empty_table()


def _connect(tables: dict[str, pa.Table]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    table_names = (
        "contribution_event",
        "review_event",
        "issue",
        "identity_link",
        "affiliation_period",
    )
    for name in table_names:
        con.register(name, _table_or_empty(tables, name))
    con.execute(
        """
        CREATE OR REPLACE VIEW resolved_identity AS
        SELECT source_type, source_value, identity_id
        FROM (
            SELECT
                source_type, source_value, identity_id,
                ROW_NUMBER() OVER (
                    PARTITION BY source_type, source_value
                    ORDER BY CASE WHEN linked_by LIKE 'manual:%' THEN 0 ELSE 1 END, identity_id
                ) AS rn
            FROM identity_link
        )
        WHERE rn = 1
        """
    )
    return con


def _bot_identifiers(con: duckdb.DuckDBPyConnection, config: ProjectConfig) -> pa.Table:
    """Every raw `(type, value)` identifier the leaderboard should exclude as
    a bot (METRICS.md §0.5), scoped to only the raw identifiers this
    module's three lists actually use (commit author, commit-trailer
    reviewer, JIRA assignee)."""
    patterns = [
        (pattern.field, re.compile(pattern.regex))
        for pattern in config.bot_patterns
        if pattern.field in _FIELD_TO_RAW_TYPE
    ]
    if not patterns:
        return _BOT_IDENTIFIER_SCHEMA.empty_table()

    distinct = con.execute(
        """
        SELECT DISTINCT author_raw_type AS raw_type, author_raw_value AS raw_value
        FROM contribution_event
        UNION
        SELECT DISTINCT reviewer_raw_type, reviewer_raw_value FROM review_event
        UNION
        SELECT DISTINCT 'jira_username', assignee_raw FROM issue WHERE assignee_raw IS NOT NULL
        """
    ).fetchall()

    rows = []
    for raw_type, raw_value in distinct:
        if raw_value is None:
            continue
        for field, pattern in patterns:
            if _FIELD_TO_RAW_TYPE[field] == raw_type and pattern.search(raw_value):
                rows.append({"raw_type": raw_type, "raw_value": raw_value})
                break
    if not rows:
        return _BOT_IDENTIFIER_SCHEMA.empty_table()
    return pa.Table.from_pylist(rows, schema=_BOT_IDENTIFIER_SCHEMA)


def _leaderboard_window(as_of: date) -> tuple[date, date]:
    """Trailing 12 completed calendar months ending at the last completed
    month before `as_of` (D19: "a stated window (e.g. trailing 12
    months)"; D5: completed months only) -- reuses the same public
    `metrics.windows` helpers every M0 trailing-12m metric window uses."""
    last_completed_month_start = add_months(month_start(as_of), -1)
    window_end = month_end(last_completed_month_start)
    return trailing_12m_window(window_end)


def _display_names(person_identity: pa.Table) -> dict[str, str | None]:
    if person_identity.num_rows == 0:
        return {}
    return dict(
        zip(
            person_identity.column("identity_id").to_pylist(),
            person_identity.column("display_name").to_pylist(),
            strict=True,
        )
    )


def _fallback_identifiers(identity_link: pa.Table) -> dict[str, str]:
    """`{identity_id: a representative raw identifier value}`, used when
    `person_identity` has no `display_name` for an identity (M0's naive
    resolver only ever fills a display name from a collector-attached
    `display_name` on the raw identifier -- `normalize/identity.py`'s
    `_canonical_display_name`; a bare JIRA username or git email with no
    accompanying display name never gets one, which is common for
    `jira_username`-only identities since JIRA assignee/reporter fields
    carry no separate display-name column in M0's `issue` schema).

    Showing this raw identifier (e.g. a JIRA username) is strictly more
    useful than an opaque `identity_id` UUID fragment: it is the person's
    own real, public identifier -- just not necessarily their full name,
    exactly the handles-vs-full-names gap `IDENTITY_LIMITATIONS_NOTE`
    already discloses next to every list. Deterministic (lexicographically
    smallest `(source_type, source_value)` pair per identity), matching
    `normalize.identity._canonical_display_name`'s own "min() for
    order-independence" convention.
    """
    if identity_link.num_rows == 0:
        return {}
    best: dict[str, tuple[str, str]] = {}
    for row in identity_link.to_pylist():
        identity_id = row["identity_id"]
        candidate = (row["source_type"], row["source_value"])
        if identity_id not in best or candidate < best[identity_id]:
            best[identity_id] = candidate
    return {identity_id: value for identity_id, (_type, value) in best.items()}


def _organizations_as_of(
    con: duckdb.DuckDBPyConnection, identity_ids: list[str], as_of_date: date
) -> dict[str, str]:
    """`{identity_id: organization}` for the affiliation effective as of
    `as_of_date` (the window's own end date), one row per identity that has
    any covering `affiliation_period` row -- same curated > email_domain >
    github_company priority `metrics.engine._organization_commit_counts`
    applies per commit (D6), applied once per person here since a
    leaderboard entry is one row per person, not per event. An identity with
    no covering row is left out of the returned mapping; callers treat that
    as `unknown` (D6: never guessed)."""
    if not identity_ids:
        return {}
    rows = con.execute(
        """
        SELECT identity_id, organization
        FROM (
            SELECT
                identity_id, organization,
                ROW_NUMBER() OVER (
                    PARTITION BY identity_id
                    ORDER BY
                        CASE source
                            WHEN 'curated' THEN 0
                            WHEN 'email_domain' THEN 1
                            WHEN 'github_company' THEN 2
                            ELSE 3
                        END,
                        organization
                ) AS rn
            FROM affiliation_period
            WHERE identity_id IN (SELECT UNNEST(?))
              AND (effective_from IS NULL OR ? >= effective_from)
              AND (effective_to IS NULL OR ? < effective_to)
        )
        WHERE rn = 1
        """,
        [identity_ids, as_of_date, as_of_date],
    ).fetchall()
    return dict(rows)


def _rank_credits(
    con: duckdb.DuckDBPyConnection,
    *,
    activity_type: str,
    sql: str,
    params: list,
    window_start: date,
    window_end: date,
    display_names: dict[str, str | None],
) -> LeaderboardList:
    rows = con.execute(sql, params).fetchall()  # [(identity_id, count), ...] desc, tie-broken
    population_size = len(rows)
    total_count = sum(count for _identity_id, count in rows)

    top_rows = rows[:TOP_N]
    orgs = _organizations_as_of(con, [identity_id for identity_id, _count in top_rows], window_end)

    entries = [
        LeaderboardEntry(
            rank=rank,
            identity_id=identity_id,
            display_name=display_names.get(identity_id),
            organization=orgs.get(identity_id, UNKNOWN_ORG),
            count=count,
        )
        for rank, (identity_id, count) in enumerate(top_rows, start=1)
    ]
    return LeaderboardList(
        activity_type=activity_type,
        label=ACTIVITY_LABELS[activity_type],
        window_start=window_start,
        window_end=window_end,
        entries=entries,
        population_size=population_size,
        total_count=total_count,
    )


def _commits_list(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date, display_names: dict
) -> LeaderboardList:
    sql = """
        SELECT ri.identity_id AS identity_id, COUNT(*) AS commits
        FROM contribution_event ce
        JOIN resolved_identity ri
            ON ri.source_type = ce.author_raw_type AND ri.source_value = ce.author_raw_value
        LEFT JOIN bot_identifier bi
            ON bi.raw_type = ce.author_raw_type AND bi.raw_value = ce.author_raw_value
        WHERE ce.event_type = ?
          AND bi.raw_value IS NULL
          AND ce.occurred_at::DATE >= ? AND ce.occurred_at::DATE <= ?
        GROUP BY 1
        ORDER BY commits DESC, identity_id ASC
    """
    return _rank_credits(
        con,
        activity_type=ACTIVITY_COMMITS,
        sql=sql,
        params=[CODE_COMMIT, window_start, window_end],
        window_start=window_start,
        window_end=window_end,
        display_names=display_names,
    )


def _reviews_list(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date, display_names: dict
) -> LeaderboardList:
    """Commit-trailer reviewer credits only (D19: "reviews (commit-trailer
    reviewers)"), matching `metrics.engine._reviewer_hhi`'s own primary
    source since fixup cycle 1 -- crediting both commit_trailer and
    jira_field would double-count a single review under M0's naive,
    non-cross-type identity resolution (METRICS.md §0.4)."""
    sql = """
        SELECT ri.identity_id AS identity_id, COUNT(*) AS credits
        FROM review_event re
        JOIN resolved_identity ri
            ON ri.source_type = re.reviewer_raw_type AND ri.source_value = re.reviewer_raw_value
        LEFT JOIN bot_identifier bi
            ON bi.raw_type = re.reviewer_raw_type AND bi.raw_value = re.reviewer_raw_value
        WHERE re.source = 'commit_trailer'
          AND bi.raw_value IS NULL
          AND re.occurred_at::DATE >= ? AND re.occurred_at::DATE <= ?
        GROUP BY 1
        ORDER BY credits DESC, identity_id ASC
    """
    return _rank_credits(
        con,
        activity_type=ACTIVITY_REVIEWS,
        sql=sql,
        params=[window_start, window_end],
        window_start=window_start,
        window_end=window_end,
        display_names=display_names,
    )


def _jira_resolved_list(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date, display_names: dict
) -> LeaderboardList:
    """JIRA issues resolved, by assignee (D19), counting an issue in the
    window it was *resolved* in (`resolved_at`), not created or updated."""
    sql = """
        SELECT ri.identity_id AS identity_id, COUNT(*) AS issues
        FROM issue i
        JOIN resolved_identity ri
            ON ri.source_type = 'jira_username' AND ri.source_value = i.assignee_raw
        LEFT JOIN bot_identifier bi
            ON bi.raw_type = 'jira_username' AND bi.raw_value = i.assignee_raw
        WHERE i.assignee_raw IS NOT NULL
          AND i.resolved_at IS NOT NULL
          AND bi.raw_value IS NULL
          AND i.resolved_at::DATE >= ? AND i.resolved_at::DATE <= ?
        GROUP BY 1
        ORDER BY issues DESC, identity_id ASC
    """
    return _rank_credits(
        con,
        activity_type=ACTIVITY_JIRA_RESOLVED,
        sql=sql,
        params=[window_start, window_end],
        window_start=window_start,
        window_end=window_end,
        display_names=display_names,
    )


def _to_table(lists: dict[str, LeaderboardList], *, run_id: str, computed_at: datetime) -> pa.Table:
    rows = []
    for leaderboard_list in lists.values():
        for entry in leaderboard_list.entries:
            rows.append(
                {
                    "run_id": run_id,
                    "activity_type": leaderboard_list.activity_type,
                    "definition_version": DEFINITION_VERSION,
                    "window_start": leaderboard_list.window_start,
                    "window_end": leaderboard_list.window_end,
                    "rank": entry.rank,
                    "identity_id": entry.identity_id,
                    "display_name": entry.display_name,
                    "organization": entry.organization,
                    "count": entry.count,
                    "computed_at": computed_at,
                }
            )
    schema = get_schema("contributor_leaderboard")
    table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    return validate("contributor_leaderboard", table)


def build_leaderboards(
    tables: dict[str, pa.Table],
    *,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    config: ProjectConfig,
) -> LeaderboardResult:
    """Compute the three D19 top-N lists in one DuckDB pass.

    `tables` may hold `contribution_event`, `review_event`, `issue`,
    `identity_link`, `affiliation_period` and `person_identity` -- a missing
    key is treated as that table's empty schema (mirrors
    `metrics.engine.compute_all`'s own contract), so a caller only needs to
    pass what it has. `person_identity` is optional and used only for
    display names; when it (or a given identity within it) has no resolved
    display name, the identity's own raw identifier (e.g. a JIRA username)
    is shown instead of a bare `identity_id` (`_fallback_identifiers`) --
    both absent leaves `display_name = None`.
    """
    window_start, window_end = _leaderboard_window(as_of)
    con = _connect(tables)
    try:
        con.register("bot_identifier", _bot_identifiers(con, config))
        fallback_names = _fallback_identifiers(_table_or_empty(tables, "identity_link"))
        display_names: dict[str, str | None] = dict(fallback_names)
        for identity_id, name in _display_names(
            _table_or_empty(tables, "person_identity")
        ).items():
            if name:
                display_names[identity_id] = name

        lists = {
            ACTIVITY_COMMITS: _commits_list(con, window_start, window_end, display_names),
            ACTIVITY_REVIEWS: _reviews_list(con, window_start, window_end, display_names),
            ACTIVITY_JIRA_RESOLVED: _jira_resolved_list(
                con, window_start, window_end, display_names
            ),
        }
    finally:
        con.close()

    table = _to_table(lists, run_id=run_id, computed_at=computed_at)
    return LeaderboardResult(
        run_id=run_id,
        definition_version=DEFINITION_VERSION,
        computed_at=computed_at,
        lists=lists,
        identity_limitations_note=IDENTITY_LIMITATIONS_NOTE,
        table=table,
    )
