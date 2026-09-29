"""Tests for project_health.private_run.quarters (issue #110)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from project_health.private_run.quarters import (
    DEFAULT_END_QUARTER,
    DEFAULT_START_QUARTER,
    QuarterError,
    next_quarter,
    parse_quarter,
    parse_quarters_arg,
    quarter_bounds,
    quarter_of,
    quarter_range,
)


class TestQuarterOf:
    @pytest.mark.parametrize(
        "month,expected",
        [(1, "2024Q1"), (3, "2024Q1"), (4, "2024Q2"), (9, "2024Q3"), (12, "2024Q4")],
    )
    def test_maps_month_to_quarter(self, month, expected):
        assert quarter_of(datetime(2024, month, 15, tzinfo=timezone.utc)) == expected

    def test_accepts_plain_date(self):
        assert quarter_of(date(2026, 9, 28)) == "2026Q3"


class TestParseQuarter:
    def test_parses_valid_quarter(self):
        assert parse_quarter("2024Q3") == (2024, 3)

    @pytest.mark.parametrize("bad", ["2024", "2024-Q1", "2024Q5", "2024Q0", "abcdQ1", "2024q1"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(QuarterError):
            parse_quarter(bad)


class TestQuarterBounds:
    def test_q1_bounds(self):
        assert quarter_bounds("2024Q1") == (date(2024, 1, 1), date(2024, 3, 31))

    def test_q4_bounds_leap_year_insensitive(self):
        assert quarter_bounds("2024Q4") == (date(2024, 10, 1), date(2024, 12, 31))

    def test_q1_bounds_includes_leap_day(self):
        # 2024 is a leap year; Q1 end must be Mar 31 regardless (Feb handled
        # by calendar.monthrange inside quarter_bounds, not hand-rolled).
        assert quarter_bounds("2024Q1")[1] == date(2024, 3, 31)


class TestNextQuarter:
    def test_within_year(self):
        assert next_quarter("2024Q1") == "2024Q2"

    def test_rolls_over_year(self):
        assert next_quarter("2024Q4") == "2025Q1"


class TestQuarterRange:
    def test_single_year(self):
        assert quarter_range("2024Q1", "2024Q4") == ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]

    def test_spans_years(self):
        assert quarter_range("2024Q4", "2025Q2") == ["2024Q4", "2025Q1", "2025Q2"]

    def test_single_quarter(self):
        assert quarter_range("2024Q2", "2024Q2") == ["2024Q2"]

    def test_start_after_end_raises(self):
        with pytest.raises(QuarterError):
            quarter_range("2024Q4", "2024Q1")

    def test_default_scope_matches_issue_110(self):
        quarters = quarter_range(DEFAULT_START_QUARTER, DEFAULT_END_QUARTER)
        assert quarters[0] == "2012Q1"
        assert quarters[-1] == "2026Q3"
        # 2012Q1..2025Q4 is 14 full years (56 quarters) + 2026Q1..Q3 (3) = 59.
        assert len(quarters) == 59


class TestParseQuartersArg:
    def test_none_returns_default_full_range(self):
        assert parse_quarters_arg(None) == quarter_range(DEFAULT_START_QUARTER, DEFAULT_END_QUARTER)

    def test_empty_string_returns_default_full_range(self):
        assert parse_quarters_arg("   ") == quarter_range(
            DEFAULT_START_QUARTER, DEFAULT_END_QUARTER
        )

    def test_parses_and_sorts_comma_separated_list(self):
        assert parse_quarters_arg("2025Q2,2024Q1") == ["2024Q1", "2025Q2"]

    def test_dedupes(self):
        assert parse_quarters_arg("2024Q1,2024Q1, 2024Q1") == ["2024Q1"]

    def test_rejects_malformed_entry(self):
        with pytest.raises(QuarterError):
            parse_quarters_arg("2024Q1,not-a-quarter")
