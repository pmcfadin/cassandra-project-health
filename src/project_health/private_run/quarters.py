"""Calendar-quarter (`YYYYQN`) utilities for the private Cassandra
communication run (issue #110).

A quarter string always looks like `2024Q1` (4-digit year, literal `Q`,
quarter number 1-4). Because that format is fixed-width, plain string
comparison (`"2024Q1" < "2024Q2"`) already matches chronological order, and
every function below relies on that rather than parsing back into a
`(year, quarter)` tuple except where it needs the numeric parts (e.g.
computing calendar-day bounds or walking to the next quarter).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime

# The issue's own scope: "2012Q1 -> 2026Q3".
DEFAULT_START_QUARTER = "2012Q1"
DEFAULT_END_QUARTER = "2026Q3"

_QUARTER_MONTHS: dict[int, tuple[int, int]] = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}


class QuarterError(ValueError):
    """Raised for a malformed or out-of-order quarter string."""


def quarter_of(value: datetime | date) -> str:
    """The `YYYYQN` calendar quarter containing `value`."""
    return f"{value.year:04d}Q{(value.month - 1) // 3 + 1}"


def parse_quarter(value: str) -> tuple[int, int]:
    """`(year, quarter_number)` from a `YYYYQN` string, or raise
    `QuarterError` if it's malformed."""
    if len(value) != 6 or value[4] != "Q":
        raise QuarterError(f"quarter must look like 'YYYYQN', got {value!r}")
    try:
        year = int(value[:4])
        quarter_number = int(value[5])
    except ValueError as exc:
        raise QuarterError(f"quarter must look like 'YYYYQN', got {value!r}") from exc
    if quarter_number not in (1, 2, 3, 4):
        raise QuarterError(f"quarter number must be 1-4, got {value!r}")
    return year, quarter_number


def quarter_bounds(value: str) -> tuple[date, date]:
    """The `[start, end]` calendar-day bounds (inclusive) of `value`."""
    year, quarter_number = parse_quarter(value)
    start_month, end_month = _QUARTER_MONTHS[quarter_number]
    start = date(year, start_month, 1)
    end = date(year, end_month, calendar.monthrange(year, end_month)[1])
    return start, end


def next_quarter(value: str) -> str:
    """The quarter immediately after `value`."""
    year, quarter_number = parse_quarter(value)
    if quarter_number == 4:
        return f"{year + 1:04d}Q1"
    return f"{year:04d}Q{quarter_number + 1}"


def quarter_range(start: str, end: str) -> list[str]:
    """Every quarter from `start` to `end`, inclusive. Raises
    `QuarterError` if `start` is after `end` or either is malformed."""
    parse_quarter(start)
    parse_quarter(end)
    if start > end:
        raise QuarterError(f"start quarter {start!r} is after end quarter {end!r}")
    quarters: list[str] = []
    current = start
    while current <= end:
        quarters.append(current)
        current = next_quarter(current)
    return quarters


def parse_quarters_arg(
    value: str | None,
    *,
    default_start: str = DEFAULT_START_QUARTER,
    default_end: str = DEFAULT_END_QUARTER,
) -> list[str]:
    """Parse `private-run --quarters` (a comma-separated `YYYYQN` list) for
    a smoke/scoped run, or -- if unset -- the full default range (the
    issue's own "2012Q1 -> 2026Q3" scope). Every parsed quarter is
    validated; the result is de-duplicated and sorted chronologically so
    callers never depend on the order the flag was typed in.
    """
    if value is None or not value.strip():
        return quarter_range(default_start, default_end)
    quarters = {q.strip() for q in value.split(",") if q.strip()}
    for q in quarters:
        parse_quarter(q)
    return sorted(quarters)
