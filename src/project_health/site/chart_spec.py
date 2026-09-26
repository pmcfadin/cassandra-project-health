"""Shared Vega-Lite time-series chart-spec helpers (issue #28).

17 years of monthly points makes a chart dense, and a handful of
low-sample-size months (e.g. a median resolution latency computed from two
or three closed issues) can swing wide enough to flatten every other point
on the same y-scale. This module centralizes the fix so it's defined once,
not duplicated between `site/generate.py` (single-series M0/conversations
charts, `_vega_lite_spec`) and `site/governance_page.py` (multi-series
compliance-trend charts, `_trend_vega_spec`):

- every chart's x-domain defaults to the last `DEFAULT_WINDOW_MONTHS`
  months; the full history is still embedded in the spec's own `usermeta`
  so `static/app.js`'s "Full history" toggle can swap it in client-side,
  with no server round-trip and no change to the underlying data;
- a point is marked `low_n` (in `data.values`, never in `n`/`value`/`flag`
  themselves) when it's below `LOW_N_DISPLAY_FLOOR` -- a presentation-only
  threshold, separate from METRICS.md §0.6's `insufficient_data` floor,
  for a point that clears that floor but is still noisy enough to visually
  dominate a chart.

Nothing here changes a metric's `value`, `n`, or `flag` -- this is
presentation only (issue #28).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from project_health.metrics.windows import add_months

# All M0/conversations/governance-compliance charts are monthly windows; this
# is how many days before the earliest data month's start / after the latest
# data month's end the x-domain is padded (issue #16) -- enough that a
# single-point series doesn't sit on the plot's edge, and that the last
# point of a long series doesn't render flush against the right edge where
# its mark would otherwise clip.
X_DOMAIN_PAD_DAYS = 15

# Target roughly this many x-axis ticks regardless of how many months of
# history a series has (issue #16: "sensible tick count").
TARGET_TICK_COUNT = 6

# Default chart window (issue #28): a chart opens showing only its most
# recent 36 months, so 17 years of monthly points don't render as a dense
# wall of points by default. "Full history" (`static/app.js`) swaps in the
# complete domain client-side.
DEFAULT_WINDOW_MONTHS = 36

# Presentation-only de-emphasis floor (issue #28). METRICS.md §0.6's own
# insufficient_data floor (5, for rate/ratio/latency metrics) already keeps
# a too-small sample from ever being plotted as a real number; this is a
# *second*, purely visual threshold -- twice that floor -- for a point that
# clears insufficient_data but is still small enough that its statistic (a
# rate, an HHI, a median) is noisy. A median latency computed from 6 closed
# issues, for example, can swing by hundreds of days month to month and
# dwarf every other point on the chart even though it's a legitimately
# "ok"-flagged value. This never changes `n`, `value`, or `flag` -- only how
# faint the point renders; the real value is still in its tooltip.
LOW_N_DISPLAY_FLOOR = 10

# The point layer's opacity when `datum.low_n` is true (Vega-Lite condition
# test, `_low_n_opacity_encoding` below) -- faint enough to read as
# de-emphasised against a full-opacity point, without disappearing.
LOW_N_OPACITY = 0.35

# Vega-Lite encoding channel for the point layer's opacity: full opacity
# unless the datum's own `low_n` field (set in each chart's `data.values`,
# see `is_low_n`) is true.
LOW_N_OPACITY_ENCODING = {
    "condition": {"test": "datum.low_n", "value": LOW_N_OPACITY},
    "value": 1,
}


def month_floor(d: date) -> date:
    """The first day of `d`'s month."""
    return date(d.year, d.month, 1)


def month_ceil_exclusive(d: date) -> date:
    """The first day of the month *after* `d`'s month."""
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def month_tick_step(month_count: int, *, target_ticks: int = TARGET_TICK_COUNT) -> int:
    """Month interval between x-axis ticks, so a long history doesn't crowd
    the axis with one label per month (issue #16)."""
    return max(1, math.ceil((month_count or 1) / target_ticks))


def distinct_month_count(dates: list[date]) -> int:
    """How many distinct calendar months `dates` spans (at least 1)."""
    return len({(d.year, d.month) for d in dates}) or 1


def padded_month_domain(
    dates: list[date], *, pad_days: int = X_DOMAIN_PAD_DAYS
) -> list[str] | None:
    """A `[start, end]` ISO-date domain padded past `dates`'s actual month
    range, or `None` when `dates` is empty. See `generate._vega_lite_spec`'s
    former docstring (now here) for why padding is anchored to calendar-
    month boundaries rather than the raw dates themselves (issue #16, the
    "05 PM" bug)."""
    if not dates:
        return None
    pad = timedelta(days=pad_days)
    start = month_floor(min(dates)) - pad
    end = month_ceil_exclusive(max(dates)) + pad
    return [start.isoformat(), end.isoformat()]


def recent_month_domain(
    dates: list[date],
    *,
    months: int = DEFAULT_WINDOW_MONTHS,
    pad_days: int = X_DOMAIN_PAD_DAYS,
) -> list[str] | None:
    """Same padded domain as `padded_month_domain`, except the start is
    clamped to at most `months` calendar months before the data's latest
    month (issue #28's default chart window). When the series already
    covers fewer than `months` months, this is identical to
    `padded_month_domain` -- a short series is never artificially
    truncated."""
    if not dates:
        return None
    latest_month = month_floor(max(dates))
    earliest_month = month_floor(min(dates))
    cutoff = add_months(latest_month, -(months - 1))
    start_month = max(earliest_month, cutoff)
    pad = timedelta(days=pad_days)
    start = start_month - pad
    end = month_ceil_exclusive(max(dates)) + pad
    return [start.isoformat(), end.isoformat()]


