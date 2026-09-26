"""DuckDB computation engine for the M0 metrics (issue #7) plus the
contributor-sustainability trio added in issue #53 (`truck_factor`,
`contributor_absence_factor`, `contributor_hhi`) and the organizational-
diversity quartet added in issue #52 (`elephant_factor`, `organizational_hhi`,
`single_org_share`, `unknown_affiliation_rate` -- METRICS.md §5, D6).

`compute_all` is the single entry point: given the normalized fact/identity
tables a run has accumulated (`schema/README.md`), compute every registered
metric's `metric_value` rows in one pass and return them as one validated
pyarrow Table.

Design notes:

- Aggregation runs in DuckDB SQL over the pyarrow tables registered directly
  with an in-memory DuckDB connection, rather than hand-rolled pyarrow/pandas
  group-bys, per this task's guidance to use DuckDB for the heavy joins and
  aggregates.
- Identity resolution joins a fact table's raw identifier columns against
  `identity_link` the same way `normalize.identity.build_resolver` does: for
  a given `(source_type, source_value)`, a `manual:`-prefixed link wins over
  the naive one (`resolved_identity` view below).
- Per D3, this always recomputes from the *entire* accumulated input passed
  in `tables` -- it never computes incrementally. Callers are expected to
  pass full history every run.
- "Completed months only" (D5 / SCORING.md §5.1): a monthly window is never
  emitted for the calendar month containing `as_of` (or later).
- **Windows are dense, never sparse** (fixup cycle 1, issue #7 review): every
  monthly/trailing-12m metric emits exactly one row for every completed month
  from the first month that metric has any data through the last completed
  month before `as_of` -- including months with zero qualifying activity in
  between or after the last observed event. A month with nothing happening in
  it is real information (it may be exactly the decline this project exists
  to surface); it gets `n = 0`, `flag = 'insufficient_data'`, `value = null`,
  never a missing row. `_dense_months` below is the shared helper.
- METRICS.md §0.6 minimum sample floors: these apply only to rate/ratio
  metrics, concentration metrics, and latency statistics, where a small `n`
  makes the *statistic* unstable (a rate computed over 2 events, or a median
  of 2 latencies, is not trustworthy). For those metrics, when the window's
  population `n` is below the metric's floor, `flag = 'insufficient_data'`
  and `value = null` -- but the row is still emitted (with its real `n`) so
  a reader can see how far short of the floor the window fell, rather than
  the window silently vanishing.
  Plain headcount metrics (`active_contributors_monthly`,
  `new_contributors_monthly`, `unique_reviewers_monthly`) are exempt from
  this floor (issue #27, owner decision 2026-09-25): a raw count of "how
  many people did X" is already the complete, meaningful statistic at any
  `n`, including 0 -- it is not an estimate whose variance shrinks with `n`.
  These three metrics always report `value = n` and `flag = 'ok'`, for any
  `n` including 0.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date, datetime, timedelta

import duckdb
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.metrics.windows import add_months, month_end, month_start, trailing_12m_window
from project_health.normalize.affiliation import (
    DEFAULT_GITHUB_COMPANY_LOOKBACK_MONTHS,
    UNKNOWN_ORG,
)
from project_health.schema import CODE_COMMIT, get_schema, validate

# Per-metric definition_version (ARCHITECTURE.md §4.4 / D2 rule 6: "nothing
# changes silently" -- a formula/floor-behavior change bumps only the
# affected metric's version, never a blanket module constant applied to
# every row). Kept in lockstep with registry.py's `_DESCRIPTIONS`/version
# rows for the same metric_id.
DEFINITION_VERSIONS: dict[str, str] = {
    # issue #27: headcount metrics no longer apply the §0.6 sample floor.
    "active_contributors_monthly": "1.1",
    "new_contributors_monthly": "1.1",
    "unique_reviewers_monthly": "1.1",
    "reviewer_hhi": "1.0",
    "median_resolution_latency_jira": "1.0",
    "stale_jira_rate": "1.0",
    "pmc_joins_quarterly": "1.0",
    # issue #53
    "truck_factor": "1.0",
    "contributor_absence_factor": "1.0",
    "contributor_hhi": "1.0",
    # issue #52 (D6 organizational-diversity metrics, METRICS.md §5)
    "elephant_factor": "1.0",
    "organizational_hhi": "1.0",
    "single_org_share": "1.0",
    "unknown_affiliation_rate": "1.0",
}

# Headcount metrics are plain counts, not rate/ratio/concentration/latency
# statistics -- METRICS.md §0.6's sample floor does not apply to them
# (issue #27). They always report `value = n`, `flag = 'ok'`, for any `n`.
HEADCOUNT_METRICS = frozenset(
    {
        "active_contributors_monthly",
        "new_contributors_monthly",
        "unique_reviewers_monthly",
        "pmc_joins_quarterly",
    }
)

# METRICS.md §0.6 default floors (this project's 6 M0 metrics use only these
# three categories -- see the task report for exactly how each metric maps
# to one, since none of the 6 is a literal "cohort/survival" metric).
FLOOR_RATE_RATIO = 5
FLOOR_CONCENTRATION = 5
FLOOR_LATENCY = 5

# Issue #52 fixup cycle 1 (orchestrator feedback): a window's organizational
# concentration metrics (elephant_factor, organizational_hhi,
# single_org_share) render insufficient_data once `unknown`'s share of the
# window's commits reaches this threshold, regardless of how many *known*
# organizations were observed -- METRICS.md §0.6's intent is that a
# concentration statistic needs a trustworthy population underneath it, and
# 5+ known organizations passing their own floor while the window is still
# majority-unaffiliated commits would make the number look more confident
# than the underlying data supports. `unknown_affiliation_rate` itself is
# exempt (its entire purpose is reporting that share honestly, however
# high).
UNKNOWN_SHARE_INSUFFICIENT_THRESHOLD = 0.5

DEFAULT_STALE_THRESHOLD_DAYS = 90

# Avelino et al. (2016) DOA regression coefficients, verified against the
# primary-source PDF (docs/spec/RESEARCH.md §8.2) -- reused verbatim, never
# re-fit against this project's own data (re-fitting would itself need
# validation this project has not done).
DOA_INTERCEPT = 3.293
DOA_FA_COEFFICIENT = 1.098
DOA_DL_COEFFICIENT = 0.164
DOA_AC_COEFFICIENT = -0.321

# Author thresholds, also verified directly from the paper's text
# (RESEARCH.md §8.2): a developer counts as a file's "author" only if their
# *normalized* DOA (their DOA divided by the file's highest absolute DOA,
# range 0-1) exceeds `DOA_NORMALIZED_THRESHOLD`, *and* their absolute DOA is
# at least `DOA_MINIMUM_ABSOLUTE` (the model's own constant term). These were
# tuned by the paper's authors on a corpus of 133 popular GitHub projects,
# not a JIRA-based ASF-governance project like Cassandra -- METRICS.md's
# `truck_factor` entry and RESEARCH.md §8.2 both flag them as a researcher
# judgment call, not a universal law, which is why `truck_factor` ships as
# `experimental`.
DOA_NORMALIZED_THRESHOLD = 0.75
DOA_MINIMUM_ABSOLUTE = DOA_INTERCEPT

# bot_patterns `field` (projects/cassandra.yaml) -> the identity_link
# `source_type` it screens (normalize/identity.py RAW_TYPES). `github_login`
# has no M0 raw-identifier counterpart yet (no M0 collector populates a
# github_login raw column), so a `github_login` bot_pattern is a documented
# no-op until that collector exists.
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


def _table_or_empty(tables: dict[str, pa.Table], name: str) -> pa.Table:
    return tables[name] if name in tables else get_schema(name).empty_table()


def _connect(tables: dict[str, pa.Table]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    # DuckDB casts/truncates TIMESTAMPTZ using the session TimeZone, which
    # otherwise defaults to the host's local zone -- pin UTC explicitly so
    # month/day bucketing is a pure function of the (UTC) input timestamps,
    # not of which machine runs the pipeline.
    con.execute("SET TimeZone='UTC'")
    for name in (
        "contribution_event",
        "file_change_event",
        "review_event",
        "issue",
        "identity_link",
        "roster_entry",
        "affiliation_period",
    ):
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
    """Every raw `(type, value)` identifier this run should exclude as a bot.

    Applies `config.bot_patterns` (METRICS.md §0.5) against every distinct
    raw identifier value observed across the fact tables. Uses Python `re`
    rather than DuckDB's regex functions so the exact same regex semantics
    the config authors wrote against (Python `re`, not RE2) are used.
    """
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
        SELECT DISTINCT author_raw_type, author_raw_value
        FROM review_event WHERE author_raw_type IS NOT NULL
        UNION
        SELECT DISTINCT 'jira_username', reporter_raw FROM issue WHERE reporter_raw IS NOT NULL
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


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0, 1]) -- no numpy dependency."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * q
    lower = int(k)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower)


def _last_completed_month(as_of: date) -> date:
    """The last calendar month that is fully before `as_of`'s month (D5)."""
    return add_months(month_start(as_of), -1)


