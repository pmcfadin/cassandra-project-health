"""Presentation metadata for the M0 metrics the home page renders.

`id -> name, dimension, tier, direction_of_good, value_kind`, taken
straight from `docs/spec/METRICS.md` §1's summary table (plus each
metric's own units, described in its own section). Metric-computation
code (#7) doesn't need this map — it's display-only metadata (card
grouping, tier badge, chart title, number formatting), owned here in one
place rather than duplicated into every metric's `metric_value
.details_json`, so a wording, tier, or unit change is a one-line edit here
instead of a metrics-engine recompute.

M0 ships exactly these six metrics (ROADMAP.md §0); a metric_id present in
a `metric_value` snapshot but absent from this map is out of scope for the
home page and is silently not rendered.
"""

from __future__ import annotations

from dataclasses import dataclass

# d3-format specifiers (used verbatim as a Vega-Lite encoding `format`) and
# a human display formatter, keyed by `value_kind`. `.1%` in d3-format
# multiplies the value by 100 and appends `%`, so `stale_jira_rate` (stored
# as a 0-1 fraction, like `reviewer_hhi`) needs no pre-multiplication here.
_VEGA_FORMAT = {
    "count": ",.0f",
    "ratio": ".3f",
    "percent": ".1%",
    "days": ".1f",
}

# Y-axis tick label formats (issue #16 follow-up to #8: the axis showed a
# bare 0-1 fraction, e.g. "0.4", for percent metrics instead of the same
# unit the tooltip already uses). Distinct from `_VEGA_FORMAT` because axis
# ticks favor terser labels than a tooltip can afford — percent rounds to a
# whole number (`.0%` -> "40%") instead of the tooltip's one-decimal
# precision.
_AXIS_FORMAT = {
    "count": ",.0f",
    "ratio": ".3f",
    "percent": ".0%",
    "days": ".1f",
}

# A Vega-Lite axis `labelExpr` suffix appended after `_AXIS_FORMAT` renders
# each tick, for value_kinds whose formatted number alone is ambiguous
# without a unit (a bare "14.2" tick reads as nothing in particular, unlike
# "40%" or "0.350", which are already self-describing).
_AXIS_LABEL_SUFFIX = {
    "days": " d",
}


@dataclass(frozen=True)
class MetricMeta:
    metric_id: str
    name: str
    dimension: str
    # tier: 'established' | 'proxy' | 'experimental' | 'classified' (METRICS.md §0.1)
    tier: str
    # direction_of_good: 'higher' | 'lower' | 'target-range' | 'none' (METRICS.md §1)
    direction_of_good: str
    # value_kind: how the metric's numeric value is displayed —
    #   'count'   -> integer, no unit suffix, e.g. "12"
    #   'ratio'   -> 3 decimal places on a 0-1 scale, e.g. "0.350"
    #   'percent' -> a 0-1 fraction shown as a percentage, e.g. "12.3%"
    #   'days'    -> 1 decimal place with a " days" suffix, e.g. "14.2 days"
    value_kind: str

    def format_value(self, value: float) -> str:
        """Format `value` for display (the card's big number)."""
        if self.value_kind == "count":
            return f"{value:,.0f}"
        if self.value_kind == "ratio":
            return f"{value:.3f}"
        if self.value_kind == "percent":
            return f"{value * 100:.1f}%"
        if self.value_kind == "days":
            return f"{value:.1f} days"
        raise ValueError(f"unknown value_kind {self.value_kind!r}")

    @property
    def vega_format(self) -> str:
        """d3-format specifier for this metric's value in a chart tooltip."""
        return _VEGA_FORMAT[self.value_kind]

    @property
    def axis_format(self) -> str:
        """d3-format specifier for this metric's Y-axis tick labels.

        Applied to the axis itself (issue #16), not just the tooltip, so a
        percent metric's axis reads "40%" rather than "0.4".
        """
        return _AXIS_FORMAT[self.value_kind]

    @property
    def axis_label_expr(self) -> str | None:
        """Vega-Lite axis `labelExpr` that appends a unit suffix to the
        `axis_format`-formatted tick label (e.g. "14.2" -> "14.2 d"), or
        `None` when `axis_format` alone is unambiguous."""
        suffix = _AXIS_LABEL_SUFFIX.get(self.value_kind)
        if suffix is None:
            return None
        return f"datum.label + {suffix!r}"

    @property
    def tooltip_title(self) -> str:
        """Tooltip label for this metric's value, with units spelled out
        wherever the formatted number alone would be ambiguous."""
        if self.value_kind == "ratio":
            return f"{self.name} (0-1)"
        if self.value_kind == "percent":
            return f"{self.name} (%)"
        if self.value_kind == "days":
            return f"{self.name} (days)"
        return self.name


# Card display order: grouped by dimension (contributor sustainability,
# reviewer capacity, responsiveness), matching the issue's "cards for
# contributor sustainability, reviewer capacity, responsiveness."
M0_METRICS: dict[str, MetricMeta] = {
    "active_contributors_monthly": MetricMeta(
        metric_id="active_contributors_monthly",
        name="Active Contributors",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="higher",
        value_kind="count",
    ),
    "new_contributors_monthly": MetricMeta(
        metric_id="new_contributors_monthly",
        name="New Contributors",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="higher",
        value_kind="count",
    ),
    "unique_reviewers_monthly": MetricMeta(
        metric_id="unique_reviewers_monthly",
        name="Unique Reviewers",
        dimension="reviewer capacity",
        tier="proxy",
        direction_of_good="higher",
        value_kind="count",
    ),
    "reviewer_hhi": MetricMeta(
        metric_id="reviewer_hhi",
        name="Reviewer Concentration (HHI)",
        dimension="reviewer capacity",
        tier="proxy",
        direction_of_good="lower",
        value_kind="ratio",
    ),
    "median_resolution_latency_jira": MetricMeta(
        metric_id="median_resolution_latency_jira",
        name="Median JIRA Resolution Latency",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="days",
    ),
    "stale_jira_rate": MetricMeta(
        metric_id="stale_jira_rate",
        name="Stale JIRA Issue Rate",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="percent",
    ),
}
