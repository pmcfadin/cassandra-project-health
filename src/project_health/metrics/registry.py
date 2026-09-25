"""M0 metric registry (`metric_definition_version` rows, ARCHITECTURE.md §4.4).

Every M0 metric ships at version ``"1.0"``. `changed_at`/`changelog_note`
record when and why a version was introduced; per ARCHITECTURE.md §4.4 a
version is never mutated in place — a future formula change adds a new
`(metric_id, version)` row rather than editing this one.

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

DEFINITION_VERSION = "1.0"

# metric_id -> description, condensed from METRICS.md's own "## <id>" sections.
_DESCRIPTIONS: dict[str, str] = {
    "active_contributors_monthly": (
        "Count of distinct non-bot, identity-resolved individuals who performed at least one "
        "code_commit (non-merge git commit, attributed by author email) in the calendar month. "
        "Tier: established. Dimension: contributor sustainability. Role: supporting. "
        "Direction of good: higher. Window: monthly, completed months only. METRICS.md §2."
    ),
    "new_contributors_monthly": (
        "Count of contributors whose first-ever code_commit to the tracked repo(s), across the "
        "full project history (not just the current window), falls in the calendar month. "
        "Tier: established. Dimension: contributor sustainability. Role: supporting. "
        "Direction of good: higher. Window: monthly, completed months only. METRICS.md §2."
    ),
    "unique_reviewers_monthly": (
        "Count of distinct non-bot individuals credited as a reviewer -- via non-merge commit "
        "trailer parse (source=commit_trailer) or the JIRA Reviewers/Reviewer custom field(s) "
        "(source=jira_field) -- on at least one change in the calendar month. Value is the union "
        "across both sources (OPEN-QUESTIONS.md #2 default); details_json carries "
        "commit_trailer_count, jira_field_count and union_count so the per-source breakdown is "
        "auditable (D2.3). KNOWN v1.0 LIMITATION: naive M0 identity resolution "
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
    """Build the `metric_definition_version` table: all 6 M0 metrics at "1.0".

    `changed_at` is caller-supplied (not wall-clock) so the table is a pure
    function of its input, matching this project's reproducibility rule.
    """
    rows = [
        {
            "metric_id": metric_id,
            "version": DEFINITION_VERSION,
            "description": description,
            "changed_at": changed_at,
            "changelog_note": "Initial M0 implementation (issue #7).",
        }
        for metric_id, description in _DESCRIPTIONS.items()
    ]
    schema = get_schema("metric_definition_version")
    return validate("metric_definition_version", pa.Table.from_pylist(rows, schema=schema))
