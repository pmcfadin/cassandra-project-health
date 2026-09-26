"""Presentation metadata for the M0 metrics the site renders.

`id -> name, dimension, tier, direction_of_good, value_kind, page`, taken
straight from `docs/spec/METRICS.md` §1's summary table (plus each
metric's own units, described in its own section). Metric-computation
code (#7) doesn't need this map — it's display-only metadata (card
grouping, tier badge, chart title, number formatting, which site page it
renders on), owned here in one place rather than duplicated into every
metric's `metric_value.details_json`, so a wording, tier, unit, or
page-placement change is a one-line edit here instead of a metrics-engine
recompute.

`page` (D13, issue #34) is the site page a metric's card renders on
(`"community"`, `"conversations"`, or `"governance"`); the generator
(`site/generate.py`) groups metrics by this field, so a new metric only
needs to declare its page here to show up in the right place.

M0 ships exactly these six metrics (ROADMAP.md §0), all on the community
page; a metric_id present in a `metric_value` snapshot but absent from
this map is out of scope for the site and is silently not rendered.
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
    # page: which site page (D13) renders this metric's card —
    # 'community' | 'conversations' | 'governance'.
    page: str

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
        page="community",
    ),
    "new_contributors_monthly": MetricMeta(
        metric_id="new_contributors_monthly",
        name="New Contributors",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="higher",
        value_kind="count",
        page="community",
    ),
    "unique_reviewers_monthly": MetricMeta(
        metric_id="unique_reviewers_monthly",
        name="Unique Reviewers",
        dimension="reviewer capacity",
        tier="proxy",
        direction_of_good="higher",
        value_kind="count",
        page="community",
    ),
    "reviewer_hhi": MetricMeta(
        metric_id="reviewer_hhi",
        name="Reviewer Concentration (HHI)",
        dimension="reviewer capacity",
        tier="proxy",
        direction_of_good="lower",
        value_kind="ratio",
        page="community",
    ),
    "median_resolution_latency_jira": MetricMeta(
        metric_id="median_resolution_latency_jira",
        name="Median JIRA Resolution Latency",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="days",
        page="community",
    ),
    "stale_jira_rate": MetricMeta(
        metric_id="stale_jira_rate",
        name="Stale JIRA Issue Rate",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="percent",
        page="community",
    ),
}


@dataclass(frozen=True)
class PageMeta:
    """Presentation metadata for one of the site's top-level pages (D13,
    issue #34): its nav label, its directory (relative to the site root,
    trailing slash), and the one-line blurb the home page's summary card
    shows above that page's headline metrics."""

    page_id: str
    title: str
    path: str
    summary: str
    # Shown on the home page's summary card in place of headline metrics
    # when this page has none published yet (an honest empty state, not a
    # broken-looking blank card).
    empty_message: str


# The site's top-level pages, in nav/home-card order (D13). `page_id` is
# what `MetricMeta.page` values match against; a metric whose `page` isn't
# a key here is a bug, not a silent drop (see `generate._group_by_page`).
PAGES: dict[str, PageMeta] = {
    "community": PageMeta(
        page_id="community",
        title="Community",
        path="community/",
        summary="Code and contributor health: sustainability, review capacity, and responsiveness.",
        empty_message="No metrics published yet.",
    ),
    "conversations": PageMeta(
        page_id="conversations",
        title="Conversations",
        path="conversations/",
        summary="Mailing-list metrics, computed from metadata only — never message content.",
        empty_message="No conversation metrics yet — mailing-list collection is in progress.",
    ),
    "governance": PageMeta(
        page_id="governance",
        title="Governance",
        path="governance/",
        summary="Per-commit minimums checked against an owner-approved policy.",
        empty_message="Governance policy in development — no compliance results yet.",
    ),
}

# Home-page summary cards show at most this many headline metrics per page
# before linking through for the rest (D13: "one summary card per page:
# its headline metrics ... and a link").
HOME_CARD_METRIC_LIMIT = 3
