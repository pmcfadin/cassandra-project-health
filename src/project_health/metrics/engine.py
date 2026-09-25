"""DuckDB computation engine for the 6 M0 metrics (issue #7).

`compute_all` is the single entry point: given the normalized fact/identity
tables a run has accumulated (`schema/README.md`), compute every M0 metric's
`metric_value` rows in one pass and return them as one validated pyarrow
Table.

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
- METRICS.md §0.6 minimum sample floors: when the window's population `n` is
  below the metric's floor, `flag = 'insufficient_data'` and `value = null`
  -- but the row is still emitted (with its real `n`) so a reader can see how
  far short of the floor the window fell, rather than the window silently
  vanishing.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime, timedelta

import duckdb
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.metrics.windows import add_months, month_end, month_start, trailing_12m_window
from project_health.schema import get_schema, validate

DEFINITION_VERSION = "1.0"

# METRICS.md §0.6 default floors (this project's 6 M0 metrics use only these
# three categories -- see the task report for exactly how each metric maps
# to one, since none of the 6 is a literal "cohort/survival" metric).
FLOOR_RATE_RATIO = 5
FLOOR_CONCENTRATION = 5
FLOOR_LATENCY = 5

DEFAULT_STALE_THRESHOLD_DAYS = 90

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
    for name in ("contribution_event", "review_event", "issue", "identity_link"):
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
) -> dict:
    """Apply the METRICS.md §0.6 floor and build one `metric_value` row dict."""
    if n < floor or raw_value is None:
        value = None
        flag = "insufficient_data"
    else:
        value = float(raw_value)
        flag = "ok"
    return {
        "metric_id": metric_id,
        "definition_version": DEFINITION_VERSION,
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
        WHERE ce.event_type = 'commit' AND bi.raw_value IS NULL
        GROUP BY 1
        ORDER BY 1
        """
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
            WHERE ce.event_type = 'commit' AND bi.raw_value IS NULL
            GROUP BY 1
        )
        SELECT date_trunc('month', first_at)::DATE AS month_start, COUNT(*) AS n
        FROM first_commit
        GROUP BY 1
        ORDER BY 1
        """
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

        rows: list[dict] = []
        rows.extend(_active_contributors_monthly(con, as_of, run_id, computed_at))
        rows.extend(_new_contributors_monthly(con, as_of, run_id, computed_at))
        rows.extend(_unique_reviewers_monthly(con, as_of, run_id, computed_at))
        rows.extend(_reviewer_hhi(con, as_of, run_id, computed_at, reliable_from))
        rows.extend(_median_resolution_latency_jira(con, as_of, run_id, computed_at))
        rows.extend(_stale_jira_rate(con, as_of, run_id, computed_at, threshold_days))
    finally:
        con.close()

    schema = get_schema("metric_value")
    table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    return validate("metric_value", table)
