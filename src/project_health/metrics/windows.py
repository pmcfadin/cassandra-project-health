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
