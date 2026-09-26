"""M0 metric registry (`metric_definition_version` rows, ARCHITECTURE.md §4.4).

Most M0 metrics ship at version ``"1.0"``. The three headcount metrics
(`active_contributors_monthly`, `new_contributors_monthly`,
`unique_reviewers_monthly`) are at ``"1.1"`` as of issue #27 (2026-09-25):
they no longer apply METRICS.md §0.6's sample-size floor, since a raw
headcount is already the complete, meaningful statistic at any `n`,
including 0. `changed_at`/`changelog_note` record when and why a version
was introduced; per ARCHITECTURE.md §4.4 a version is never mutated in
place — a future formula change adds a new `(metric_id, version)` row
rather than editing this one.

Descriptions are condensed from each metric's own section in
`docs/spec/METRICS.md` (source of truth); this module exists so a
`metric_value` row's `definition_version` resolves to a human-readable
formula/tier/phase summary without a reader (or the site, task #8)
re-parsing the spec doc.
"""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa

from project_health.schema import get_schema, validate

# metric_id -> the version that metric currently ships at. Kept in lockstep
# with engine.py's `DEFINITION_VERSIONS` for the same metric_id.
_VERSIONS: dict[str, str] = {
    "active_contributors_monthly": "1.1",
    "new_contributors_monthly": "1.1",
    "unique_reviewers_monthly": "1.1",
    "reviewer_hhi": "1.0",
    "median_resolution_latency_jira": "1.0",
    "stale_jira_rate": "1.0",
    "pmc_joins_quarterly": "1.0",
}

_INITIAL_M0_CHANGELOG_NOTE = "Initial M0 implementation (issue #7)."

_HEADCOUNT_FLOOR_CHANGELOG_NOTE = (
    "1.0 -> 1.1 (issue #27, 2026-09-25): no longer applies METRICS.md §0.6's rate/ratio "
    "sample-size floor. Reports its value for any sample size n, including 0, always with "
    "flag='ok' -- a raw headcount is already the complete, meaningful statistic at any n, "
    "not an estimate whose stability depends on sample size. Previously a low-n month (e.g. "
    "3 new contributors) was suppressed as insufficient_data, hiding real onboarding/attrition "
    "signal."
)

# metric_id -> changelog_note for its current version.
_CHANGELOG_NOTES: dict[str, str] = {
    "active_contributors_monthly": _HEADCOUNT_FLOOR_CHANGELOG_NOTE,
    "new_contributors_monthly": _HEADCOUNT_FLOOR_CHANGELOG_NOTE,
    "unique_reviewers_monthly": _HEADCOUNT_FLOOR_CHANGELOG_NOTE,
    "reviewer_hhi": _INITIAL_M0_CHANGELOG_NOTE,
    "median_resolution_latency_jira": _INITIAL_M0_CHANGELOG_NOTE,
    "stale_jira_rate": _INITIAL_M0_CHANGELOG_NOTE,
    "pmc_joins_quarterly": _INITIAL_M0_CHANGELOG_NOTE,
}

