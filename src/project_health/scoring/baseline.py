"""Per-metric baseline status math (`docs/spec/SCORING.md` §4-§5, issue #57).

Pure functions over a plain `{month_start: value}` mapping (only months with
a usable, `flag == 'ok'` value in the accumulated `metric_value` snapshot --
see `engine.py` for how that mapping is built from the real snapshot table),
so this module is fully unit-testable without pyarrow/DuckDB in the loop.

Implements, in order:
- §4.2: median/MAD baseline and the modified z-score, with the documented
  MAD=0 fallback.
- §4.3: `direction_of_good` handling (`higher`/`lower`/`target-range`/`none`).
- §5.1: the four-status classification rule (`improving`/`stable`/
  `declining`/`insufficient_data`), including the 12-completed-month floor
  and the "2 of last 3 months" sustained-trend confirmation, and the
  "notable single-month event" flag for a lone `|z| >= 3.0` month.
- §5.3's own worked-example note that a `target-range` metric (only
  `release_frequency` today) has no "improving" reading -- see
  `scoring.yaml`'s `baseline.target_range_declining_only` for the disclosed,
  versioned rule this module applies for that case (SCORING.md itself does
  not spell out the exact classification for `target-range`).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

from project_health.metrics.windows import add_months
from project_health.scoring.config import BaselineConfig

# The Western-Electric-derived modified z-score constant (SCORING.md §4.2):
# rescales MAD to estimate a standard deviation under a normal distribution.
MODIFIED_Z_CONSTANT = 0.6745

DIRECTIONS_CLASSIFIED = ("higher", "lower", "target-range")


@dataclass(frozen=True)
class BaselineStatusResult:
    metric_id: str
    window_end: date
    current_value: float | None
    baseline_median: float | None
    baseline_mad: float | None
    baseline_months: int
    modified_z: float | None
    status: str  # 'improving' | 'stable' | 'declining' | 'insufficient_data'
    confirmed: bool | None
    notable_single_month_event: bool | None


def directional_z(modified_z: float | None, direction_of_good: str) -> float | None:
    """`modified_z`, sign-adjusted so a positive result always means "moving
    toward improving" -- `None` for `direction_of_good == 'none'` (never
    classified, SCORING.md §5.1 rule 1) or when `modified_z` itself is
    `None`. `target-range` has no "toward improving" side (§4.3), so this
    returns the *unsigned magnitude* for it -- callers that need the sign for
    a `higher`/`lower` metric only should check `direction_of_good` first.
    """
    if modified_z is None or direction_of_good == "none":
        return None
    if direction_of_good == "lower":
        return -modified_z
    if direction_of_good == "target-range":
        return abs(modified_z)
    return modified_z  # 'higher'


def _median_mad(values: list[float]) -> tuple[float, float]:
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    return median, mad


def _baseline_pool(
    monthly_values: dict[date, float], target_month: date, config: BaselineConfig
) -> list[float]:
    """Every value from the `trailing_months` completed months strictly
    before `target_month` that has a usable entry in `monthly_values`
    (SCORING.md §4.1). Gaps (a month with no `ok` value) simply contribute
    nothing -- they are not synthesized as zero or interpolated."""
    earliest = add_months(target_month, -config.trailing_months)
    return [
        value
        for month, value in monthly_values.items()
        if earliest <= month < target_month
    ]


def _modified_z_for_month(
    monthly_values: dict[date, float], target_month: date, config: BaselineConfig, metric_id: str
) -> tuple[float | None, float | None, float | None, int]:
    """`(modified_z, baseline_median, baseline_mad, baseline_months)` for
    `target_month`, or `(None, None, None, n)` if `target_month` has no
    usable value or the preceding baseline pool is below
    `min_completed_months` (SCORING.md §5.1 rule 1).

    MAD = 0 fallback (§4.2): compares the raw magnitude of
    `current - median` against `config.mad_zero_floor(metric_id)`. Within
    the floor, `modified_z = 0.0` (stable). Beyond it, this project's
    disclosed convention (`scoring.yaml`'s `baseline.mad_zero_floor_default`
    comment) is to treat *any* confirmed departure from a perfectly flat
    24-month baseline as maximally anomalous relative to that baseline:
    `modified_z` is set to +/- `large_deviation_threshold` (sign matching the
    direction of the raw difference), which both drives the same
    classification path as a real 3-sigma reading and saturates the
    composite's 0-100 normalization the same way.
    """
    current = monthly_values.get(target_month)
    if current is None:
        return None, None, None, 0
    pool = _baseline_pool(monthly_values, target_month, config)
    if len(pool) < config.min_completed_months:
        return None, None, None, len(pool)

    median, mad = _median_mad(pool)
    if mad > 0:
        modified_z = MODIFIED_Z_CONSTANT * (current - median) / mad
    else:
        diff = current - median
        floor = config.mad_zero_floor(metric_id)
        if abs(diff) <= floor:
            modified_z = 0.0
        else:
            modified_z = (
                config.large_deviation_threshold if diff > 0 else -config.large_deviation_threshold
            )
    return modified_z, median, mad, len(pool)


def compute_baseline_status(
    metric_id: str,
    direction_of_good: str,
    monthly_values: dict[date, float],
    config: BaselineConfig,
) -> BaselineStatusResult | None:
    """The current (most recent) month's baseline status for one metric.

    `monthly_values` maps completed-month `date`s (the first day of the
    month) to the metric's value for that month, containing only months
    where the source `metric_value` row had `flag == 'ok'` -- a month with
    `insufficient_data` or no row at all is simply absent from this mapping.

    Returns `None` if `monthly_values` is empty (no usable data for this
    metric at all yet) -- there is no month to report a status *for* in that
    case, so no row is produced (mirrors `metrics/engine.py`'s own "nothing
    to report yet" convention of returning no rows rather than a placeholder).
    """
    if not monthly_values:
        return None

    window_end = max(monthly_values)
    current_value = monthly_values[window_end]

    if direction_of_good not in DIRECTIONS_CLASSIFIED:
        # 'none' direction: informative but never classified (SCORING.md
        # §5.1 rule 1) -- shown as insufficient_data, not a 5th status.
        return BaselineStatusResult(
            metric_id=metric_id,
            window_end=window_end,
            current_value=current_value,
            baseline_median=None,
            baseline_mad=None,
            baseline_months=0,
            modified_z=None,
            status="insufficient_data",
            confirmed=None,
            notable_single_month_event=None,
        )

    modified_z, median, mad, baseline_months = _modified_z_for_month(
        monthly_values, window_end, config, metric_id
    )
    if modified_z is None:
        return BaselineStatusResult(
            metric_id=metric_id,
            window_end=window_end,
            current_value=current_value,
            baseline_median=median,
            baseline_mad=mad,
            baseline_months=baseline_months,
            modified_z=None,
            status="insufficient_data",
            confirmed=None,
            notable_single_month_event=None,
        )

    signed = directional_z(modified_z, direction_of_good)
    assert signed is not None  # direction_of_good is classified here
    deviating = abs(signed) >= config.stable_threshold
    notable = abs(modified_z) >= config.large_deviation_threshold

    if not deviating:
        return BaselineStatusResult(
            metric_id=metric_id,
            window_end=window_end,
            current_value=current_value,
            baseline_median=median,
            baseline_mad=mad,
            baseline_months=baseline_months,
            modified_z=modified_z,
            status="stable",
            confirmed=False,
            notable_single_month_event=False,
        )

    # Deviating this month -- check the "2 of last `confirmation_window_months`"
    # sustained-trend override (SCORING.md §5.1 rule 4). A prior month whose
    # own preceding baseline doesn't clear `min_completed_months` simply
    # doesn't count toward the tally (neither confirming nor breaking it) --
    # SCORING.md doesn't specify this edge case explicitly; this is this
    # project's documented, disclosed choice (see module docstring).
    confirmations = 1  # the current month itself
    for offset in range(1, config.confirmation_window_months):
        prior_month = add_months(window_end, -offset)
        prior_z, _, _, _ = _modified_z_for_month(monthly_values, prior_month, config, metric_id)
        if prior_z is None:
            continue
        prior_signed = directional_z(prior_z, direction_of_good)
        if prior_signed is None:
            continue
        threshold = config.stable_threshold
        same_declining_side = prior_signed <= -threshold and signed <= -threshold
        same_improving_side = prior_signed >= threshold and signed >= threshold
        if same_declining_side or same_improving_side:
            confirmations += 1

    confirmed = confirmations >= config.confirmation_required
    if confirmed:
        if direction_of_good == "target-range":
            status = "declining"  # never "improving" -- neither extreme is healthy (§4.3)
        else:
            status = "improving" if signed > 0 else "declining"
    else:
        status = "stable"

    return BaselineStatusResult(
        metric_id=metric_id,
        window_end=window_end,
        current_value=current_value,
        baseline_median=median,
        baseline_mad=mad,
        baseline_months=baseline_months,
        modified_z=modified_z,
        status=status,
        confirmed=confirmed,
        notable_single_month_event=notable,
    )