def chart_window(dates: list[date], *, months: int = DEFAULT_WINDOW_MONTHS) -> dict | None:
    """Everything a chart's spec needs to default to a recent window while
    keeping full history one client-side toggle away (issue #28): both
    x-domains, plus both tick steps (`month_tick_step`, computed over the
    recent window's own month count vs. the full series') so the axis
    doesn't read as sparse when a 36-month window is applied to a series
    whose tick spacing was chosen for its full multi-year span.

    Returns `None` when `dates` is empty (nothing to plot, no domain to
    pick). The result is written into the spec's own `usermeta.chartWindow`
    (inert to Vega-Lite/vega-embed, which ignore unknown top-level keys) so
    `static/app.js`'s toggle can read it back out client-side without a
    second network request.
    """
    full_domain = padded_month_domain(dates)
    if full_domain is None:
        return None
    recent_domain = recent_month_domain(dates, months=months)
    full_months = distinct_month_count(dates)
    recent_months = min(full_months, months)
    return {
        "domain": {"recent": recent_domain, "full": full_domain},
        "tickStep": {
            "recent": month_tick_step(recent_months),
            "full": month_tick_step(full_months),
        },
    }


def recent_value_domain(
    points: list[tuple[date, float | None]],
    *,
    months: int = DEFAULT_WINDOW_MONTHS,
    pad_fraction: float = 0.1,
) -> list[float] | None:
    """A `[0, max]` y-scale domain, padded `pad_fraction` above the largest
    non-null value seen *within the last `months` calendar months* --
    never the whole series (issue #28).

    Restricting the x-domain to a recent window alone doesn't fix the
    flattening problem the issue describes: Vega-Lite's default y-scale
    auto-fits to every value in `data.values` regardless of which ones
    fall inside the visible x-domain, so a single low-n outlier month
    (e.g. a ~1,200-day median-latency spike computed from two closed
    issues) still stretches the y-axis and flattens every point that IS
    visible, even after that outlier scrolls out of view. This computes
    the y-domain from the *same* recent window the x-domain defaults to,
    so the default view is genuinely legible; `static/app.js`'s "Full
    history" toggle removes this override (falls back to `None`/auto) so
    switching to full history still shows the true, unflattened range --
    that's the point of asking for it.

    Returns `None` when there are no non-null values in the recent window
    (an all-insufficient_data recent window, or an empty series) -- callers
    fall back to Vega-Lite's own auto-scaling over the whole series,
    identical to this module's behavior before issue #28.
    """
    if not points:
        return None
    # The recency cutoff is anchored to the *series'* latest date -- every
    # point, `insufficient_data` (null-valued) ones included -- the same
    # date `recent_month_domain` anchors the x-domain to. Anchoring it to
    # the latest *non-null* date instead would be wrong: a series whose
    # most recent months are all `insufficient_data` would then treat an
    # old `ok` point as "recent" just because it's the newest one with a
    # value.
    latest_month = month_floor(max(d for d, _ in points))
    cutoff = add_months(latest_month, -(months - 1))
    in_window_values = [
        v for d, v in points if v is not None and month_floor(d) >= cutoff
    ]
    if not in_window_values:
        return None
    peak = max(in_window_values)
    if peak <= 0:
        return [0, 1]  # degenerate all-zero-or-negative window
    return [0, peak * (1 + pad_fraction)]


# `value_kind`s where `n` measures the sample size behind a *statistic*
# computed over a population -- a rate, a concentration index, a median --
# whose reliability genuinely depends on how many events/individuals went
# into it. Deliberately excludes `"count"`: METRICS.md §0.6's own owner
# decision (issue #27) is that a plain headcount is already the complete,
# meaningful number at any `n` including 0 -- "a month with 3 new
# contributors is real, meaningful signal ... rather than [an] estimate
# whose variance shrinks as `n` grows". De-emphasising a low headcount as
# if it were a noisy estimate would misrepresent it the same way
# `insufficient_data`-for-counts would (which METRICS.md §0.6 also
# forbids) -- so a count metric's points are never marked `low_n` here,
# regardless of `n`.
_LOW_N_ELIGIBLE_VALUE_KINDS = frozenset({"ratio", "percent", "days"})


def is_low_n(n: int, flag: str, *, value_kind: str, floor: int = LOW_N_DISPLAY_FLOOR) -> bool:
    """True when a plotted point should render de-emphasised (issue #28):
    an `insufficient_data` point, or an `ok` point whose `n` clears the
    `insufficient_data` floor but is still below this display floor --
    only for `value_kind`s where `n` is a sample size behind a computed
    statistic (`_LOW_N_ELIGIBLE_VALUE_KINDS`); a `"count"` metric's point
    is never marked low_n. Never changes `n`, `value`, or `flag` -- purely
    a rendering decision."""
    if flag != "ok":
        return True
    if value_kind not in _LOW_N_ELIGIBLE_VALUE_KINDS:
        return False
    return n < floor
