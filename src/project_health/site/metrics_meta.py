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
    # 'avg' (issue #54): a mean/average count that isn't a 0-1 ratio and
    # isn't an integer headcount either (e.g. mean reviewers per PR, 2.3) --
    # one decimal place, no unit suffix.
    "avg": ".1f",
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
    "avg": ".1f",
}

# A Vega-Lite axis `labelExpr` suffix appended after `_AXIS_FORMAT` renders
# each tick, for value_kinds whose formatted number alone is ambiguous
# without a unit (a bare "14.2" tick reads as nothing in particular, unlike
# "40%" or "0.350", which are already self-describing).
_AXIS_LABEL_SUFFIX = {
    "days": " d",
}


def format_days(value: float) -> str:
    """Format a `value_kind == "days"` duration for display, switching to a
    smaller unit rather than rounding a genuinely sub-day duration down to a
    misleading "0.0 days" (orchestrator review of issue #57: a 0.03-day
    median -- roughly 43 minutes -- rendered as "0.0 days", indistinguishable
    from a true zero-latency response).

    - `>= 1` day: `"X.X days"` (unchanged from before this fixup).
    - `>= 1` hour and `< 1` day: `"X.X h"`.
    - `< 1` hour: `"X min"` (whole minutes -- a sub-hour duration doesn't
      need decimal-minute precision to be legible).

    Shared by every card's big-number display and (`generate.py`'s
    `_vega_lite_spec`) the chart tooltip, so a "days" metric never shows two
    different numbers for the same point.
    """
    hours = value * 24
    if hours < 1.0:
        minutes = hours * 60
        return f"{minutes:.0f} min"
    if hours < 24.0:
        return f"{hours:.1f} h"
    return f"{value:.1f} days"


# Human-readable labels for a manifest `sources.<key>` entry (issue #86's
# per-card staleness badge, ARCHITECTURE.md §7.3: "JIRA data last refreshed
# ..."), keyed by the exact source key `pipeline.py` writes into the run
# manifest (`pipeline.py`'s `active_sources` handling) -- never a metric_id
# or a collector's own `source_id` attribute, which don't always match (e.g.
# `collectors/github.py`'s `source_id` is `"github_pr"`, but the manifest
# key it's written under is `"github"`).
SOURCE_LABELS: dict[str, str] = {
    "git": "Git",
    "jira": "JIRA",
    "github": "GitHub",
    "github_commit_authors": "GitHub commit authors",
    "github_profile": "GitHub profiles",
    "ponymail": "Pony Mail",
    "asf_roster": "ASF roster",
    "security": "Security",
    "release": "Release",
}

# The contributor leaderboard section (D19, issue #56) has no `metric_id` of
# its own -- it's a ranked-table section, not a `metric_value` series -- so
# its source dependency is declared here rather than as a `MetricMeta.sources`
# entry.
#
# Derived from `leaderboard.py`'s actual queries (fixup: orchestrator review
# of issue #86 -- the original mapping guessed 'github' for PRs, but
# `leaderboard.py` never reads `pr`/`pr_review` at all, verified: no match
# for `pr_review`/`pull_request` in that module):
# - `commits` -- `contribution_event` (git) by author.
# - `reviews` -- `review_event` filtered `WHERE re.source = 'commit_trailer'`
#   ONLY (`leaderboard.py::_reviews_list`, "the same primary source
#   `metrics.engine._reviewer_hhi` uses" per that function's own docstring)
#   -- git, not jira, not github.
# - `jira_issues_resolved` -- the `issue` table (jira) by assignee.
# - Every list's `organization` column comes from the same
#   `affiliation_period` table the org metrics use (built once in
#   `pipeline.py` via `build_affiliation_periods` and passed to both
#   `metrics.engine.compute_all` and `leaderboard.build_leaderboards`) --
#   so `github_commit_authors`/`github_profile` belong here for the same
#   reason they belong on `elephant_factor`/etc. above.
LEADERBOARD_SOURCES: tuple[str, ...] = ("git", "jira", "github_commit_authors", "github_profile")

# The Governance page's Security section (issue #55, D21 item 3:
# OpenSSF Scorecard + CVE/advisory history) reads its own raw tables
# directly (`site/generate.py::_read_security_context`,
# `storage.read_table(data_dir, "security", ...)`) rather than a
# `metric_value` series, so it gets the same kind of standalone constant as
# `LEADERBOARD_SOURCES` rather than a `MetricMeta.sources` entry.
SECURITY_SOURCES: tuple[str, ...] = ("security",)


@dataclass(frozen=True)
class PriorArt:
    """One prior-art reference for a metric's site "Based on" line (issue
    #134 reviewer feedback: "what were the metric types based off? there's a
    lot of prior art"). `label` is a short display string -- a CHAOSS
    Knowledge Base metric name (optionally "(adjacent)"/"(applied to ...)"
    when this project's metric isn't an exact match) or a paper/author
    citation; `url` is a live link, curl-verified to return HTTP 200 at the
    time it was added here. Every entry below is transcribed from this
    exact metric's own "CHAOSS equivalent" line (or literature citation) in
    `docs/spec/METRICS.md`'s per-metric section -- never paraphrased into a
    stronger match than that section itself claims, and never a fabricated
    citation where METRICS.md says "none exact"/"project-specific" (see
    `MetricMeta.prior_art`, which renders "project-specific" for an empty
    tuple instead of inventing one)."""

    label: str
    url: str


