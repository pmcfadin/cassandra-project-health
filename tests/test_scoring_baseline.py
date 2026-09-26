"""Golden tests for `scoring/baseline.py` (issue #57, `docs/spec/SCORING.md`
§4-§5): the median/MAD baseline, the modified z-score (including its MAD=0
fallback), the improving/stable/declining/insufficient_data status rule, the
12-completed-month floor, and the "2 of last 3 months" sustained-trend
confirmation.

Test fixtures use a deliberately hand-verifiable baseline: 23 or 24
consecutive completed months valued `10.0, 11.0, 12.0, ...` (strictly
increasing, no ties -- an evenly alternating two-value baseline turns out to
produce degenerate `MAD == 0` sub-windows for several of the 23/24-month
lookbacks this module recomputes, which would make the "2 of 3" test's
intermediate months accidentally hit the MAD=0 fallback path instead of the
real modified-z path being tested). For 23 consecutive values `10..32`:
`median = 21`, `MAD = median(|x - 21|) = 6` (by hand: sorted absolute
deviations are one 0 then the pairs 1..11, so the 12th of 23 is 6). For 24
consecutive values `10..33`: `median = 21.5`, `MAD = 6.0` (by hand: 24 paired
absolute deviations 0.5, 0.5, 1.5, 1.5, ..., 11.5, 11.5; the 12th/13th sorted
values are 5.5/6.5, averaging to 6.0).
"""

from __future__ import annotations

from datetime import date

import pytest

from project_health.metrics.windows import add_months
from project_health.scoring.baseline import compute_baseline_status, directional_z
from project_health.scoring.config import load_scoring_config

BASE = date(2023, 1, 1)
MONTHS = [add_months(BASE, i) for i in range(30)]
CONFIG = load_scoring_config().baseline


def _clean_baseline(n: int) -> dict[date, float]:
    """`n` consecutive completed months, `MONTHS[0..n-1]`, valued `10.0, 11.0,
    ..., 10.0 + n - 1` -- see module docstring for why strictly-increasing,
    not alternating."""
    return {MONTHS[i]: float(10 + i) for i in range(n)}


# --- Median/MAD band + basic classification (SCORING.md §5.1 rules 1-3) ----


def test_stable_within_the_1_5_sigma_band():
    """A current value 0.5 above a median-21.5/MAD-6.0 baseline: modified_z =
    0.6745 * (22.0 - 21.5) / 6.0 = 0.056208... -- well inside `|z| < 1.5`."""
    values = _clean_baseline(24)
    values[MONTHS[24]] = 21.5 + 0.5
    result = compute_baseline_status("m", "lower", values, CONFIG)
    assert result.baseline_median == pytest.approx(21.5)
    assert result.baseline_mad == pytest.approx(6.0)
    assert result.modified_z == pytest.approx(0.6745 * 0.5 / 6.0)
    assert result.status == "stable"
    assert result.confirmed is False
    assert result.notable_single_month_event is False


def test_declining_single_month_not_yet_confirmed_but_flagged_notable():
    """A single month at modified_z = 8.82 (hand: 0.6745 * (100 - 21.5) / 6.0)
    is a real deviation (`|z| >= 1.5`) and a "large deviation" (`|z| >= 3.0`),
    but with no confirming prior month it stays `stable` for the *displayed*
    status (SCORING.md §5.1 rule 4) while still being flagged as a notable
    single-month event -- "a real shock is never hidden for two months
    waiting for confirmation."
    """
    values = _clean_baseline(24)
    values[MONTHS[24]] = 100.0
    result = compute_baseline_status("m", "lower", values, CONFIG)
    assert result.modified_z == pytest.approx(0.6745 * (100.0 - 21.5) / 6.0)
    assert result.status == "stable"
    assert result.confirmed is False
    assert result.notable_single_month_event is True


def test_declining_confirmed_2_of_3_months():
    """T-1 (`MONTHS[23]`) is set to 100.0 against its own *clean* 23-month
    baseline (`MONTHS[0..22]`, values 10..32: median 21, MAD 6 by hand) --
    modified_z = 0.6745 * (100 - 21) / 6 = 8.876 (isolated, uncontaminated
    computation) -- a confirmed deviation in its own right. The current
    month is also set to 100.0. 2 of the last 3 completed months (current
    and T-1) now clear the 1.5-sigma line in the same (declining) direction,
    so the *displayed* status promotes from stable to declining."""
    values = _clean_baseline(23)
    values[MONTHS[23]] = 100.0  # T-1
    values[MONTHS[24]] = 100.0  # current
    result = compute_baseline_status("m", "lower", values, CONFIG)
    assert result.status == "declining"
    assert result.confirmed is True
    assert result.notable_single_month_event is True