def _dense_months(first_month: date | None, as_of: date) -> list[date]:
    """Every completed month from `first_month` through the last completed
    month before `as_of`, inclusive -- with no gaps, regardless of whether a
    given in-between month has any data of its own.

    Returns `[]` when `first_month` is `None` (no data at all yet) or when
    `first_month` is itself not yet a completed month (all data so far falls
    in the current/future month).
    """
    if first_month is None:
        return []
    last = _last_completed_month(as_of)
    first = month_start(first_month)
    if first > last:
        return []
    months = []
    cursor = first
    while cursor <= last:
        months.append(cursor)
        cursor = add_months(cursor, 1)
    return months


def _hhi_from_credits(credits: list[int]) -> tuple[float | None, int]:
    """HHI (sum of squared shares) and the distinct-identity count `n` behind it.

    Returns `(None, 0)` for an empty credit list (nothing to divide by).
    """
    n = len(credits)
    total = sum(credits)
    if total <= 0:
        return None, n
    hhi = sum((c / total) ** 2 for c in credits)
    return hhi, n


def _make_row(
    *,
    metric_id: str,
    window_start: date,
    window_end: date,
    raw_value: float | None,
    n: int,
    floor: int,
    run_id: str,
    computed_at: datetime,
    details: dict,
    force_insufficient: bool = False,
) -> dict:
    """Apply the METRICS.md §0.6 floor (headcount metrics exempt, issue #27)
    and build one `metric_value` row dict.

    `force_insufficient` (issue #52 fixup cycle 1): lets a caller suppress
    `value`/`flag` for a reason beyond the plain `n < floor` check -- used
    by the organizational-diversity concentration metrics
    (`elephant_factor`/`organizational_hhi`/`single_org_share`) when the
    window's `unknown` share is >= 50%, per METRICS.md §0.6's intent that a
    concentration statistic needs a trustworthy population underneath it:
    a "known-org count" floor alone can pass (5+ known orgs observed) while
    the window is still mostly unaffiliated commits, which would make the
    concentration number look more confident than the data supports.
    """
    if metric_id in HEADCOUNT_METRICS:
        # Headcounts report their value for any n, including 0 -- never
        # gated by a sample floor (issue #27). `raw_value` is always a
        # computable count for these three metrics (never None), but guard
        # anyway rather than assume that invariant holds forever.
        value = float(raw_value) if raw_value is not None else None
        flag = "ok" if raw_value is not None else "insufficient_data"
    elif force_insufficient or n < floor or raw_value is None:
        value = None
        flag = "insufficient_data"
    else:
        value = float(raw_value)
        flag = "ok"
    return {
        "metric_id": metric_id,
        "definition_version": DEFINITION_VERSIONS[metric_id],
        "window_start": window_start,
        "window_end": window_end,
        "value": value,
        "n": n,
        "flag": flag,
        "run_id": run_id,
        "computed_at": computed_at,
        "details_json": json.dumps(details, sort_keys=True) if details else None,
    }


# --- Per-metric computation --------------------------------------------------


