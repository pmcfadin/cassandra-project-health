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
GOVERNANCE_METRICS: dict[str, MetricMeta] = {
    "governance_reviewer_present_pass_rate": MetricMeta(
        metric_id="governance_reviewer_present_pass_rate",
        name="Reviewer Present — Pass Rate",
        dimension="governance",
        tier="established",
        direction_of_good="higher",
        value_kind="percent",
        page="governance",
        sources=("git", "jira"),
    ),
    "governance_jira_ticket_referenced_pass_rate": MetricMeta(
        metric_id="governance_jira_ticket_referenced_pass_rate",
        name="JIRA Ticket Referenced — Pass Rate",
        dimension="governance",
        tier="established",
        direction_of_good="higher",
        value_kind="percent",
        page="governance",
        sources=("git",),
    ),
    "governance_pre_commit_ci_evidence_pass_rate": MetricMeta(
        metric_id="governance_pre_commit_ci_evidence_pass_rate",
        name="Pre-Commit CI Evidence — Pass Rate",
        dimension="governance",
        tier="proxy",
        direction_of_good="higher",
        value_kind="percent",
        page="governance",
        sources=("git",),
    ),
    "governance_code_style_checkstyle_pass_rate": MetricMeta(
        metric_id="governance_code_style_checkstyle_pass_rate",
        name="Checkstyle — Pass Rate",
        dimension="governance",
        tier="established",
        direction_of_good="higher",
        value_kind="percent",
        page="governance",
        sources=("git",),
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
