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

M0 shipped six metrics (ROADMAP.md §0); issue #53 adds three more
(`truck_factor`, `contributor_absence_factor`, `contributor_hhi`), also on
the community page. A metric_id present in a `metric_value` snapshot but
absent from this map is out of scope for the site and is silently not
rendered.
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
    # issue #53
    "truck_factor": MetricMeta(
        metric_id="truck_factor",
        name="Truck Factor (DOA)",
        dimension="contributor sustainability",
        tier="experimental",
        direction_of_good="higher",
        value_kind="count",
        page="community",
    ),
    "contributor_absence_factor": MetricMeta(
        metric_id="contributor_absence_factor",
        name="Contributor Dependency (N for 50%)",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="higher",
        value_kind="count",
        page="community",
    ),
    "contributor_hhi": MetricMeta(
        metric_id="contributor_hhi",
        name="Contributor Concentration (HHI)",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="lower",
        value_kind="ratio",
        page="community",
    ),
    # issue #52 (D6 organizational-diversity metrics, METRICS.md §5)
    "elephant_factor": MetricMeta(
        metric_id="elephant_factor",
        name="Elephant Factor",
        dimension="organizational diversity",
        tier="experimental",
        direction_of_good="higher",
        value_kind="count",
        page="community",
    ),
    "organizational_hhi": MetricMeta(
        metric_id="organizational_hhi",
        name="Organizational Concentration (HHI)",
        dimension="organizational diversity",
        tier="established",
        direction_of_good="lower",
        value_kind="ratio",
        page="community",
    ),
    "single_org_share": MetricMeta(
        metric_id="single_org_share",
        name="Single-Organization Share",
        dimension="organizational diversity",
        tier="established",
        direction_of_good="lower",
        value_kind="percent",
        page="community",
    ),
    "unknown_affiliation_rate": MetricMeta(
        metric_id="unknown_affiliation_rate",
        name="Unknown-Affiliation Rate",
        dimension="organizational diversity",
        tier="established",
        direction_of_good="none",
        value_kind="percent",
        page="community",
    ),
}

# Governance compliance engine (issue #36, D14/D15): one monthly pass-rate
# card per scored governance-policy.yaml check
# (`governance/metrics.py::compute_monthly_check_metrics`,
# `governance/registry.py` registers these at definition_version "1.0").
# Presentation metadata only, registered here per the issue's acceptance
# criterion ("registered at v1.0 on the Governance page, page='governance'
# in metrics_meta") — building the `/governance/` page itself (reading these
# out of a snapshot, rendering the per-commit table) is issue #37's scope,
# not this one's.
GOVERNANCE_METRICS: dict[str, MetricMeta] = {
    "governance_reviewer_present_pass_rate": MetricMeta(
        metric_id="governance_reviewer_present_pass_rate",
        name="Reviewer Present — Pass Rate",
        dimension="governance",
        tier="established",
        direction_of_good="higher",
        value_kind="percent",
        page="governance",
    ),
    "governance_jira_ticket_referenced_pass_rate": MetricMeta(
        metric_id="governance_jira_ticket_referenced_pass_rate",
        name="JIRA Ticket Referenced — Pass Rate",
        dimension="governance",
        tier="established",
        direction_of_good="higher",
        value_kind="percent",
        page="governance",
    ),
    "governance_pre_commit_ci_evidence_pass_rate": MetricMeta(
        metric_id="governance_pre_commit_ci_evidence_pass_rate",
        name="Pre-Commit CI Evidence — Pass Rate",
        dimension="governance",
        tier="proxy",
        direction_of_good="higher",
        value_kind="percent",
        page="governance",
    ),
    "governance_code_style_checkstyle_pass_rate": MetricMeta(
        metric_id="governance_code_style_checkstyle_pass_rate",
        name="Checkstyle — Pass Rate",
        dimension="governance",
        tier="established",
        direction_of_good="higher",
        value_kind="percent",
        page="governance",
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