# metric_id -> description, condensed from METRICS.md's own "## <id>" sections.
_DESCRIPTIONS: dict[str, str] = {
    "pmc_joins_quarterly": (
        "New PMC members per quarter (from join dates only). Sourced directly from the Whimsy "
        "public roster (ground truth, no identity ambiguity). Note: Whimsy currently shows only "
        "current members, so departures and historical departures are not visible in this metric. "
        "As historical roster snapshots accumulate, real net change (joins minus departures) will "
        "become computable. Tier: established. Dimension: contributor sustainability. Role: "
        "supporting. Direction of good: higher. Window: quarterly, completed quarters only. No "
        "sample-size floor applies (roster entries are already resolved identity data per "
        "METRICS.md §0.6). METRICS.md §5."
    ),
    "active_contributors_monthly": (
        "Count of distinct non-bot, identity-resolved individuals who performed at least one "
        "code_commit (non-merge git commit, attributed by author email) in the calendar month. "
        "No sample-size floor applies (issue #27, definition_version 1.1): reports its value for "
        "any n, including 0, always with flag='ok' -- a raw headcount is already the complete, "
        "meaningful statistic regardless of sample size. Tier: established. Dimension: "
        "contributor sustainability. Role: supporting. Direction of good: higher. Window: "
        "monthly, completed months only. METRICS.md §2."
    ),
    "new_contributors_monthly": (
        "Count of contributors whose first-ever code_commit to the tracked repo(s), across the "
        "full project history (not just the current window), falls in the calendar month. No "
        "sample-size floor applies (issue #27, definition_version 1.1): reports its value for "
        "any n, including 0, always with flag='ok' -- a raw headcount is already the complete, "
        "meaningful statistic regardless of sample size. Tier: established. Dimension: "
        "contributor sustainability. Role: supporting. Direction of good: higher. Window: "
        "monthly, completed months only. METRICS.md §2."
    ),
    "unique_reviewers_monthly": (
        "Count of distinct non-bot individuals credited as a reviewer -- via non-merge commit "
        "trailer parse (source=commit_trailer) or the JIRA Reviewers/Reviewer custom field(s) "
        "(source=jira_field) -- on at least one change in the calendar month. Value is the union "
        "across both sources (OPEN-QUESTIONS.md #2 default); details_json carries "
        "commit_trailer_count, jira_field_count and union_count so the per-source breakdown is "
        "auditable (D2.3). No sample-size floor applies (issue #27, definition_version 1.1): "
        "reports its value for any n, including 0, always with flag='ok' -- a raw headcount is "
        "already the complete, meaningful statistic regardless of sample size. KNOWN LIMITATION: "
        "naive M0 identity resolution "
        "(normalize/identity.py) never cross-type-merges a commit-trailer git_name identity with "
        "the same person's git_email or jira_username identity, so one human credited via both a "
        "trailer and a JIRA field can be counted twice in the union -- an honest overcount, "
        "disclosed here rather than silently corrected; expected to improve once identity "
        "resolution matures past M0. Tier: proxy. Dimension: reviewer capacity. Role: key. "
        "Direction of good: higher. Window: monthly, completed months only, 2017+ baseline "
        "(METRICS.md §0.4). METRICS.md §3."
    ),
    "reviewer_hhi": (
        "Herfindahl-Hirschman Index (sum of squared review-credit shares) per source-agnostic "
        "reviewer identity, over trailing-12-calendar-month windows ending at each completed "
        "month (dense: one row per completed month from the first month any review activity "
        "exists through the last completed month before the run's as_of date, even where a "
        "given month has zero credited reviews). VALUE (v1.0): computed from commit_trailer "
        "credits only, not the union of commit_trailer + jira_field. Crediting both sources "
        "double-counts a single review as two credits, and under M0's naive, non-cross-type "
        "identity resolution those two credits land on two different identities (a git_name "
        "identity and a jira_username identity that may be the same human), which deflates HHI "
        "relative to true concentration; commit_trailer is also the denser signal since 2017 "
        "(METRICS.md §0.4). details_json carries effective_reviewer_population (1/HHI, from the "
        "commit_trailer value), jira_field_hhi and union_hhi (the same source-mixed computation "
        "this metric used before v1.0's fixup, kept as cross-checks rather than the value), and "
        "before_reliable_from (true when the window ends before the project's reviewer-data "
        "reliable_from date, METRICS.md §0.4 -- pre-2017 Cassandra reviewer-trailer coverage is "
        "materially patchier and should not anchor a baseline). Tier: proxy. Dimension: reviewer "
        "capacity. Role: key. Direction of good: lower. METRICS.md §3."
    ),
    "median_resolution_latency_jira": (
        "Median days from JIRA issue creation to resolution, for issues resolved in each "
        "completed calendar month; details_json carries p90_days and n. Tier: established. "
        "Dimension: responsiveness. Role: supporting. Direction of good: lower. Window: monthly, "
        "completed months only. METRICS.md §4."
    ),
    "stale_jira_rate": (
        "Share of currently-open JIRA issues (resolved_at is null) with no update in the last "
        "90 days (configurable, details_json.threshold_days), as of the run's as_of date. M0 has "
        "no JIRA comment or changelog history, so issue.updated_at is the only staleness signal "
        "available and this project's collectors cannot reconstruct history for this metric -- "
        "each run emits exactly one snapshot row (window_start == window_end == as_of, not a "
        "monthly time series), and history for this metric accumulates only from nightly "
        "snapshots going forward. Tier: established. Dimension: responsiveness. Role: key. "
        "Direction of good: lower. METRICS.md §4."
    ),
}

METRIC_IDS: tuple[str, ...] = tuple(_DESCRIPTIONS)


def build_registry(changed_at: datetime) -> pa.Table:
    """Build the `metric_definition_version` table: one row per M0 metric,
    each stamped with its own current `version` (see `_VERSIONS`) and a
    `changelog_note` describing that version.

    `changed_at` is caller-supplied (not wall-clock) so the table is a pure
    function of its input, matching this project's reproducibility rule. All
    6 rows share the same `changed_at` in a given `build_registry()` call --
    the per-metric distinction is `version`/`changelog_note`, not `changed_at`.
    """
    rows = [
        {
            "metric_id": metric_id,
            "version": _VERSIONS[metric_id],
            "description": description,
            "changed_at": changed_at,
            "changelog_note": _CHANGELOG_NOTES[metric_id],
        }
        for metric_id, description in _DESCRIPTIONS.items()
    ]
    schema = get_schema("metric_definition_version")
    return validate("metric_definition_version", pa.Table.from_pylist(rows, schema=schema))
