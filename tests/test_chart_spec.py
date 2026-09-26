"""Unit tests for `project_health.site.chart_spec` (issue #28): the shared
x-domain/tick/low-n helpers behind the site's "default to the last 36
months, with a client-side Full history toggle" chart windowing and the
low-n/insufficient_data visual de-emphasis, independent of full site
generation (see `tests/test_site.py` for the end-to-end chart-spec tests)."""

from __future__ import annotations

from datetime import date

from project_health.site import chart_spec


def _months(count: int, *, start_year: int = 2010, start_month: int = 1) -> list[date]:
    """`count` distinct months' worth of dates, one per month, starting at
    `start_year`-`start_month`."""
    dates = []
    for i in range(count):
        month_index = (start_month - 1) + i
        year = start_year + month_index // 12
        month = month_index % 12 + 1
        dates.append(date(year, month, 28))
    return dates


def test_padded_month_domain_is_none_for_no_dates():
    assert chart_spec.padded_month_domain([]) is None


def test_recent_month_domain_equals_full_when_series_shorter_than_window():
    """A series shorter than `DEFAULT_WINDOW_MONTHS` is never truncated --
    the "recent" default and the full-history domain must be identical."""
    dates = _months(6)
    assert chart_spec.recent_month_domain(dates) == chart_spec.padded_month_domain(dates)


def test_recent_month_domain_clamps_to_the_window_for_a_longer_series():
    dates = _months(48)  # 4 years, well past the 36-month default window
    full = chart_spec.padded_month_domain(dates)
    recent = chart_spec.recent_month_domain(dates)
    assert recent is not None and full is not None
    assert recent[1] == full[1]  # same padded end (both cover the latest month)
    assert recent[0] > full[0]  # narrower start (issue #28's default window)


def test_recent_month_domain_respects_a_custom_months_argument():
    dates = _months(24)
    recent_12 = chart_spec.recent_month_domain(dates, months=12)
    recent_24 = chart_spec.recent_month_domain(dates, months=24)
    full = chart_spec.padded_month_domain(dates)
    assert recent_12[0] > recent_24[0]
    assert recent_24 == full  # 24-month window over a 24-month series == full


def test_chart_window_returns_none_for_no_dates():
    assert chart_spec.chart_window([]) is None


def test_chart_window_bundles_both_domains_and_tick_steps():
    dates = _months(96)  # 8 years
    window = chart_spec.chart_window(dates)
    assert window["domain"]["recent"] == chart_spec.recent_month_domain(dates)
    assert window["domain"]["full"] == chart_spec.padded_month_domain(dates)
    # The recent window's tick step is computed over its own (36-month, per
    # the default) span, not the full 96-month series' -- otherwise a
    # chart opening on the recent window would show far fewer ticks than
    # its own width warrants (issue #28).
    assert window["tickStep"]["recent"] == chart_spec.month_tick_step(36)
    assert window["tickStep"]["full"] == chart_spec.month_tick_step(96)
    assert window["tickStep"]["recent"] < window["tickStep"]["full"]


def test_month_tick_step_targets_roughly_six_ticks():
    assert chart_spec.month_tick_step(1) == 1
    assert chart_spec.month_tick_step(6) == 1
    assert chart_spec.month_tick_step(12) == 2
    assert chart_spec.month_tick_step(36) == 6


def test_recent_value_domain_ignores_an_out_of_window_outlier():
    """This is the actual bug issue #28 reports: a low-n outlier month (a
    median latency spike to ~1,200 days from a couple of closed issues)
    must not set the y-scale for the default (recent) view once it's
    scrolled out of the visible x-window -- restricting the x-domain alone
    doesn't do this, because Vega-Lite auto-fits the y-scale to *every*
    value in `data.values`, not just the ones inside the visible x-domain."""
    old_outlier_month = _months(1, start_year=2015, start_month=1)[0]
    recent_months = _months(36, start_year=2023, start_month=1)
    points = [(old_outlier_month, 1200.0)] + [(d, 14.0) for d in recent_months]

    domain = chart_spec.recent_value_domain(points)
    assert domain is not None
    lo, hi = domain
    assert lo == 0
    assert hi < 1200.0  # the outlier never enters this calculation
    assert hi > 14.0  # padded above the recent window's own peak


def test_recent_value_domain_none_when_recent_window_has_no_ok_values():
    old_ok_month = _months(1, start_year=2015, start_month=1)[0]
    recent_insufficient_months = _months(36, start_year=2023, start_month=1)
    points = [(old_ok_month, 5.0)] + [(d, None) for d in recent_insufficient_months]
    assert chart_spec.recent_value_domain(points) is None


def test_recent_value_domain_is_none_for_no_points():
    assert chart_spec.recent_value_domain([]) is None


def test_is_low_n_true_for_insufficient_data_regardless_of_n_or_value_kind():
    """`insufficient_data` is always de-emphasised, even if `n` happens to
    be large (it shouldn't be, per METRICS.md §0.6, but this must never
    depend on that) and even for a `"count"` metric."""
    assert chart_spec.is_low_n(1000, "insufficient_data", value_kind="days") is True
    assert chart_spec.is_low_n(1000, "insufficient_data", value_kind="count") is True


def test_is_low_n_thresholds_on_n_for_rate_ratio_and_latency_metrics():
    for value_kind in ("ratio", "percent", "days"):
        floor = chart_spec.LOW_N_DISPLAY_FLOOR
        assert chart_spec.is_low_n(floor - 1, "ok", value_kind=value_kind) is True
        assert chart_spec.is_low_n(floor, "ok", value_kind=value_kind) is False
        assert chart_spec.is_low_n(floor + 1, "ok", value_kind=value_kind) is False


def test_is_low_n_never_flags_an_ok_count_metric_regardless_of_n():
    """METRICS.md §0.6 (owner decision, issue #27): a plain headcount is
    already the complete, meaningful number at any `n` including 0 -- a
    month with 3 new contributors is real signal, not a noisy estimate.
    De-emphasising it as `low_n` would misrepresent it the same way an
    `insufficient_data` flag would (which the same decision forbids for
    counts)."""
    assert chart_spec.is_low_n(0, "ok", value_kind="count") is False
    assert chart_spec.is_low_n(3, "ok", value_kind="count") is False
    assert chart_spec.is_low_n(1_000_000, "ok", value_kind="count") is False


def test_low_n_opacity_encoding_conditions_on_the_low_n_datum_field():
    encoding = chart_spec.LOW_N_OPACITY_ENCODING
    assert encoding["condition"]["test"] == "datum.low_n"
    assert encoding["condition"]["value"] < encoding["value"]