@dataclass(frozen=True)
class MetricMeta:
    metric_id: str
    name: str
    dimension: str
    # tier: 'established' | 'proxy' | 'experimental' | 'classified' (METRICS.md §0.1)
    tier: str
    # direction_of_good: 'higher' | 'lower' | 'target-range' | 'none' (METRICS.md §1),
    # or `None` (JSON null) for a metric this project deliberately never
    # judges good/bad -- distinct from the string `'none'` above, which
    # scoring/baseline.py still reads as "no directional judgment, but still
    # part of the scored/composite system" (SCORING.md §4.3). Issue #97 (D25
    # amendment): the governance fact-based trend metrics use `None` because
    # they are outside that system entirely -- descriptive, not scored.
    direction_of_good: str | None
    # value_kind: how the metric's numeric value is displayed —
    #   'count'   -> integer, no unit suffix, e.g. "12"
    #   'ratio'   -> 3 decimal places on a 0-1 scale, e.g. "0.350"
    #   'percent' -> a 0-1 fraction shown as a percentage, e.g. "12.3%"
    #   'days'    -> 1 decimal place with a " days" suffix, e.g. "14.2 days"
    value_kind: str
    # page: which site page (D13) renders this metric's card —
    # 'community' | 'conversations' | 'governance'.
    page: str
    # sources: which manifest `sources.<key>` entries this metric's value is
    # computed from (METRICS.md's own "Required data / source" prose per
    # metric, translated into the same keys `SOURCE_LABELS` above and the run
    # manifest use) -- issue #86's per-card staleness badge (ARCHITECTURE.md
    # §7.3) looks a metric's card up by this tuple to decide whether to show
    # "<source> data last refreshed <date>; collection failed on <date>".
    # `affiliations.yaml` (elephant_factor/organizational_hhi/single_org_share/
    # unknown_affiliation_rate) is a curated, PR-reviewed static file, not a
    # collected source with its own manifest status, so it's never listed here.
    sources: tuple[str, ...] = ()
    # prior_art (issue #134): zero or more `PriorArt` references for this
    # metric's card "Based on" line, transcribed from METRICS.md's own
    # per-metric "CHAOSS equivalent"/citation prose. Empty tuple (the
    # default) means METRICS.md itself says "none exact"/"none
    # directly"/"no single CHAOSS metric" for this metric -- rendered on
    # the site as "project-specific" rather than left blank or guessed at.
    prior_art: tuple[PriorArt, ...] = ()

    @property
    def prior_art_label(self) -> str:
        """"Based on" line display text: joined prior-art labels, or the
        literal "project-specific" when METRICS.md documents no established
        CHAOSS/literature equivalent for this metric (issue #134 -- "where a
        metric has no prior art, say 'project-specific' rather than
        inventing one")."""
        if not self.prior_art:
            return "project-specific"
        return ", ".join(pa.label for pa in self.prior_art)

    def format_value(self, value: float) -> str:
        """Format `value` for display (the card's big number)."""
        if self.value_kind == "count":
            return f"{value:,.0f}"
        if self.value_kind == "ratio":
            return f"{value:.3f}"
        if self.value_kind == "percent":
            return f"{value * 100:.1f}%"
        if self.value_kind == "days":
            return format_days(value)
        if self.value_kind == "avg":
            return f"{value:.1f}"
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
        if self.value_kind == "avg":
            return f"{self.name} (avg per PR)"
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
        sources=("git",),
        prior_art=(PriorArt("CHAOSS: Contributors", "https://chaoss.community/kb/metric-contributors/"),),
    ),
    "new_contributors_monthly": MetricMeta(
        metric_id="new_contributors_monthly",
        name="New Contributors",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="higher",
        value_kind="count",
        page="community",
        sources=("git",),
        prior_art=(
            PriorArt(
                "CHAOSS: New Contributors",
                "https://www.chaoss.community/kb/metric-new-contributors/",
            ),
        ),
    ),
    # (fixup: orchestrator review of issue #86) `pmc_joins_quarterly` is
    # registered in `metrics.registry.METRIC_IDS` and computed every run
    # (`metrics/engine.py::_pmc_joins_quarterly`, reading `roster_entry`
    # only -- no git/jira join at all) but had no `MetricMeta` entry here at
    # all until this fixup, so it never got a card on the site. Dimension/
    # tier/direction_of_good/value_kind/page per METRICS.md's own section.
    "pmc_joins_quarterly": MetricMeta(
        metric_id="pmc_joins_quarterly",
        name="PMC Joins per Quarter",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="higher",
        value_kind="count",
        page="community",
        sources=("asf_roster",),
        # METRICS.md: "none standardized; ASF-specific ground-truth measurement."
        prior_art=(),
    ),
    # `unique_reviewers_monthly`'s `value`/`n` (`union_count`,
    # `metrics/engine.py::_unique_reviewers_monthly`) is a DISTINCT count of
    # reviewers credited via EITHER `review_event.source = 'commit_trailer'`
    # (git) OR `'jira_field'` (jira) -- `review_event` itself is only ever
    # populated from those two sources (`collectors/git.py`'s trailer parse,
    # `collectors/jira.py`'s reviewer-field extraction); GitHub PR reviews
    # never feed this table, so 'github' does NOT belong here (fixup:
    # orchestrator review of issue #86 caught this -- the original mapping
    # here was derived from the metric's docs prose/name, not the actual
    # engine SQL and collectors, and wrongly included 'github').
    "unique_reviewers_monthly": MetricMeta(
        metric_id="unique_reviewers_monthly",
        name="Unique Reviewers",
        dimension="reviewer capacity",
        tier="proxy",
        direction_of_good="higher",
        value_kind="count",
        page="community",
        sources=("git", "jira"),
        prior_art=(
            PriorArt(
                "CHAOSS: Change Request Reviews (adjacent)",
                "https://chaoss.community/kb/metric-change-request-reviews/",
            ),
        ),
    ),
    # `reviewer_hhi`'s `value`/`n` (`hhi_commit_trailer`/`n_commit_trailer`,
    # `metrics/engine.py::_reviewer_hhi`) is computed from `commit_trailer`
    # (git) review credits ONLY -- deliberately, per that function's own
    # docstring (fixup cycle 1, review comment on issue #7): crediting both
    # `commit_trailer` and `jira_field` double-counts a single review
    # across two different "people" under M0's naive identity resolution,
    # deflating HHI. `jira_field_hhi`/`union_hhi` are computed too, but only
    # ever land in `details_json` as a cross-check -- never in the `value`
    # this card's number/chart render from (`site/generate.py::_card_context`
    # reads `point.value`, not `details_json`). So this card's own staleness
    # genuinely never depends on 'jira' (or 'github', which review_event
    # never gets rows from at all -- see `unique_reviewers_monthly` above).
    "reviewer_hhi": MetricMeta(
        metric_id="reviewer_hhi",
        name="Reviewer Concentration (HHI)",
        dimension="reviewer capacity",
        tier="proxy",
        direction_of_good="lower",
        value_kind="ratio",
        page="community",
        sources=("git",),
        # METRICS.md: "CHAOSS equivalent: none exact." -- no link to cite.
        prior_art=(),
    ),
    "median_resolution_latency_jira": MetricMeta(
        metric_id="median_resolution_latency_jira",
        name="Median JIRA Resolution Latency",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="days",
        page="community",
        sources=("jira",),
        prior_art=(
            PriorArt(
                "CHAOSS: Issue Resolution Duration",
                "https://chaoss.community/kb/metric-issue-resolution-duration/",
            ),
            PriorArt(
                "CHAOSS: Change Request Closure Ratio (adjacent)",
                "https://chaoss.community/kb/metric-change-request-closure-ratio/",
            ),
        ),
    ),
    # issue #134: cohort companion series (CHAOSS "Issue Resolution
    # Duration" style), restricted to issues created within the trailing 12
    # months of their resolution month -- see metrics/engine.py's
    # `_median_resolution_latency_jira_cohort_12m`. Rendered on the same
    # chart/page as the plain metric above (same dimension/page), not a
    # separate card elsewhere.
    "median_resolution_latency_jira_cohort_12m": MetricMeta(
        metric_id="median_resolution_latency_jira_cohort_12m",
        name="Median JIRA Resolution Latency (12m cohort)",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="days",
        page="community",
        sources=("jira",),
        prior_art=(
            PriorArt(
                "CHAOSS: Issue Resolution Duration",
                "https://chaoss.community/kb/metric-issue-resolution-duration/",
            ),
        ),
    ),
    "stale_jira_rate": MetricMeta(
        metric_id="stale_jira_rate",
        name="Stale JIRA Issue Rate",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="percent",
        page="community",
        sources=("jira",),
        prior_art=(
            PriorArt(
                "CHAOSS: Change Requests (adjacent)",
                "https://chaoss.community/kb/metric-change-requests/",
            ),
            PriorArt(
                "CHAOSS: Change Requests Declined (adjacent)",
                "https://chaoss.community/kb/metric-change-requests-declined/",
            ),
        ),
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
        sources=("git",),
        prior_art=(
            PriorArt(
                "CHAOSS: Contributor Absence Factor / Bus Factor (adjacent)",
                "https://chaoss.community/kb/metric-bus-factor/",
            ),
            PriorArt(
                "Avelino, Passos, Hora & Valente (2016), A Novel Approach for Estimating "
                "Truck Factors",
                "https://arxiv.org/abs/1604.06766",
            ),
        ),
    ),
    "contributor_absence_factor": MetricMeta(
        metric_id="contributor_absence_factor",
        name="Contributor Dependency (N for 50%)",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="higher",
        value_kind="count",
        page="community",
        sources=("git",),
        prior_art=(
            PriorArt(
                "CHAOSS: Contributor Absence Factor",
                "https://chaoss.community/kb/metric-bus-factor/",
            ),
        ),
    ),
    "contributor_hhi": MetricMeta(
        metric_id="contributor_hhi",
        name="Contributor Concentration (HHI)",
        dimension="contributor sustainability",
        tier="established",
        direction_of_good="lower",
        value_kind="ratio",
        page="community",
        sources=("git",),
        prior_art=(
            PriorArt(
                "DOJ/FTC: Herfindahl-Hirschman Index",
                "https://www.justice.gov/atr/herfindahl-hirschman-index",
            ),
        ),
    ),
    # issue #52 (D6 organizational-diversity metrics, METRICS.md §5).
    #
    # Every one of these four joins `contribution_event` (git) against
    # `affiliation_period` (`metrics/engine.py`'s `_organization_commit_
    # counts`-style queries, e.g. `_elephant_factor`/`_organizational_hhi`/
    # `_single_org_share`/`_unknown_affiliation_rate`). `affiliation_period`
    # itself (`normalize/affiliation.py::build_affiliation_periods`) is
    # built from three priority sources: `curated`/`email_domain` are
    # static, PR-reviewed YAML files (affiliations.yaml, org_domains.yaml --
    # not a *collected* source with its own manifest status), and
    # `github_company` -- which reads the accumulated `github_profile` raw
    # table (`collectors/github_profile.py`), keyed by logins the
    # accumulated `github_commit_authors` association table
    # (`collectors/github_commit_authors.py`) links to a commit's raw email.
    # No `roster_entry` (`asf_roster`) join anywhere in this affiliation
    # path (verified: `grep roster normalize/affiliation.py` -- no hits).
    # (fixup: orchestrator review of issue #86 -- the original mapping here
    # was ('git',) only, missing the github_profile/github_commit_authors
    # dependency entirely.)
    "elephant_factor": MetricMeta(
        metric_id="elephant_factor",
        name="Elephant Factor",
        dimension="organizational diversity",
        tier="experimental",
        direction_of_good="higher",
        value_kind="count",
        page="community",
        sources=("git", "github_commit_authors", "github_profile"),
        prior_art=(
            PriorArt(
                "CHAOSS: Elephant Factor",
                "https://www.chaoss.community/kb/metric-elephant-factor/",
            ),
            PriorArt(
                "CHAOSS wg-risk: Elephant Factor",
                "https://github.com/chaoss/wg-risk/blob/main/focus-areas/business-risk/"
                "elephant-factor.md",
            ),
        ),
    ),
    "organizational_hhi": MetricMeta(
        metric_id="organizational_hhi",
        name="Organizational Concentration (HHI)",
        dimension="organizational diversity",
        tier="established",
        direction_of_good="lower",
        value_kind="ratio",
        page="community",
        sources=("git", "github_commit_authors", "github_profile"),
        prior_art=(
            PriorArt(
                "CHAOSS: Organizational Diversity",
                "https://www.chaoss.community/kb/metric-organizational-diversity/",
            ),
        ),
    ),
    "single_org_share": MetricMeta(
        metric_id="single_org_share",
        name="Single-Organization Share",
        dimension="organizational diversity",
        tier="established",
        direction_of_good="lower",
        value_kind="percent",
        page="community",
        sources=("git", "github_commit_authors", "github_profile"),
        prior_art=(
            PriorArt(
                "CHAOSS: Organizational Diversity",
                "https://www.chaoss.community/kb/metric-organizational-diversity/",
            ),
        ),
    ),
    "unknown_affiliation_rate": MetricMeta(
        metric_id="unknown_affiliation_rate",
        name="Unknown-Affiliation Rate",
        dimension="organizational diversity",
        tier="established",
        direction_of_good="none",
        value_kind="percent",
        page="community",
        sources=("git", "github_commit_authors", "github_profile"),
        # METRICS.md: "none directly; a transparency companion metric this
        # project adds on top of the CHAOSS organizational metrics."
        prior_art=(),
    ),
    # issue #35: dev@ mailing-list responsiveness (D16), metadata only.
    "time_to_first_reply_devlist": MetricMeta(
        metric_id="time_to_first_reply_devlist",
        name="Time to First Reply — dev@",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="days",
        page="conversations",
        sources=("ponymail",),
        prior_art=(
            PriorArt(
                "CHAOSS: Time to First Response (applied to email)",
                "https://www.chaoss.community/kb/metric-time-to-first-response/",
            ),
        ),
    ),
    "unanswered_thread_rate_devlist": MetricMeta(
        metric_id="unanswered_thread_rate_devlist",
        name="Unanswered Thread Rate — dev@",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="percent",
        page="conversations",
        sources=("ponymail",),
        # METRICS.md: "adjacent to CHAOSS responsiveness metrics; no exact
        # named equivalent for 'unanswered thread rate' specifically."
        prior_art=(),
    ),
    # issue #54: GitHub-PR development metrics + JIRA responsiveness
    "pr_merge_lead_time": MetricMeta(
        metric_id="pr_merge_lead_time",
        name="PR Merge Lead Time",
        dimension="responsiveness",
        tier="proxy",
        direction_of_good="lower",
        value_kind="days",
        page="community",
        sources=("github",),
        prior_art=(
            PriorArt(
                "CHAOSS: Change Request Closure Ratio (adjacent)",
                "https://chaoss.community/kb/metric-change-request-closure-ratio/",
            ),
        ),
    ),
    "pr_time_to_first_review": MetricMeta(
        metric_id="pr_time_to_first_review",
        name="PR Time to First Review",
        dimension="reviewer capacity",
        tier="proxy",
        direction_of_good="lower",
        value_kind="days",
        page="community",
        sources=("github",),
        prior_art=(
            PriorArt(
                "CHAOSS: Time to First Response (GitHub-only variant)",
                "https://www.chaoss.community/kb/metric-time-to-first-response/",
            ),
        ),
    ),
    "pr_time_to_close": MetricMeta(
        metric_id="pr_time_to_close",
        name="PR Time to Close",
        dimension="responsiveness",
        tier="proxy",
        direction_of_good="lower",
        value_kind="days",
        page="community",
        sources=("github",),
        prior_art=(
            PriorArt(
                "CHAOSS: Change Requests (adjacent)",
                "https://chaoss.community/kb/metric-change-requests/",
            ),
        ),
    ),
    "pr_review_engagement": MetricMeta(
        metric_id="pr_review_engagement",
        name="PR Review Engagement (Reviewers/PR)",
        dimension="reviewer capacity",
        tier="proxy",
        direction_of_good="none",
        value_kind="avg",
        page="community",
        sources=("github",),
        prior_art=(
            PriorArt(
                "CHAOSS: Change Request Reviews (adjacent)",
                "https://chaoss.community/kb/metric-change-request-reviews/",
            ),
        ),
    ),
    "time_to_first_response_jira": MetricMeta(
        metric_id="time_to_first_response_jira",
        name="Time to First Response (JIRA)",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="days",
        page="community",
        sources=("jira",),
        prior_art=(
            PriorArt(
                "CHAOSS: Time to First Response",
                "https://www.chaoss.community/kb/metric-time-to-first-response/",
            ),
            PriorArt(
                "CHAOSS: Responsiveness practitioner guide",
                "https://www.chaoss.community/practitioner-guide-responsiveness/",
            ),
        ),
    ),
    "stale_pr_rate": MetricMeta(
        metric_id="stale_pr_rate",
        name="Stale PR Rate",
        dimension="responsiveness",
        tier="established",
        direction_of_good="lower",
        value_kind="percent",
        page="community",
        sources=("github",),
        prior_art=(
            PriorArt(
                "CHAOSS: Change Requests (adjacent)",
                "https://chaoss.community/kb/metric-change-requests/",
            ),
            PriorArt(
                "CHAOSS: Change Requests Declined (adjacent)",
                "https://chaoss.community/kb/metric-change-requests-declined/",
            ),
        ),
    ),
    # issue #136, DECISIONS.md D29: CHAOSS "Change Request Closure Ratio"
    # (closed/opened in the period), reported as two labelled series since
    # Cassandra's reviewed changes are committed via JIRA, not GitHub PRs
    # alone -- see `metrics/dev_metrics.py`'s own module docstring.
    "change_request_closure_ratio_pr": MetricMeta(
        metric_id="change_request_closure_ratio_pr",
        name="Change Request Closure Ratio — GitHub PR",
        dimension="responsiveness",
        tier="proxy",
        direction_of_good="higher",
        value_kind="ratio",
        page="community",
        sources=("github",),
        prior_art=(
            PriorArt(
                "CHAOSS: Change Request Closure Ratio",
                "https://chaoss.community/kb/metric-change-request-closure-ratio/",
            ),
        ),
    ),
    "change_request_closure_ratio_jira_patch": MetricMeta(
        metric_id="change_request_closure_ratio_jira_patch",
        name="Change Request Closure Ratio — JIRA Patch Submissions",
        dimension="responsiveness",
        tier="established",
        direction_of_good="higher",
        value_kind="ratio",
        page="community",
        sources=("jira",),
        prior_art=(
            PriorArt(
                "CHAOSS: Change Request Closure Ratio",
                "https://chaoss.community/kb/metric-change-request-closure-ratio/",
            ),
        ),
    ),
    # issue #135: release cadence metrics (METRICS.md §6), published as plain
    # CHAOSS-mapped metrics per DECISIONS.md D29 (no composite/dimension
    # score). All four read the `release` raw table only (collectors/
    # release.py) -- the archive.apache.org cross-check is a per-row field
    # on that same table, not a separate manifest source.
    "release_frequency": MetricMeta(
        metric_id="release_frequency",
        name="Release Frequency",
        dimension="release cadence",
        tier="established",
        direction_of_good="target-range",
        value_kind="count",
        page="community",
        sources=("release",),
        # Verified live 2026-10-09: chaoss.community/kb/metric-release-frequency/
        # (redirects to www.chaoss.community) returns HTTP 200, titled "Metric:
        # Release Frequency" -- an exact-name CHAOSS Knowledge Base match
        # (corrects METRICS.md §6's earlier "none named identically," written
        # before this metric had a real collector to check against).
        prior_art=(
            PriorArt(
                "CHAOSS: Release Frequency",
                "https://chaoss.community/kb/metric-release-frequency/",
            ),
        ),
    ),
    "release_regularity": MetricMeta(
        metric_id="release_regularity",
        name="Release Interval Regularity (CoV)",
        dimension="release cadence",
        tier="proxy",
        direction_of_good="none",
        value_kind="ratio",
        page="community",
        sources=("release",),
    ),
    "time_since_last_release": MetricMeta(
        metric_id="time_since_last_release",
        name="Time Since Last Release",
        dimension="release cadence",
        tier="established",
        direction_of_good="none",
        value_kind="days",
        page="community",
        sources=("release",),
    ),
    # issue #135, D29 follow-up: the "companion days_between_releases
    # (median gap, trailing 12 months)" the issue itself named, promoted to
    # its own metric_id (previously only release_regularity's details_json)
    # now that publication isn't gated by a dimension's 1-3 key-metric cap.
    "days_between_releases": MetricMeta(
        metric_id="days_between_releases",
        name="Days Between Releases (median)",
        dimension="release cadence",
        tier="proxy",
        direction_of_good="none",
        value_kind="days",
        page="community",
        sources=("release",),
        # Same CHAOSS KB page as release_frequency (verified live 2026-10-09)
        # -- the closest published prior art for release-timing cadence;
        # CHAOSS's own metric measures count-over-time, not gap length
        # directly, so this is cited as the nearest match, not an exact one.
        prior_art=(
            PriorArt(
                "CHAOSS: Release Frequency",
                "https://chaoss.community/kb/metric-release-frequency/",
            ),
        ),
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
#
# `sources` below (fixup: orchestrator review of issue #86) is derived from
# `pipeline.py::_collect_governance` and `governance/checks.py`'s scoring
# functions, not from the check's name/docs prose:
#
# - EVERY check scores `commit_record` rows from governance's own,
#   separately-watermarked local git walk (`_collect_governance_commit_
#   records`), which reads the SAME `workdir` clone the real `git` source
#   populates via `clone_or_fetch` (`_collect_git`, called earlier in
#   `run_pipeline`) — governance never re-clones. So a `git` collection
#   failure means governance scores a stale local clone too: 'git' belongs
#   on all four.
# - `reviewer-present` ALSO checks `jira_reviewers` — read straight from the
#   real, accumulated `jira` source's `review_event` table
#   (`pipeline._governance_reviewers_by_issue`:
#   `storage.read_table(data_dir, "jira", "review_event")`), so 'jira'
#   genuinely belongs there too.
# - `jira-ticket-referenced` only regexes `commit.message`/`issue_keys` —
#   no live JIRA lookup at all (`governance/checks.py::
#   score_jira_ticket_referenced`) — so 'jira' does NOT belong there,
#   despite the name.
# - `pre-commit-ci-evidence`'s CI evidence comes from `JiraCommentsCollector`
#   (`collectors/jira_comments.py`), a distinct collector from the `jira`
#   search collector, whose results only ever land in governance's own
#   `raw/governance/ci_evidence` table
#   (`pipeline._governance_ci_evidence_found_map`) — never in
#   `manifest.sources`. There is no manifest source key this evidence's own
#   staleness can be attributed to today (verified: `manifest["sources"]`
#   has no `jira_comments`/`governance` entry — `governance` is a distinct,
#   differently-shaped top-level manifest field, `site.manifest.
#   GovernanceStatus`, with no `last_good_snapshot`). Documented here rather
#   than guessed at, per this project's "collect imperfectly but honestly,
#   not silently" rule — a future issue adding a real per-source status for
#   that collector should add it here too.
# - `code-style-checkstyle`'s evidence likewise comes from
#   `GitHubChecksCollector` (`collectors/github_checks.py`), a distinct
#   collector from the PR collector `sources.github` reports on, whose
#   results only ever land in governance's own `raw/governance/check_run`
#   table (`pipeline._governance_check_run_evidence_for_scoring`) — never in
#   `sources.github`. Same documented gap as CI evidence above; 'github'
#   does NOT belong here despite the check's evidence literally coming from
#   GitHub, because `sources.github`'s status doesn't track it.
# Issue #97 (D25 amendment, orchestrator review): the pass-rate metrics
# these five entries used to register (`governance_*_pass_rate`) are
# policy-derived -- `exempt` commits excluded from the denominator, months
# before a rule's `effective_from` blanked `not_in_force` -- which reads as
# a verdict rate even with a neutral-sounding name. They are replaced here
# with `governance/fact_metrics.py`'s metric_ids: a monthly share computed
# directly from raw evidence, with no policy gating at all, so a pre-2020
# month still carries a real value. `direction_of_good=None` (not the
# string `"none"` some M0 metrics use -- see `MetricMeta.direction_of_good`)
# because these sit entirely outside the scored/composite system
# (scoring/baseline.py never reads them; governance isn't in `METRIC_IDS`).
# The old pass-rate metric_ids are no longer registered here at all, so
# `generate.py`'s per-metric-id download loop never writes their
# `data/*.json`/`.csv` files -- `governance/metrics.py` still computes and
# `pipeline.py` still persists them, for the scoring engine's own internal
# record only.
GOVERNANCE_METRICS: dict[str, MetricMeta] = {
    "governance_commits_with_named_reviewer_share": MetricMeta(
        metric_id="governance_commits_with_named_reviewer_share",
        name="Commits with a named reviewer",
        dimension="governance",
        tier="established",
        direction_of_good=None,
        value_kind="percent",
        page="governance",
        sources=("git", "jira"),
    ),
    "governance_commits_with_ticket_share": MetricMeta(
        metric_id="governance_commits_with_ticket_share",
        name="Commits referencing a ticket",
        dimension="governance",
        tier="established",
        direction_of_good=None,
        value_kind="percent",
        page="governance",
        sources=("git",),
    ),
    "governance_commits_with_ci_evidence_before_commit_share": MetricMeta(
        metric_id="governance_commits_with_ci_evidence_before_commit_share",
        name="Commits with CI evidence on JIRA before commit",
        dimension="governance",
        tier="proxy",
        direction_of_good=None,
        value_kind="percent",
        page="governance",
        sources=("git",),
    ),
    "governance_commits_with_both_ci_artefacts_share": MetricMeta(
        metric_id="governance_commits_with_both_ci_artefacts_share",
        name="Commits with both CI artefacts attached",
        dimension="governance",
        tier="proxy",
        direction_of_good=None,
        value_kind="percent",
        page="governance",
        sources=("git",),
    ),
    "governance_commits_with_checkstyle_success_share": MetricMeta(
        metric_id="governance_commits_with_checkstyle_success_share",
        name="Commits with a successful checkstyle run",
        dimension="governance",
        tier="established",
        direction_of_good=None,
        value_kind="percent",
        page="governance",
        sources=("git",),
    ),
}

# Open PR backlog composition (issue #142): deliberately outside M0_METRICS'
# scored/composite system (`metrics/pr_backlog.py`'s module docstring) --
# same `direction_of_good=None` treatment GOVERNANCE_METRICS gets, since
# these are backlog-composition facts, not a rate this project judges
# "higher/lower is better". `page="community"`, `dimension="responsiveness"`
# so these cards render in the Community page's existing Responsiveness
# group, alongside stale_pr_rate/time_to_first_response_jira/etc.
#
# Prior art: CHAOSS's Knowledge Base has no "Change Request Backlog" metric
# (`kb/metric-change-request-backlog` -- verified 404, 2026-10-09) -- cited
# here as *adapted from* two CHAOSS metrics instead (Change Requests, and
# Issue Age applied to change requests rather than issues), per the issue's
# own verified citation; never claimed as an exact CHAOSS match.
PR_BACKLOG_PRIOR_ART: tuple[PriorArt, ...] = (
    PriorArt(
        "CHAOSS: Change Requests (adapted)",
        "https://chaoss.community/kb/metric-change-requests/",
    ),
    PriorArt(
        "CHAOSS: Issue Age (adapted, applied to change requests)",
        "https://chaoss.community/kb/metric-issue-age/",
    ),
)

PR_BACKLOG_METRICS: dict[str, MetricMeta] = {
    "open_pr_backlog_total": MetricMeta(
        metric_id="open_pr_backlog_total",
        name="Open PR backlog",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_drafts": MetricMeta(
        metric_id="open_pr_backlog_drafts",
        name="Open PR backlog — drafts",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_age_lt_30d": MetricMeta(
        metric_id="open_pr_backlog_age_lt_30d",
        name="Open PR backlog — age under 30d",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_age_30_90d": MetricMeta(
        metric_id="open_pr_backlog_age_30_90d",
        name="Open PR backlog — age 30-90d",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_age_90d_1y": MetricMeta(
        metric_id="open_pr_backlog_age_90d_1y",
        name="Open PR backlog — age 90d-1y",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_age_1_3y": MetricMeta(
        metric_id="open_pr_backlog_age_1_3y",
        name="Open PR backlog — age 1-3y",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_age_gt_3y": MetricMeta(
        metric_id="open_pr_backlog_age_gt_3y",
        name="Open PR backlog — age over 3y",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_ticket_open": MetricMeta(
        metric_id="open_pr_backlog_ticket_open",
        name="Open PR backlog — linked ticket still open",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github", "jira"),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_ticket_fixed": MetricMeta(
        metric_id="open_pr_backlog_ticket_fixed",
        name="Open PR backlog — linked ticket Fixed",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github", "jira"),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_ticket_closed_other": MetricMeta(
        metric_id="open_pr_backlog_ticket_closed_other",
        name="Open PR backlog — linked ticket closed (other)",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github", "jira"),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_no_ticket_key": MetricMeta(
        metric_id="open_pr_backlog_no_ticket_key",
        name="Open PR backlog — no ticket key in title",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="count",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
    ),
    "open_pr_backlog_no_github_response_share": MetricMeta(
        metric_id="open_pr_backlog_no_github_response_share",
        name="Open PR backlog — no GitHub response",
        dimension="responsiveness",
        tier="established",
        direction_of_good=None,
        value_kind="percent",
        page="community",
        sources=("github",),
        prior_art=PR_BACKLOG_PRIOR_ART,
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
        summary="Per-commit facts from the public record — reviewers, CI evidence, checkstyle.",
        empty_message="No commit history collected yet.",
    ),
}

# Home-page summary cards show at most this many headline metrics per page
# before linking through for the rest (D13: "one summary card per page:
# its headline metrics ... and a link").
HOME_CARD_METRIC_LIMIT = 3


@dataclass(frozen=True)
class SectionMeta:
    """Presentation metadata for one collapsible `<details>` section on a
    site page (issue #144: "Layout: collapsible sections with summary rows,
    grouped by CHAOSS practitioner-guide topics"). `section_id` is both the
    `<details id>` (so `/community/#responsiveness` opens it directly,
    `static/app.js`) and the key `generate.py` groups a page's metrics by.
    `chaoss_url` links to the CHAOSS practitioner-guide page this section's
    topic comes from -- `None` for a section (Governance's) that isn't a
    CHAOSS-mapped topic. `headline_metric_ids` are the 1-4 metric_ids shown
    (value, month, n, sparkline) in the section's collapsed summary row --
    always a subset of whatever this section actually contains, so the
    summary row's numbers are never anything other than the same already-
    computed card values the expanded section also shows."""

    section_id: str
    title: str
    chaoss_url: str | None
    headline_metric_ids: tuple[str, ...] = ()
    # Link text for `chaoss_url`; most sections link a practitioner guide,
    # Releases links a metric page (no release practitioner guide exists).
    chaoss_label: str = "CHAOSS practitioner guide"


# Community page sections (issue #144's owner-approved grouping, CHAOSS
# practitioner-guide topics; every URL below curl-verified 200 on
# 2026-10-09). Order is the page's own section order, top to bottom.
COMMUNITY_SECTIONS: tuple[SectionMeta, ...] = (
    SectionMeta(
        section_id="responsiveness",
        title="Responsiveness",
        chaoss_url="https://chaoss.community/practitioner-guide-responsiveness/",
        headline_metric_ids=(
            "time_to_first_response_jira",
            "stale_pr_rate",
            "open_pr_backlog_total",
            "median_resolution_latency_jira",
        ),
    ),
    SectionMeta(
        section_id="contributor-sustainability",
        title="Contributor sustainability",
        chaoss_url="https://chaoss.community/practitioner-guide-contributor-sustainability/",
        headline_metric_ids=(
            "active_contributors_monthly",
            "new_contributors_monthly",
            "contributor_absence_factor",
            "unique_reviewers_monthly",
        ),
    ),
    SectionMeta(
        section_id="organizational-participation",
        title="Organizational participation",
        chaoss_url="https://chaoss.community/practitioner-guide-organizational-participation/",
        headline_metric_ids=(
            "elephant_factor",
            "organizational_hhi",
            "single_org_share",
            "unknown_affiliation_rate",
        ),
    ),
    SectionMeta(
        section_id="leadership",
        title="Leadership",
        chaoss_url="https://chaoss.community/practitioner-guide-diverse-leadership/",
        headline_metric_ids=("pmc_joins_quarterly",),
    ),
    SectionMeta(
        section_id="releases",
        title="Releases",
        # The issue's own citation note: "cite
        # https://chaoss.community/kb/metric-release-frequency/ (the
        # 'assessing viability' guide URL 404s; do not link it)".
        chaoss_url="https://chaoss.community/kb/metric-release-frequency/",
        chaoss_label="CHAOSS: Release Frequency",
        headline_metric_ids=(
            "release_frequency",
            "days_between_releases",
            "time_since_last_release",
        ),
    ),
)

# Every `M0_METRICS`/`PR_BACKLOG_METRICS` metric_id with `page == "community"`
# maps to exactly one of `COMMUNITY_SECTIONS` above -- enforced by
# `_validate_community_sections` below (import-time fail-fast, like
# `_prior_art_url`'s validation above) and by
# `tests/test_site_sections.py`'s own membership test ("no orphan, no
# duplicate", issue #144's acceptance criterion). `truck_factor` and
# `pr_review_engagement` aren't named in the issue's own per-section bullet
# list, but both still need a home: `truck_factor` (bus-factor-adjacent) and
# `pr_review_engagement` (reviewers-per-PR) are grouped with their nearest
# named sibling metric's section rather than left without one.
COMMUNITY_METRIC_SECTION: dict[str, str] = {
    # Responsiveness.
    "median_resolution_latency_jira": "responsiveness",
    "median_resolution_latency_jira_cohort_12m": "responsiveness",
    "stale_jira_rate": "responsiveness",
    "pr_merge_lead_time": "responsiveness",
    "pr_time_to_first_review": "responsiveness",
    "pr_time_to_close": "responsiveness",
    "time_to_first_response_jira": "responsiveness",
    "stale_pr_rate": "responsiveness",
    "change_request_closure_ratio_pr": "responsiveness",
    "change_request_closure_ratio_jira_patch": "responsiveness",
    "open_pr_backlog_total": "responsiveness",
    "open_pr_backlog_drafts": "responsiveness",
    "open_pr_backlog_age_lt_30d": "responsiveness",
    "open_pr_backlog_age_30_90d": "responsiveness",
    "open_pr_backlog_age_90d_1y": "responsiveness",
    "open_pr_backlog_age_1_3y": "responsiveness",
    "open_pr_backlog_age_gt_3y": "responsiveness",
    "open_pr_backlog_ticket_open": "responsiveness",
    "open_pr_backlog_ticket_fixed": "responsiveness",
    "open_pr_backlog_ticket_closed_other": "responsiveness",
    "open_pr_backlog_no_ticket_key": "responsiveness",
    "open_pr_backlog_no_github_response_share": "responsiveness",
    # Contributor sustainability.
    "active_contributors_monthly": "contributor-sustainability",
    "new_contributors_monthly": "contributor-sustainability",
    "unique_reviewers_monthly": "contributor-sustainability",
    "reviewer_hhi": "contributor-sustainability",
    "truck_factor": "contributor-sustainability",
    "contributor_absence_factor": "contributor-sustainability",
    "contributor_hhi": "contributor-sustainability",
    "pr_review_engagement": "contributor-sustainability",
    # Organizational participation.
    "elephant_factor": "organizational-participation",
    "organizational_hhi": "organizational-participation",
    "single_org_share": "organizational-participation",
    "unknown_affiliation_rate": "organizational-participation",
    # Leadership.
    "pmc_joins_quarterly": "leadership",
    # Releases.
    "release_frequency": "releases",
    "release_regularity": "releases",
    "time_since_last_release": "releases",
    "days_between_releases": "releases",
}


def _validate_community_sections() -> None:
    """Fail loudly at import time (not silently at render time) if a
    community-page metric is missing from `COMMUNITY_METRIC_SECTION`, if the
    map has a stray entry for a metric that no longer exists or isn't on the
    community page, or if a `SectionMeta.headline_metric_ids` entry isn't
    actually assigned to that same section -- the same "collect imperfectly
    but honestly, not silently" standard `_group_by_page` enforces for
    `MetricMeta.page` itself."""
    community_metric_ids = {
        meta.metric_id
        for meta in (*M0_METRICS.values(), *PR_BACKLOG_METRICS.values())
        if meta.page == "community"
    }
    mapped_ids = set(COMMUNITY_METRIC_SECTION)
    missing = community_metric_ids - mapped_ids
    if missing:
        raise ValueError(
            f"community-page metric(s) {sorted(missing)} have no "
            "COMMUNITY_METRIC_SECTION entry"
        )
    stray = mapped_ids - community_metric_ids
    if stray:
        raise ValueError(
            f"COMMUNITY_METRIC_SECTION has entry/entries for non-community "
            f"metric(s) {sorted(stray)}"
        )
    section_ids = {meta.section_id for meta in COMMUNITY_SECTIONS}
    bad_section_values = set(COMMUNITY_METRIC_SECTION.values()) - section_ids
    if bad_section_values:
        raise ValueError(
            f"COMMUNITY_METRIC_SECTION names unknown section id(s) {sorted(bad_section_values)}"
        )
    for meta in COMMUNITY_SECTIONS:
        for metric_id in meta.headline_metric_ids:
            if COMMUNITY_METRIC_SECTION.get(metric_id) != meta.section_id:
                raise ValueError(
                    f"section {meta.section_id!r} lists headline metric "
                    f"{metric_id!r}, which isn't assigned to it in "
                    "COMMUNITY_METRIC_SECTION"
                )


_validate_community_sections()

# Governance page sections (issue #144): "Facts summary (collapsed with
# headline shares) · Commit history table (expanded by default — it's the
# page's main content) · References." Not CHAOSS-practitioner-guide-mapped
# topics like Community's (`chaoss_url=None`), so these are static section
# metadata only -- no metric-membership map/validation like Community's,
# since every `GOVERNANCE_METRICS` entry already renders in the one "Facts
# summary" section (there's nowhere else on this page for one to land).
GOVERNANCE_SECTIONS: tuple[SectionMeta, ...] = (
    SectionMeta(
        section_id="facts-summary",
        title="Facts summary",
        chaoss_url=None,
        headline_metric_ids=(
            "governance_commits_with_named_reviewer_share",
            "governance_commits_with_ticket_share",
            "governance_commits_with_ci_evidence_before_commit_share",
            "governance_commits_with_checkstyle_success_share",
        ),
    ),
    SectionMeta(section_id="commit-history", title="Commit history", chaoss_url=None),
    SectionMeta(section_id="references", title="References", chaoss_url=None),
)


@dataclass(frozen=True)
class ChaossStarterSeries:
    """One labelled series within a CHAOSS Starter Project Health card
    (issue #136, DECISIONS.md D29) -- a single `MetricMeta.metric_id`
    already registered in `M0_METRICS` above, shown with its own label
    (e.g. "GitHub PR" vs. "JIRA") so a multi-venue CHAOSS concept is never
    collapsed into one number."""

    metric_id: str
    label: str


@dataclass(frozen=True)
class ChaossStarterMetric:
    """Presentation metadata for one of the four CHAOSS "Starter Project
    Health" metrics-model metrics (issue #136, DECISIONS.md D29, reversing
    D20's composite score): the landing page leads with this published,
    external standard instead of an in-house composite. `chaoss_url` is
    sourced from the representative series metric's own `MetricMeta.
    prior_art` (issue #134) via `_prior_art_url` below for all four cards
    (issue #135 registered `release_frequency`'s own `PriorArt` entry,
    retiring the one literal-URL exception this docstring used to carry) --
    never a second, independently-maintained copy of the same CHAOSS KB
    link. Every link here and `chaoss_model_url` were verified live
    (redirect-then-200, orchestrator, 2026-10-09). `mapping_note` states
    which of this project's own metrics compute each CHAOSS concept for
    Cassandra -- a plain fact, never intent/recommendation language (D29
    item 4)."""

    key: str
    chaoss_name: str
    chaoss_url: str
    mapping_note: str
    series: tuple[ChaossStarterSeries, ...]
    # True for a CHAOSS metric this project hasn't implemented yet (Release
    # Frequency, pending #135) -- the card shows `mapping_note` only, no
    # series/chart/value.
    pending: bool = False


CHAOSS_STARTER_MODEL_URL = "https://chaoss.community/kb/metrics-model-starter-project-health/"


def _prior_art_url(metric_id: str) -> str:
    """The first `PriorArt` citation's URL already registered on
    `M0_METRICS[metric_id]` (issue #134) -- reused here (issue #136,
    orchestrator review) so a CHAOSS Starter Project Health landing card's
    link is never a second, independently-drifting copy of the exact same
    CHAOSS KB URL that metric's own community/conversations card already
    cites under "Based on". Every metric named below carries a real,
    non-"(adjacent)"/non-"(applied to ...)" prior-art entry for this exact
    CHAOSS concept (verified by reading `M0_METRICS` directly, not assumed)."""
    prior_art = M0_METRICS[metric_id].prior_art
    if not prior_art:
        raise ValueError(f"{metric_id!r} has no prior_art to source a CHAOSS link from")
    return prior_art[0].url


# Order matches the CHAOSS Starter Project Health model's own listing
# (verified live, 2026-10-09).
CHAOSS_STARTER_METRICS: tuple[ChaossStarterMetric, ...] = (
    ChaossStarterMetric(
        key="time_to_first_response",
        chaoss_name="Time to First Response",
        chaoss_url=_prior_art_url("time_to_first_response_jira"),
        mapping_note=(
            "Computed for Cassandra from three venues, shown separately: GitHub PR time to "
            "first review, JIRA time to first response, and dev@ time to first reply."
        ),
        series=(
            ChaossStarterSeries(metric_id="pr_time_to_first_review", label="GitHub PR"),
            ChaossStarterSeries(metric_id="time_to_first_response_jira", label="JIRA"),
            ChaossStarterSeries(metric_id="time_to_first_reply_devlist", label="dev@"),
        ),
    ),
    ChaossStarterMetric(
        key="change_request_closure_ratio",
        chaoss_name="Change Request Closure Ratio",
        chaoss_url=_prior_art_url("change_request_closure_ratio_pr"),
        mapping_note=(
            "Computed for Cassandra from two venues, shown separately, since reviewed changes "
            "are committed via JIRA as well as GitHub: GitHub pull requests, and JIRA issues "
            "entering Patch Available (Cassandra's patch-submission equivalent of a change "
            "request)."
        ),
        series=(
            ChaossStarterSeries(metric_id="change_request_closure_ratio_pr", label="GitHub PR"),
            ChaossStarterSeries(
                metric_id="change_request_closure_ratio_jira_patch", label="JIRA patch"
            ),
        ),
    ),
    ChaossStarterMetric(
        key="contributor_absence_factor",
        chaoss_name="Contributor Absence Factor",
        chaoss_url=_prior_art_url("contributor_absence_factor"),
        mapping_note=(
            "This project's existing contributor_absence_factor metric: the smallest number "
            "of contributors whose combined trailing-12-month commits reach 50% of all "
            "commits."
        ),
        series=(ChaossStarterSeries(metric_id="contributor_absence_factor", label=""),),
    ),
    ChaossStarterMetric(
        key="release_frequency",
        chaoss_name="Release Frequency",
        # issue #135: now sourced via `_prior_art_url` like the three cards
        # above -- `release_frequency` has a real `MetricMeta`/`PriorArt`
        # entry (collectors/release.py, metrics/release_cadence.py).
        chaoss_url=_prior_art_url("release_frequency"),
        mapping_note=(
            "This project's existing release_frequency metric: count of GA releases "
            "(collectors/release.py: git tags, excluding alpha/beta/rc) in the trailing-24-month "
            "window ending each completed month."
        ),
        series=(ChaossStarterSeries(metric_id="release_frequency", label=""),),
    ),
)
