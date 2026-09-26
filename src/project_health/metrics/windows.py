"""Calendar-month and trailing-12m window helpers (METRICS.md, D5).

Kept dependency-free (stdlib `datetime.date` only) so the metrics engine can
reuse the same window arithmetic for every metric without pulling window
logic into SQL, where "which month" and "completed vs. in-progress" would be
harder to unit test in isolation.
"""

from __future__ import annotations

from datetime import date, timedelta


def month_start(d: date) -> date:
    """First day of `d`'s calendar month."""
    return date(d.year, d.month, 1)


def month_end(d: date) -> date:
    """Last day of `d`'s calendar month."""
    if d.month == 12:
        next_month = date(d.year + 1, 1, 1)
    else:
        next_month = date(d.year, d.month + 1, 1)
    return next_month - timedelta(days=1)


def add_months(d: date, n: int) -> date:
    """`d`'s month, shifted by `n` months (n may be negative), day fixed to 1."""
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def is_completed_month(month: date, as_of: date) -> bool:
    """True if `month` (any day within it) is a completed month relative to `as_of`.

    D5 / SCORING.md: "status calls use completed months only; partial months
    never drive status." A monthly window is never emitted for the calendar
    month containing `as_of` (or a later one).
    """
    return month_start(month) < month_start(as_of)


def trailing_12m_window(window_end: date) -> tuple[date, date]:
    """The trailing-12-calendar-month window ending at `window_end`.

    `window_end` is expected to be a month-end date (the last day of a
    completed month); the window start is the first day of the month 11
    months earlier, so the window spans exactly 12 calendar months.
    """
    start = add_months(month_start(window_end), -11)
    return start, window_end


def quarter_start(d: date) -> date:
    """First day of `d`'s calendar quarter (Q1/Q2/Q3/Q4)."""
    quarter_month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, quarter_month, 1)


def quarter_end(d: date) -> date:
    """Last day of `d`'s calendar quarter."""
    quarter_month = ((d.month - 1) // 3) * 3 + 3
    if quarter_month == 12:
        next_quarter = date(d.year + 1, 1, 1)
    else:
        next_quarter = date(d.year, quarter_month + 1, 1)
    return next_quarter - timedelta(days=1)


def add_quarters(d: date, n: int) -> date:
    """`d`'s quarter, shifted by `n` quarters (n may be negative), day fixed to 1."""
    month_index = d.month - 1 + (n * 3)
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def is_completed_quarter(quarter: date, as_of: date) -> bool:
    """True if `quarter` (any day within it) is a completed quarter relative to `as_of`.

    A quarterly window is never emitted for the calendar quarter containing
    `as_of` (or a later one).
    """
    return quarter_start(quarter) < quarter_start(as_of)


def dense_quarters(first_quarter: date | None, as_of: date) -> list[date]:
    """Every completed quarter from `first_quarter` through the last completed
    quarter before `as_of`, inclusive -- with no gaps.

    Returns `[]` when `first_quarter` is `None` (no data at all yet) or when
    `first_quarter` is itself not yet a completed quarter.
    """
    if first_quarter is None:
        return []
    last = quarter_start(as_of)
    last_completed = add_quarters(last, -1)
    first = quarter_start(first_quarter)
    if first > last_completed:
        return []
    quarters = []
    cursor = first
    while cursor <= last_completed:
        quarters.append(cursor)
        cursor = add_quarters(cursor, 1)
    return quarters