def test_improving_direction_higher_confirmed():
    """Same shape as the declining-confirmed case, `direction_of_good =
    'higher'` instead: a rise is favorable, so 2-of-3 confirmed deviation
    reads `improving`, never `declining`."""
    values = _clean_baseline(23)
    values[MONTHS[23]] = 100.0
    values[MONTHS[24]] = 100.0
    result = compute_baseline_status("m", "higher", values, CONFIG)
    assert result.status == "improving"
    assert result.confirmed is True


def test_stable_when_no_key_confirmation_and_within_band_directional_z():
    """`directional_z` sign-flips a `lower` metric's raw z so a positive
    result always means "toward improving" -- sanity-checked directly."""
    assert directional_z(2.0, "higher") == 2.0
    assert directional_z(2.0, "lower") == -2.0
    assert directional_z(2.0, "target-range") == 2.0
    assert directional_z(-2.0, "target-range") == 2.0
    assert directional_z(2.0, "none") is None
    assert directional_z(None, "higher") is None


# --- target-range direction (SCORING.md §4.3; no "improving" side) --------


def test_target_range_deviation_below_range_is_declining_never_improving():
    values = _clean_baseline(23)
    values[MONTHS[23]] = -100.0
    values[MONTHS[24]] = -100.0
    result = compute_baseline_status("m", "target-range", values, CONFIG)
    assert result.status == "declining"
    assert result.confirmed is True


def test_target_range_deviation_above_range_is_also_declining():
    """Neither extreme is healthy for a target-range metric (SCORING.md
    §4.3) -- a confirmed deviation *above* the historical range reads
    `declining`, exactly like a deviation below it, never `improving`."""
    values = _clean_baseline(23)
    values[MONTHS[23]] = 200.0
    values[MONTHS[24]] = 200.0
    result = compute_baseline_status("m", "target-range", values, CONFIG)
    assert result.status == "declining"
    assert result.confirmed is True


# --- `none` direction: never classified (SCORING.md §5.1 rule 1) ----------


def test_none_direction_is_always_insufficient_data():
    result = compute_baseline_status("m", "none", _clean_baseline(24), CONFIG)
    assert result.status == "insufficient_data"
    assert result.modified_z is None


# --- The 12-completed-month floor (SCORING.md §5.1 rule 1) ----------------


def test_fewer_than_the_12_month_floor_is_insufficient_data():
    """8 months of baseline history (well under `min_completed_months = 12`)
    -- insufficient_data, regardless of how extreme the current value is."""
    values = {MONTHS[i]: float(10 + i) for i in range(8)}
    values[MONTHS[8]] = 50.0
    result = compute_baseline_status("m", "lower", values, CONFIG)
    assert result.status == "insufficient_data"
    assert result.baseline_months == 8


def test_partial_baseline_between_12_and_24_months_still_classifies():
    """SCORING.md §4.1: a metric whose own history is shorter than the full
    24-month window still gets a real, classifiable "partial baseline" once
    it clears the 12-month floor -- 15 months here, not insufficient_data."""
    values = {MONTHS[i]: float(10 + i) for i in range(15)}
    values[MONTHS[15]] = 200.0
    result = compute_baseline_status("m", "lower", values, CONFIG)
    assert result.baseline_months == 15
    assert result.status != "insufficient_data"


def test_no_data_at_all_returns_no_row():
    assert compute_baseline_status("m", "lower", {}, CONFIG) is None


# --- MAD = 0 fallback (SCORING.md §4.2) ------------------------------------


def test_mad_zero_fallback_no_meaningful_change_is_stable():
    flat = {MONTHS[i]: 5.0 for i in range(24)}
    flat[MONTHS[24]] = 5.0  # identical to the flat baseline -- diff = 0
    result = compute_baseline_status("m", "lower", flat, CONFIG)
    assert result.baseline_mad == 0.0
    assert result.modified_z == 0.0
    assert result.status == "stable"
    assert result.notable_single_month_event is False


def test_mad_zero_fallback_any_nonzero_change_is_a_large_deviation():
    """Beyond the (default 0.0) mad_zero_floor, this project's disclosed
    convention (`scoring.yaml`) treats a confirmed departure from a
    perfectly flat baseline as maximally anomalous: `modified_z` saturates
    at `large_deviation_threshold`. With no confirming prior month, the
    *displayed* status still stays `stable`, flagged as notable -- same
    rule 4 behavior as a real 8.8-sigma reading above."""
    flat = {MONTHS[i]: 5.0 for i in range(24)}
    flat[MONTHS[24]] = 5.1
    result = compute_baseline_status("m", "lower", flat, CONFIG)
    assert result.baseline_mad == 0.0
    assert result.modified_z == pytest.approx(CONFIG.large_deviation_threshold)
    assert result.status == "stable"
    assert result.confirmed is False
    assert result.notable_single_month_event is True