def _active_contributors_monthly(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    rows = con.execute(
        """
        SELECT
            date_trunc('month', ce.occurred_at)::DATE AS month_start,
            COUNT(DISTINCT ri.identity_id) AS n
        FROM contribution_event ce
        JOIN resolved_identity ri
            ON ri.source_type = ce.author_raw_type AND ri.source_value = ce.author_raw_value
        LEFT JOIN bot_identifier bi
            ON bi.raw_type = ce.author_raw_type AND bi.raw_value = ce.author_raw_value
        WHERE ce.event_type = ? AND bi.raw_value IS NULL
        GROUP BY 1
        ORDER BY 1
        """,
        [CODE_COMMIT],
    ).fetchall()
    counts: dict[date, int] = dict(rows)
    if not counts:
        return []

    out = []
    for month in _dense_months(min(counts), as_of):
        n = counts.get(month, 0)
        out.append(
            _make_row(
                metric_id="active_contributors_monthly",
                window_start=month,
                window_end=month_end(month),
                raw_value=float(n),
                n=n,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={},
            )
        )
    return out


def _new_contributors_monthly(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    rows = con.execute(
        """
        WITH first_commit AS (
            SELECT ri.identity_id AS identity_id, MIN(ce.occurred_at) AS first_at
            FROM contribution_event ce
            JOIN resolved_identity ri
                ON ri.source_type = ce.author_raw_type AND ri.source_value = ce.author_raw_value
            LEFT JOIN bot_identifier bi
                ON bi.raw_type = ce.author_raw_type AND bi.raw_value = ce.author_raw_value
            WHERE ce.event_type = ? AND bi.raw_value IS NULL
            GROUP BY 1
        )
        SELECT date_trunc('month', first_at)::DATE AS month_start, COUNT(*) AS n
        FROM first_commit
        GROUP BY 1
        ORDER BY 1
        """,
        [CODE_COMMIT],
    ).fetchall()
    counts: dict[date, int] = dict(rows)
    if not counts:
        return []

    out = []
    for month in _dense_months(min(counts), as_of):
        n = counts.get(month, 0)
        out.append(
            _make_row(
                metric_id="new_contributors_monthly",
                window_start=month,
                window_end=month_end(month),
                raw_value=float(n),
                n=n,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={},
            )
        )
    return out


def _unique_reviewers_monthly(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    rows = con.execute(
        """
        WITH reviewer_month AS (
            SELECT
                date_trunc('month', re.occurred_at)::DATE AS month_start,
                re.source AS source,
                ri.identity_id AS identity_id
            FROM review_event re
            JOIN resolved_identity ri
                ON ri.source_type = re.reviewer_raw_type AND ri.source_value = re.reviewer_raw_value
            LEFT JOIN bot_identifier bi
                ON bi.raw_type = re.reviewer_raw_type AND bi.raw_value = re.reviewer_raw_value
            WHERE bi.raw_value IS NULL
        )
        SELECT
            month_start,
            COUNT(DISTINCT identity_id) FILTER (WHERE source = 'commit_trailer')
                AS commit_trailer_count,
            COUNT(DISTINCT identity_id) FILTER (WHERE source = 'jira_field') AS jira_field_count,
            COUNT(DISTINCT identity_id) AS union_count
        FROM reviewer_month
        GROUP BY 1
        ORDER BY 1
        """
    ).fetchall()
    counts: dict[date, tuple[int, int, int]] = {
        month_start_: (trailer_count, jira_count, union_count)
        for month_start_, trailer_count, jira_count, union_count in rows
    }
    if not counts:
        return []

    out = []
    for month in _dense_months(min(counts), as_of):
        trailer_count, jira_count, union_count = counts.get(month, (0, 0, 0))
        out.append(
            _make_row(
                metric_id="unique_reviewers_monthly",
                window_start=month,
                window_end=month_end(month),
                raw_value=float(union_count),
                n=union_count,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "commit_trailer_count": trailer_count,
                    "jira_field_count": jira_count,
                    "union_count": union_count,
                },
            )
        )
    return out


def _reviewer_hhi(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    reliable_from: date | None,
) -> list[dict]:
    """Reviewer-concentration HHI, trailing-12m windows ending at each
    completed month, dense from the first month any review activity exists
    through the last completed month before `as_of` (fixup cycle 1).

    Primary `value`/`n` are computed from `commit_trailer` credits only
    (fixup cycle 1, review comment on issue #7): crediting both
    `commit_trailer` and `jira_field` double-counts a single review as two
    credits, and under M0's naive, non-cross-type identity resolution those
    two credits land on two different "people" (a `git_name` identity and a
    `jira_username` identity for what may be the same human) -- which
    deflates HHI rather than measuring true concentration. `commit_trailer`
    is also the denser source since 2017 (METRICS.md §0.4). `jira_field_hhi`
    and `union_hhi` (the old, source-mixed computation) are kept in
    `details_json` as cross-checks, not as the metric's value.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m FROM review_event"
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        window_start, _ = trailing_12m_window(window_end)

        credit_rows = con.execute(
            """
            SELECT ri.identity_id AS identity_id, re.source AS source, COUNT(*) AS credits
            FROM review_event re
            JOIN resolved_identity ri
                ON ri.source_type = re.reviewer_raw_type AND ri.source_value = re.reviewer_raw_value
            LEFT JOIN bot_identifier bi
                ON bi.raw_type = re.reviewer_raw_type AND bi.raw_value = re.reviewer_raw_value
            WHERE bi.raw_value IS NULL
              AND re.occurred_at::DATE >= ?
              AND re.occurred_at::DATE <= ?
            GROUP BY 1, 2
            """,
            [window_start, window_end],
        ).fetchall()

        commit_trailer_credits = [c for _, source, c in credit_rows if source == "commit_trailer"]
        jira_field_credits = [c for _, source, c in credit_rows if source == "jira_field"]
        union_by_identity: dict[str, int] = {}
        for identity_id, _source, credits in credit_rows:
            union_by_identity[identity_id] = union_by_identity.get(identity_id, 0) + credits

        hhi_commit_trailer, n_commit_trailer = _hhi_from_credits(commit_trailer_credits)
        hhi_jira_field, _n_jira_field = _hhi_from_credits(jira_field_credits)
        hhi_union, _n_union = _hhi_from_credits(list(union_by_identity.values()))

        effective_population = (
            (1.0 / hhi_commit_trailer) if hhi_commit_trailer is not None else None
        )
        before_reliable_from = bool(reliable_from and window_end < reliable_from)

        out.append(
            _make_row(
                metric_id="reviewer_hhi",
                window_start=window_start,
                window_end=window_end,
                raw_value=hhi_commit_trailer,
                n=n_commit_trailer,
                floor=FLOOR_CONCENTRATION,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "effective_reviewer_population": effective_population,
                    "before_reliable_from": before_reliable_from,
                    "jira_field_hhi": hhi_jira_field,
                    "union_hhi": hhi_union,
                },
            )
        )
    return out


def _median_resolution_latency_jira(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    rows = con.execute(
        """
        SELECT
            date_trunc('month', resolved_at)::DATE AS month_start,
            epoch(resolved_at) - epoch(created_at) AS latency_seconds
        FROM issue
        WHERE resolved_at IS NOT NULL
        ORDER BY 1
        """
    ).fetchall()

    by_month: dict[date, list[float]] = {}
    for month_start_, latency_seconds in rows:
        by_month.setdefault(month_start_, []).append(latency_seconds / 86400.0)
    if not by_month:
        return []

    out = []
    for month in _dense_months(min(by_month), as_of):
        latencies = by_month.get(month, [])
        n = len(latencies)
        median_days = statistics.median(latencies) if latencies else None
        p90_days = _percentile(latencies, 0.90) if latencies else None
        out.append(
            _make_row(
                metric_id="median_resolution_latency_jira",
                window_start=month,
                window_end=month_end(month),
                raw_value=median_days,
                n=n,
                floor=FLOOR_LATENCY,
                run_id=run_id,
                computed_at=computed_at,
                details={"p90_days": p90_days, "n": n},
            )
        )
    return out


def _stale_jira_rate(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    threshold_days: int,
) -> list[dict]:
    cutoff = as_of - timedelta(days=threshold_days)
    n_open, n_stale = con.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE resolved_at IS NULL) AS n_open,
            COUNT(*) FILTER (WHERE resolved_at IS NULL AND updated_at::DATE < ?) AS n_stale
        FROM issue
        """,
        [cutoff],
    ).fetchone()

    raw_value = (n_stale / n_open) if n_open else None
    return [
        _make_row(
            metric_id="stale_jira_rate",
            window_start=as_of,
            window_end=as_of,
            raw_value=raw_value,
            n=n_open,
            floor=FLOOR_RATE_RATIO,
            run_id=run_id,
            computed_at=computed_at,
            details={
                "n_open": n_open,
                "n_stale": n_stale,
                "threshold_days": threshold_days,
                "note": (
                    "M0 has no JIRA comment/changelog history; issue.updated_at is the only "
                    "staleness signal available, so history for this metric accumulates from "
                    "nightly snapshots -- this row is a single snapshot as of window_end, not a "
                    "monthly time series."
                ),
            },
        )
    ]


def _pmc_joins_quarterly(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """New PMC members per quarter, from join dates only.

    Queries the roster_entry table (from the ASF Whimsy collector) to compute
    quarterly counts of new PMC member joins (those with effective_from dates in
    that quarter), then emits those counts as metric values.

    Per METRICS.md §5: ground-truth data with no identity-ambiguity risk,
    since roster entries are already resolved identities from Whimsy.
    Note: Whimsy currently shows only living members, so departures are invisible.
    As historical roster snapshots accumulate, real net change (joins minus departures)
    will become computable. No sample-size floor applies.
    """
    from project_health.metrics.windows import (
        dense_quarters,
        quarter_end,
        quarter_start,
    )

    # Fetch quarterly PMC member counts
    rows = con.execute(
        """
        SELECT
            DATE_TRUNC('quarter', CAST(effective_from AS DATE))::DATE AS quarter_start,
            COUNT(DISTINCT asf_id) AS n
        FROM roster_entry
        WHERE role = 'pmc' AND effective_from IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """,
    ).fetchall()

    # Build a map of quarter -> count
    counts_per_quarter: dict[date, int] = {}
    first_quarter = None
    for quarter_start_val, count in rows:
        if quarter_start_val is not None:
            quarter_date = quarter_start(quarter_start_val)
            counts_per_quarter[quarter_date] = count
            if first_quarter is None or quarter_date < first_quarter:
                first_quarter = quarter_date

    if not counts_per_quarter:
        return []

    # Generate dense quarters and emit quarterly join counts
    out = []
    for quarter in dense_quarters(first_quarter, as_of):
        join_count = counts_per_quarter.get(quarter, 0)

        out.append(
            _make_row(
                metric_id="pmc_joins_quarterly",
                window_start=quarter,
                window_end=quarter_end(quarter),
                raw_value=float(join_count),
                n=join_count,
                floor=FLOOR_RATE_RATIO,  # Ignored for headcount metrics (issue #27)
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "pmc_new_joins": join_count,
                },
            )
        )

    return out


def _contributor_commit_counts(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date
) -> list[tuple[str, int]]:
    """`[(identity_id, commit_count), ...]` for non-bot, resolved identities
    with >= 1 `code_commit` in `[window_start, window_end]` -- the shared
    population `contributor_absence_factor` and `contributor_hhi` both rank/
    weight (METRICS.md `contributor_hhi`: "commits per identity")."""
    return con.execute(
        """
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
        """,
        [CODE_COMMIT, window_start, window_end],
    ).fetchall()


def _contributor_absence_factor(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """CHAOSS "Bus Factor" (METRICS.md `contributor_absence_factor`): the
    smallest number of contributors, ranked by trailing-12m commit count
    descending, whose cumulative commits reach 50% of the window's total --
    "contributor dependency," in the issue's own words. Dense trailing-12m
    windows, one per completed month, from the first month any `code_commit`
    exists through the last completed month before `as_of`.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m "
        "FROM contribution_event WHERE event_type = ?",
        [CODE_COMMIT],
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        window_start, _ = trailing_12m_window(window_end)
        credits = _contributor_commit_counts(con, window_start, window_end)

        n = len(credits)
        total_commits = sum(c for _, c in credits)
        smallest_n = None
        top_contributors: list[dict] = []
        if total_commits > 0:
            target = 0.5 * total_commits
            cumulative = 0
            for identity_id, commits in credits:
                cumulative += commits
                top_contributors.append(
                    {
                        "identity_id": identity_id,
                        "commits": commits,
                        "cumulative_share": round(cumulative / total_commits, 4),
                    }
                )
                if smallest_n is None and cumulative >= target:
                    smallest_n = len(top_contributors)

        out.append(
            _make_row(
                metric_id="contributor_absence_factor",
                window_start=window_start,
                window_end=window_end,
                raw_value=float(smallest_n) if smallest_n is not None else None,
                n=n,
                floor=FLOOR_CONCENTRATION,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "total_commits": total_commits,
                    # Full ranked list, not just the top `smallest_n` -- an
                    # auditor can see exactly which contributors and shares
                    # produced the count (D2.3).
                    "contributors": top_contributors,
                },
            )
        )
    return out


def _contributor_hhi(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """Contributor-concentration HHI (METRICS.md `contributor_hhi` /
    `effective_contributor_population`): sum-of-squared commit shares per
    resolved, non-bot identity, over dense trailing-12m windows (same
    population/window as `contributor_absence_factor`, same math as
    `reviewer_hhi` applied to commit shares instead of review credits).
    `effective_contributor_population` (1/HHI) is carried in `details_json`,
    not as its own metric_value row -- same convention `reviewer_hhi`
    established for its own reciprocal.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m "
        "FROM contribution_event WHERE event_type = ?",
        [CODE_COMMIT],
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        window_start, _ = trailing_12m_window(window_end)
        credits = [c for _, c in _contributor_commit_counts(con, window_start, window_end)]
        hhi, n = _hhi_from_credits(credits)
        effective_population = (1.0 / hhi) if hhi is not None else None

        out.append(
            _make_row(
                metric_id="contributor_hhi",
                window_start=window_start,
                window_end=window_end,
                raw_value=hhi,
                n=n,
                floor=FLOOR_CONCENTRATION,
                run_id=run_id,
                computed_at=computed_at,
                details={"effective_contributor_population": effective_population},
            )
        )
    return out


def _organization_commit_counts(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date
) -> list[tuple[str, int, int]]:
    """`[(organization, commits, contributors), ...]`, desc by commits, for
    every non-bot, resolved `code_commit` in `[window_start, window_end]`
    (issue #52, METRICS.md §5).

    Each commit resolves to an organization via `affiliation_period` (D6,
    `normalize/affiliation.py`): the highest-priority row covering that
    commit's `occurred_at` date wins (`curated` > `email_domain` >
    `github_company`, "the curated override wins over heuristics"). An
    identity with no covering `affiliation_period` row at all -- or a
    commit whose only covering row is `source = 'curated'` with a dated
    range that doesn't include this commit's date -- resolves to
    `UNKNOWN_ORG` (METRICS.md §0.5: unresolved affiliation is its own
    bucket, shown in the denominator, never redistributed or guessed).

    `ROW_NUMBER() ... PARTITION BY c.event_id` (not `identity_id,
    occurred_at`) is deliberate: two different commits by the same identity
    can share the same `occurred_at` second, and partitioning on the pair
    would silently drop one of them from its own ranking.
    """
    return con.execute(
        """
        WITH commits AS (
            SELECT ce.event_id AS event_id, ri.identity_id AS identity_id,
                   ce.occurred_at AS occurred_at
            FROM contribution_event ce
            JOIN resolved_identity ri
                ON ri.source_type = ce.author_raw_type AND ri.source_value = ce.author_raw_value
            LEFT JOIN bot_identifier bi
                ON bi.raw_type = ce.author_raw_type AND bi.raw_value = ce.author_raw_value
            WHERE ce.event_type = ?
              AND bi.raw_value IS NULL
              AND ce.occurred_at::DATE >= ? AND ce.occurred_at::DATE <= ?
        ),
        ranked AS (
            SELECT
                c.event_id, c.identity_id, ap.organization AS organization,
                ROW_NUMBER() OVER (
                    PARTITION BY c.event_id
                    ORDER BY
                        CASE ap.source
                            WHEN 'curated' THEN 0
                            WHEN 'email_domain' THEN 1
                            WHEN 'github_company' THEN 2
                            ELSE 3
                        END,
                        ap.organization
                ) AS rn
            FROM commits c
            LEFT JOIN affiliation_period ap
                ON ap.identity_id = c.identity_id
               AND (ap.effective_from IS NULL OR c.occurred_at::DATE >= ap.effective_from)
               AND (ap.effective_to IS NULL OR c.occurred_at::DATE < ap.effective_to)
        )
        SELECT
            COALESCE(organization, ?) AS organization,
            COUNT(*) AS commits,
            COUNT(DISTINCT identity_id) AS contributors
        FROM ranked
        WHERE rn = 1
        GROUP BY 1
        ORDER BY commits DESC, organization ASC
        """,
        [CODE_COMMIT, window_start, window_end, UNKNOWN_ORG],
    ).fetchall()


def _organization_contributor_unknown_count(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date
) -> tuple[int, int]:
    """`(total_contributors, unknown_contributors)` for
    `unknown_affiliation_rate`'s "equivalent contributor-count version"
    (METRICS.md `unknown_affiliation_rate`), computed as exact distinct
    identity counts (not a sum of `_organization_commit_counts`' per-org
    contributor counts, which can double-count an identity whose curated
    affiliation changes organization mid-window). A contributor counts as
    `unknown` here only if **none** of their commits in the window resolved
    to a known organization.
    """
    row = con.execute(
        """
        WITH commits AS (
            SELECT ce.event_id AS event_id, ri.identity_id AS identity_id,
                   ce.occurred_at AS occurred_at
            FROM contribution_event ce
            JOIN resolved_identity ri
                ON ri.source_type = ce.author_raw_type AND ri.source_value = ce.author_raw_value
            LEFT JOIN bot_identifier bi
                ON bi.raw_type = ce.author_raw_type AND bi.raw_value = ce.author_raw_value
            WHERE ce.event_type = ?
              AND bi.raw_value IS NULL
              AND ce.occurred_at::DATE >= ? AND ce.occurred_at::DATE <= ?
        ),
        ranked AS (
            SELECT
                c.event_id, c.identity_id, ap.organization AS organization,
                ROW_NUMBER() OVER (
                    PARTITION BY c.event_id
                    ORDER BY
                        CASE ap.source
                            WHEN 'curated' THEN 0
                            WHEN 'email_domain' THEN 1
                            WHEN 'github_company' THEN 2
                            ELSE 3
                        END,
                        ap.organization
                ) AS rn
            FROM commits c
            LEFT JOIN affiliation_period ap
                ON ap.identity_id = c.identity_id
               AND (ap.effective_from IS NULL OR c.occurred_at::DATE >= ap.effective_from)
               AND (ap.effective_to IS NULL OR c.occurred_at::DATE < ap.effective_to)
        ),
        per_identity AS (
            SELECT identity_id, BOOL_OR(organization IS NOT NULL) AS has_known
            FROM ranked
            WHERE rn = 1
            GROUP BY identity_id
        )
        SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE NOT has_known) AS unknown
        FROM per_identity
        """,
        [CODE_COMMIT, window_start, window_end],
    ).fetchone()
    return (row[0] or 0, row[1] or 0)


def _unknown_dominates(org_counts: list[tuple[str, int, int]], total_commits: int) -> bool:
    """True iff `unknown`'s share of `total_commits` is >=
    `UNKNOWN_SHARE_INSUFFICIENT_THRESHOLD` (issue #52 fixup cycle 1).
    `total_commits == 0` is never "dominated" (nothing to be dominated) --
    the ordinary `n < floor` check already renders that case
    insufficient_data on its own.
    """
    if total_commits <= 0:
        return False
    unknown_commits = next((c for o, c, _ in org_counts if o == UNKNOWN_ORG), 0)
    return (unknown_commits / total_commits) >= UNKNOWN_SHARE_INSUFFICIENT_THRESHOLD


def _elephant_factor(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    github_company_lookback_months: int,
) -> list[dict]:
    """`elephant_factor` (METRICS.md §5): the minimum number of
    organizations whose combined trailing-12m commits reach 50% of the
    window's total -- same algorithm as `contributor_absence_factor`,
    applied to `affiliation_period`-resolved organization instead of
    individual identity.

    `unknown` (D6: unresolved affiliation, never guessed) is itself counted
    as one "organization" bucket for the cumulative-sum threshold
    (METRICS.md: "unknown ... treated as its own organization bucket ... so
    it cannot silently vanish from the denominator"), and
    `unknown_needed_to_reach_threshold` records whether it was one of the
    entities the cumulative sum needed. The §0.6 concentration floor (5)
    and this row's reported `n`, however, count only *known* organizations
    (METRICS.md: "floor 5 distinct known organizations ... if unknown
    dominates the population, the metric renders insufficient_data for
    status purposes even if a raw number can be shown") -- `details_json`
    always carries the raw computed value (`raw_value_before_floor`) even
    when `n_known` is below the floor and `value`/`flag` are suppressed.
    Also suppressed (issue #52 fixup cycle 1) whenever `unknown`'s own
    share of the window's commits is >=
    `UNKNOWN_SHARE_INSUFFICIENT_THRESHOLD`, even if `n_known` alone would
    have passed the floor -- 5+ known organizations observed in a window
    that's still majority-unaffiliated commits is not a trustworthy
    concentration reading.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m "
        "FROM contribution_event WHERE event_type = ?",
        [CODE_COMMIT],
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        window_start, _ = trailing_12m_window(window_end)
        org_counts = _organization_commit_counts(con, window_start, window_end)

        n_known = sum(1 for org, _commits, _c in org_counts if org != UNKNOWN_ORG)
        total_commits = sum(commits for _org, commits, _c in org_counts)

        smallest_n = None
        unknown_needed = False
        ranked_orgs: list[dict] = []
        if total_commits > 0:
            target = 0.5 * total_commits
            cumulative = 0
            for org, commits, contributors in org_counts:  # already sorted desc by commits
                cumulative += commits
                ranked_orgs.append(
                    {
                        "organization": org,
                        "commits": commits,
                        "contributors": contributors,
                        "cumulative_share": round(cumulative / total_commits, 4),
                    }
                )
                if smallest_n is None and cumulative >= target:
                    smallest_n = len(ranked_orgs)
                    unknown_needed = any(o["organization"] == UNKNOWN_ORG for o in ranked_orgs)

        out.append(
            _make_row(
                metric_id="elephant_factor",
                window_start=window_start,
                window_end=window_end,
                raw_value=float(smallest_n) if smallest_n is not None else None,
                n=n_known,
                floor=FLOOR_CONCENTRATION,
                run_id=run_id,
                computed_at=computed_at,
                force_insufficient=_unknown_dominates(org_counts, total_commits),
                details={
                    "total_commits": total_commits,
                    "organizations": ranked_orgs,
                    "unknown_needed_to_reach_threshold": unknown_needed,
                    "raw_value_before_floor": smallest_n,
                    "github_company_lookback_months": github_company_lookback_months,
                },
            )
        )
    return out


def _organizational_hhi(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    github_company_lookback_months: int,
) -> list[dict]:
    """`organizational_hhi` / `effective_organizational_population`
    (METRICS.md §5): sum-of-squared organizational commit shares, dense
    trailing-12m windows, same population/window/org-resolution as
    `elephant_factor`. `unknown` counts as its own bucket in the HHI sum
    itself (consistent with `elephant_factor`'s treatment -- D6, never
    redistributed), but the §0.6 floor and reported `n` count only known
    organizations. `effective_organizational_population` (1/HHI) is carried
    in `details_json` only, matching `reviewer_hhi`/`contributor_hhi`'s own
    convention for their reciprocals -- not a separate metric_value row.
    Also suppressed to insufficient_data (issue #52 fixup cycle 1) whenever
    `unknown`'s share of the window's commits is >=
    `UNKNOWN_SHARE_INSUFFICIENT_THRESHOLD` -- see `_elephant_factor`.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m "
        "FROM contribution_event WHERE event_type = ?",
        [CODE_COMMIT],
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        window_start, _ = trailing_12m_window(window_end)
        org_counts = _organization_commit_counts(con, window_start, window_end)

        n_known = sum(1 for org, _commits, _c in org_counts if org != UNKNOWN_ORG)
        credits = [commits for _org, commits, _c in org_counts]
        total_commits = sum(credits)
        hhi, _n_all = _hhi_from_credits(credits)
        effective_population = (1.0 / hhi) if hhi is not None else None

        out.append(
            _make_row(
                metric_id="organizational_hhi",
                window_start=window_start,
                window_end=window_end,
                raw_value=hhi,
                n=n_known,
                floor=FLOOR_CONCENTRATION,
                run_id=run_id,
                computed_at=computed_at,
                force_insufficient=_unknown_dominates(org_counts, total_commits),
                details={
                    "effective_organizational_population": effective_population,
                    "unknown_included_in_hhi": any(
                        org == UNKNOWN_ORG for org, _commits, _c in org_counts
                    ),
                    "github_company_lookback_months": github_company_lookback_months,
                },
            )
        )
    return out


def _single_org_share(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    github_company_lookback_months: int,
) -> list[dict]:
    """`single_org_share` (METRICS.md §5): share of trailing-12m commits
    from the single largest *known* organization -- the published CHAOSS
    "Organizational Diversity" ratio. `unknown` is never eligible to be
    "the largest org" and is shown separately in `details_json` rather than
    folded into this figure (METRICS.md: "unknown bucket shown separately,
    never merged into the 'largest known' figure"); the share's denominator
    is still every commit in the window, `unknown` included, so a
    high-unknown-rate window correctly produces a small `single_org_share`
    rather than an artificially inflated one. Also suppressed to
    insufficient_data (issue #52 fixup cycle 1) whenever `unknown`'s own
    share of the window's commits is >=
    `UNKNOWN_SHARE_INSUFFICIENT_THRESHOLD` -- see `_elephant_factor`.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m "
        "FROM contribution_event WHERE event_type = ?",
        [CODE_COMMIT],
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        window_start, _ = trailing_12m_window(window_end)
        org_counts = _organization_commit_counts(con, window_start, window_end)

        total_commits = sum(commits for _org, commits, _c in org_counts)
        known = [(org, commits) for org, commits, _c in org_counts if org != UNKNOWN_ORG]
        n_known = len(known)

        raw_value = None
        top_org = None
        if known and total_commits > 0:
            # org_counts (and therefore `known`) is already sorted desc by
            # commits -- its first entry is the largest known organization.
            top_org, top_commits = known[0]
            raw_value = top_commits / total_commits

        unknown_commits = next((c for o, c, _ in org_counts if o == UNKNOWN_ORG), 0)
        unknown_share = (unknown_commits / total_commits) if total_commits > 0 else None

        out.append(
            _make_row(
                metric_id="single_org_share",
                window_start=window_start,
                window_end=window_end,
                raw_value=raw_value,
                n=n_known,
                floor=FLOOR_CONCENTRATION,
                run_id=run_id,
                computed_at=computed_at,
                force_insufficient=_unknown_dominates(org_counts, total_commits),
                details={
                    "largest_known_organization": top_org,
                    "total_commits": total_commits,
                    "unknown_commits": unknown_commits,
                    "unknown_share": unknown_share,
                    "github_company_lookback_months": github_company_lookback_months,
                },
            )
        )
    return out


def _unknown_affiliation_rate(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """`unknown_affiliation_rate` (METRICS.md §5): share of trailing-12m
    commits whose author's organization is `unknown` per `affiliation_period`
    (D6's curated file + reviewed email-domain map + GitHub profile company
    field), with the equivalent distinct-contributor-share version in
    `details_json`. Deliberately has no direction of good (METRICS.md: this
    completeness metric doesn't get a health verdict of its own -- it's a
    confidence modifier on `elephant_factor`/`organizational_hhi`/
    `single_org_share`) and is `established` **as a measurement of the
    unknown bucket itself**, per its own METRICS.md section. Unlike the
    other three organizational metrics, its `n`/floor is total commits in
    the window (METRICS.md §0.6's default rate/ratio floor), not a count of
    known organizations -- this metric's whole point is measuring how much
    of the population resolved at all.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m "
        "FROM contribution_event WHERE event_type = ?",
        [CODE_COMMIT],
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        window_end = month_end(month)
        window_start, _ = trailing_12m_window(window_end)
        org_counts = _organization_commit_counts(con, window_start, window_end)
        total_contributors, unknown_contributors = _organization_contributor_unknown_count(
            con, window_start, window_end
        )

        total_commits = sum(commits for _org, commits, _c in org_counts)
        unknown_commits = next((c for o, c, _ in org_counts if o == UNKNOWN_ORG), 0)
        commit_rate = (unknown_commits / total_commits) if total_commits > 0 else None
        contributor_rate = (
            (unknown_contributors / total_contributors) if total_contributors > 0 else None
        )

        out.append(
            _make_row(
                metric_id="unknown_affiliation_rate",
                window_start=window_start,
                window_end=window_end,
                raw_value=commit_rate,
                n=total_commits,
                floor=FLOOR_RATE_RATIO,
                run_id=run_id,
                computed_at=computed_at,
                details={
                    "unknown_commits": unknown_commits,
                    "total_commits": total_commits,
                    "unknown_contributors": unknown_contributors,
                    "total_contributors": total_contributors,
                    "contributor_rate": contributor_rate,
                },
            )
        )
    return out


def _file_change_rows_through(
    con: duckdb.DuckDBPyConnection, cutoff: date
) -> list[tuple[str, str, str, float]]:
    """`[(file_path, identity_id, change_type, occurred_at_epoch), ...]` for
    every non-bot, resolved `file_change_event` row at or before `cutoff` --
    `truck_factor`'s raw per-(file, developer) input, full history up to that
    point (issue #53: this is a full-repository snapshot, recomputed at each
    completed month, never a rolling window -- METRICS.md `truck_factor`
    "Population & exclusions").

    Returns `occurred_at` as a Unix-epoch `float` (`epoch(...)`, matching
    `_median_resolution_latency_jira`'s pattern below) rather than fetching
    the raw `TIMESTAMPTZ` column directly: `_truck_factor_snapshot` only ever
    needs it for relative ordering (min/max per file), and every other query
    in this module avoids fetching a raw `TIMESTAMPTZ` column as a Python
    object for exactly this reason -- some duckdb/Python driver builds need
    an optional `pytz` install to convert one, which this project doesn't
    otherwise depend on.
    """
    return con.execute(
        """
        SELECT fc.file_path, ri.identity_id AS identity_id, fc.change_type,
               epoch(fc.occurred_at) AS occurred_at_epoch
        FROM file_change_event fc
        JOIN resolved_identity ri
            ON ri.source_type = fc.author_raw_type AND ri.source_value = fc.author_raw_value
        LEFT JOIN bot_identifier bi
            ON bi.raw_type = fc.author_raw_type AND bi.raw_value = fc.author_raw_value
        WHERE bi.raw_value IS NULL AND fc.occurred_at::DATE <= ?
        """,
        [cutoff],
    ).fetchall()


def _doa(fa: int, dl: int, ac: int) -> float:
    """Avelino et al. (2016) Degree-of-Authorship, verified formula
    (RESEARCH.md §8.2): ``3.293 + 1.098*FA + 0.164*DL - 0.321*ln(1+AC)``.

    ``FA`` = 1 if this developer authored the file's first observed version,
    else 0. ``DL`` = number of commits this developer made to the file
    ("deliveries," Fritz et al. 2010's degree-of-knowledge model, which
    Avelino et al. adopt this formula from). ``AC`` = number of commits
    *other* developers made to the file ("acceptances" of other authors'
    changes) -- more competing changes by others lowers this developer's DOA.
    """
    return (
        DOA_INTERCEPT
        + DOA_FA_COEFFICIENT * fa
        + DOA_DL_COEFFICIENT * dl
        + DOA_AC_COEFFICIENT * math.log(1 + ac)
    )


def _truck_factor_snapshot(
    file_rows: list[tuple[str, str, str, float]],
) -> dict | None:
    """One point-in-time truck-factor computation from `_file_change_rows_through`'s
    output (issue #53).

    Returns `None` if there are no existing files to compute over (e.g. every
    file collected so far has since been deleted, or there's no data yet).
    Otherwise returns a dict with `truck_factor`, `n` (candidate-author
    population), `total_files`, `orphaned_files_at_start` (files with no
    qualifying author even before any developer is removed -- METRICS.md
    §0.6 doesn't apply to this count itself, it's diagnostic), and
    `removed_developers` (ordered list of identity_ids, for audit per D2.3).

    Algorithm (METRICS.md `truck_factor`, RESEARCH.md §8.2's "adopt ... the
    'orphaned files > 50%' stopping criterion verbatim"): repeatedly remove
    the developer who is a qualifying DOA-author of the most still-covered
    files, until more than half the project's (still-existing) files have no
    remaining qualifying author. This project's greedy tie-break (highest
    coverage count, then lowest identity_id string, for determinism) and its
    "a file's author set may hold more than one qualifying developer, not
    just the single top one" reading are this project's own reproducible
    choice among several defensible reimplementations -- RESEARCH.md §8.2
    documents that the algorithm's own authors note "no consensus about how
    to calculate truck factor" and that a later comparative study
    (Ferreira et al. 2019) exists specifically because independent
    reimplementations disagree with each other on the same repository.
    """
    # Which files still exist at the cutoff: a file whose most recent change
    # at or before the cutoff was a deletion has nothing left to "own."
    last_change_at: dict[str, float] = {}
    last_change_type: dict[str, str] = {}
    for file_path, _identity_id, change_type, occurred_at in file_rows:
        if file_path not in last_change_at or occurred_at >= last_change_at[file_path]:
            last_change_at[file_path] = occurred_at
            last_change_type[file_path] = change_type
    existing_files = {f for f, t in last_change_type.items() if t != "D"}
    if not existing_files:
        return None

    # Per (file, identity): DL (this dev's commits on the file), AC (everyone
    # else's), FA (1 iff this dev authored the file's earliest observed
    # commit).
    commits_by_file_identity: dict[str, dict[str, int]] = {}
    first_commit_at: dict[str, float] = {}
    first_author: dict[str, str] = {}
    for file_path, identity_id, _change_type, occurred_at in file_rows:
        if file_path not in existing_files:
            continue
        by_identity = commits_by_file_identity.setdefault(file_path, {})
        by_identity[identity_id] = by_identity.get(identity_id, 0) + 1
        if file_path not in first_commit_at or occurred_at < first_commit_at[file_path]:
            first_commit_at[file_path] = occurred_at
            first_author[file_path] = identity_id

    authors_by_file: dict[str, set[str]] = {}
    for file_path, by_identity in commits_by_file_identity.items():
        total_on_file = sum(by_identity.values())
        doa_by_identity = {
            identity_id: _doa(
                fa=1 if first_author[file_path] == identity_id else 0,
                dl=dl,
                ac=total_on_file - dl,
            )
            for identity_id, dl in by_identity.items()
        }
        max_doa = max(doa_by_identity.values())
        authors: set[str] = set()
        if max_doa > 0:
            for identity_id, doa in doa_by_identity.items():
                if (
                    doa / max_doa > DOA_NORMALIZED_THRESHOLD
                    and doa >= DOA_MINIMUM_ABSOLUTE
                ):
                    authors.add(identity_id)
        authors_by_file[file_path] = authors

    total_files = len(existing_files)
    orphaned = {f for f, authors in authors_by_file.items() if not authors}
    orphaned_at_start = len(orphaned)
    remaining = {f: set(a) for f, a in authors_by_file.items() if a}
    candidate_pool = {i for a in authors_by_file.values() for i in a}

    removed: list[str] = []
    threshold = 0.5 * total_files
    while len(orphaned) <= threshold and remaining:
        coverage: dict[str, int] = {}
        for authors in remaining.values():
            for identity_id in authors:
                coverage[identity_id] = coverage.get(identity_id, 0) + 1
        if not coverage:
            break
        next_removed = min(coverage, key=lambda identity_id: (-coverage[identity_id], identity_id))
        removed.append(next_removed)
        for file_path in list(remaining):
            remaining[file_path].discard(next_removed)
            if not remaining[file_path]:
                orphaned.add(file_path)
                del remaining[file_path]

    return {
        "truck_factor": len(removed),
        "n": len(candidate_pool),
        "total_files": total_files,
        "orphaned_files_at_start": orphaned_at_start,
        "orphaned_files_final": len(orphaned),
        "removed_developers": removed,
    }


def _truck_factor(
    con: duckdb.DuckDBPyConnection, as_of: date, run_id: str, computed_at: datetime
) -> list[dict]:
    """`truck_factor` (METRICS.md, RESEARCH.md §8.2): a full-repository,
    point-in-time snapshot recomputed at each completed month to build a
    trend -- explicitly *not* a windowed rate (METRICS.md `truck_factor`:
    "a snapshot metric, not a windowed rate"), unlike every other metric in
    this module. Dense months, one per completed month, from the first month
    any `file_change_event` exists through the last completed month before
    `as_of`; each month's snapshot uses every `file_change_event` up to that
    month's end (D2.2 -- trends over snapshots), not just that month's
    activity.
    """
    month_rows = con.execute(
        "SELECT DISTINCT date_trunc('month', occurred_at)::DATE AS m FROM file_change_event"
    ).fetchall()
    if not month_rows:
        return []
    first_month = min(m for (m,) in month_rows)

    out = []
    for month in _dense_months(first_month, as_of):
        cutoff = month_end(month)
        snapshot = _truck_factor_snapshot(_file_change_rows_through(con, cutoff))

        if snapshot is None:
            raw_value = None
            n = 0
            details = {"note": "no existing files with recorded authorship at this snapshot"}
        else:
            raw_value = float(snapshot["truck_factor"])
            n = snapshot["n"]
            details = {
                "total_files": snapshot["total_files"],
                "orphaned_files_at_start": snapshot["orphaned_files_at_start"],
                "orphaned_files_final": snapshot["orphaned_files_final"],
                "removed_developers": snapshot["removed_developers"],
                "doa_normalized_threshold": DOA_NORMALIZED_THRESHOLD,
                "doa_minimum_absolute": DOA_MINIMUM_ABSOLUTE,
                "limitations": (
                    "File-authorship concentration, not 'who could review/merge/design' "
                    "(METRICS.md `truck_factor` Weaknesses); not validated as a failure "
                    "predictor for an ASF/JIRA-centric project (RESEARCH.md §8.2); DOA is "
                    "computed per literal file path, not rename-followed. The snapshot counts "
                    "every historical author through the cutoff, including people inactive for "
                    "years -- their files are already effectively orphaned in practice (that "
                    "knowledge is already gone), but the algorithm still counts them as a "
                    "removable 'key developer,' so the reported value can overstate the "
                    "project's *current* resilience relative to its actually-available "
                    "contributor pool."
                ),
            }

        out.append(
            _make_row(
                metric_id="truck_factor",
                window_start=cutoff,
                window_end=cutoff,
                raw_value=raw_value,
                n=n,
                floor=FLOOR_CONCENTRATION,
                run_id=run_id,
                computed_at=computed_at,
                details=details,
            )
        )
    return out


# --- Config helpers -----------------------------------------------------------


def _stale_threshold_days(config: ProjectConfig) -> int:
    if config.issue_tracker is not None:
        value = getattr(config.issue_tracker, "stale_threshold_days", None)
        if value is not None:
            return int(value)
    return DEFAULT_STALE_THRESHOLD_DAYS


def _reliable_from(config: ProjectConfig) -> date | None:
    raw = config.reviewer_extraction.reliable_from
    return date.fromisoformat(raw) if raw else None


def _github_company_lookback_months(config: ProjectConfig) -> int:
    """The `github_company_lookback_months` value `normalize.affiliation.
    build_affiliation_periods` actually applied this run (issue #52 fixup
    cycle 2) -- carried into the organizational-diversity concentration
    metrics' `details_json` so a reader can see which lookback bound
    produced the numbers, without re-deriving it from `projects/<id>.yaml`.
    """
    value = config.github_company_lookback_months
    return value if value is not None else DEFAULT_GITHUB_COMPANY_LOOKBACK_MONTHS


# --- Entry point ---------------------------------------------------------


def compute_all(
    tables: dict[str, pa.Table],
    *,
    as_of: date,
    run_id: str,
    computed_at: datetime,
    config: ProjectConfig,
) -> pa.Table:
    """Compute every M0 metric's `metric_value` rows in one DuckDB pass.

    `tables` holds whatever normalized tables this run has accumulated
    (`schema/README.md`): `contribution_event`, `review_event`, `issue`,
    `identity_link`. A missing key is treated as an empty table of that
    name's declared schema, so a caller (or a golden test) only needs to
    pass the tables its scenario actually needs.

    Per D3, every run recomputes from the *entire* accumulated input passed
    in `tables` -- this function never computes incrementally.
    """
    con = _connect(tables)
    try:
        con.register("bot_identifier", _bot_identifiers(con, config))

        reliable_from = _reliable_from(config)
        threshold_days = _stale_threshold_days(config)
        github_company_lookback_months = _github_company_lookback_months(config)

        rows: list[dict] = []
        rows.extend(_pmc_joins_quarterly(con, as_of, run_id, computed_at))
        rows.extend(_active_contributors_monthly(con, as_of, run_id, computed_at))
        rows.extend(_new_contributors_monthly(con, as_of, run_id, computed_at))
        rows.extend(_unique_reviewers_monthly(con, as_of, run_id, computed_at))
        rows.extend(_reviewer_hhi(con, as_of, run_id, computed_at, reliable_from))
        rows.extend(_median_resolution_latency_jira(con, as_of, run_id, computed_at))
        rows.extend(_stale_jira_rate(con, as_of, run_id, computed_at, threshold_days))
        rows.extend(_truck_factor(con, as_of, run_id, computed_at))
        rows.extend(_contributor_absence_factor(con, as_of, run_id, computed_at))
        rows.extend(_contributor_hhi(con, as_of, run_id, computed_at))
        rows.extend(
            _elephant_factor(con, as_of, run_id, computed_at, github_company_lookback_months)
        )
        rows.extend(
            _organizational_hhi(con, as_of, run_id, computed_at, github_company_lookback_months)
        )
        rows.extend(
            _single_org_share(con, as_of, run_id, computed_at, github_company_lookback_months)
        )
        rows.extend(_unknown_affiliation_rate(con, as_of, run_id, computed_at))
    finally:
        con.close()

    schema = get_schema("metric_value")
    table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    return validate("metric_value", table)
