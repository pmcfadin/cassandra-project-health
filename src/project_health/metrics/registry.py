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
    "truck_factor": "1.0",
    "contributor_absence_factor": "1.0",
    "contributor_hhi": "1.0",
    "elephant_factor": "1.0",
    "organizational_hhi": "1.0",
    "single_org_share": "1.0",
    "unknown_affiliation_rate": "1.0",
    # issue #35
    "time_to_first_reply_devlist": "1.0",
    "unanswered_thread_rate_devlist": "1.0",
    # issue #54
    "pr_merge_lead_time": "1.0",
    "pr_time_to_first_review": "1.0",
    "pr_time_to_close": "1.0",
    "pr_review_engagement": "1.0",
    "time_to_first_response_jira": "1.0",
    "stale_pr_rate": "1.0",
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
_INITIAL_ISSUE_53_CHANGELOG_NOTE = "Initial implementation (issue #53)."
_INITIAL_ISSUE_52_CHANGELOG_NOTE = "Initial implementation (issue #52, D6)."
_INITIAL_ISSUE_35_CHANGELOG_NOTE = "Initial implementation (issue #35)."
_INITIAL_ISSUE_54_CHANGELOG_NOTE = "Initial implementation (issue #54)."

_CHANGELOG_NOTES: dict[str, str] = {
    "active_contributors_monthly": _HEADCOUNT_FLOOR_CHANGELOG_NOTE,
    "new_contributors_monthly": _HEADCOUNT_FLOOR_CHANGELOG_NOTE,
    "unique_reviewers_monthly": _HEADCOUNT_FLOOR_CHANGELOG_NOTE,
    "reviewer_hhi": _INITIAL_M0_CHANGELOG_NOTE,
    "median_resolution_latency_jira": _INITIAL_M0_CHANGELOG_NOTE,
    "stale_jira_rate": _INITIAL_M0_CHANGELOG_NOTE,
    "pmc_joins_quarterly": _INITIAL_M0_CHANGELOG_NOTE,
    "truck_factor": _INITIAL_ISSUE_53_CHANGELOG_NOTE,
    "contributor_absence_factor": _INITIAL_ISSUE_53_CHANGELOG_NOTE,
    "contributor_hhi": _INITIAL_ISSUE_53_CHANGELOG_NOTE,
    "elephant_factor": _INITIAL_ISSUE_52_CHANGELOG_NOTE,
    "organizational_hhi": _INITIAL_ISSUE_52_CHANGELOG_NOTE,
    "single_org_share": _INITIAL_ISSUE_52_CHANGELOG_NOTE,
    "unknown_affiliation_rate": _INITIAL_ISSUE_52_CHANGELOG_NOTE,
    "time_to_first_reply_devlist": _INITIAL_ISSUE_35_CHANGELOG_NOTE,
    "unanswered_thread_rate_devlist": _INITIAL_ISSUE_35_CHANGELOG_NOTE,
    "pr_merge_lead_time": _INITIAL_ISSUE_54_CHANGELOG_NOTE,
    "pr_time_to_first_review": _INITIAL_ISSUE_54_CHANGELOG_NOTE,
    "pr_time_to_close": _INITIAL_ISSUE_54_CHANGELOG_NOTE,
    "pr_review_engagement": _INITIAL_ISSUE_54_CHANGELOG_NOTE,
    "time_to_first_response_jira": _INITIAL_ISSUE_54_CHANGELOG_NOTE,
    "stale_pr_rate": _INITIAL_ISSUE_54_CHANGELOG_NOTE,
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
    "truck_factor": (
        "Avelino et al. (2016) Degree-of-Authorship (DOA) truck/bus factor: the minimum "
        "number of contributors whose simultaneous departure leaves more than 50% of the "
        "project's (still-existing) files with no remaining qualifying 'author.' DOA(dev, "
        "file) = 3.293 + 1.098*FA + 0.164*DL - 0.321*ln(1+AC), where FA=1 if the developer "
        "authored the file's first observed commit, DL=that developer's commit count on the "
        "file, AC=every other developer's commit count on the file (RESEARCH.md §8.2, "
        "verified against the paper's own PDF). A developer is a file's 'author' iff their "
        "DOA normalized against the file's highest DOA exceeds 0.75 and their absolute DOA "
        "is >= 3.293 (the paper's own thresholds). Greedy algorithm: repeatedly remove "
        "whichever remaining developer is a qualifying author of the most still-covered "
        "files, until more than half the existing files have no qualifying author left; "
        "truck_factor = number removed. Computed from file_change_event (git log --no-merges "
        "--name-status; excludes generated/vendored paths per "
        "projects/<id>.yaml's truck_factor.excluded_path_globs -- METRICS.md §0.5). "
        "SNAPSHOT METRIC, NOT A WINDOWED RATE: each completed month's row uses every "
        "file_change_event up to that month's end (full accumulated history), not just that "
        "month's activity -- window_start == window_end == the snapshot date; this "
        "deliberately differs from the 'trailing-12m' label in METRICS.md's own §1 summary "
        "table, which its own §2 detail section (\"a snapshot metric, not a windowed rate\") "
        "overrides. details_json carries total_files, orphaned_files_at_start/_final, "
        "removed_developers (identity_ids, D2.3 audit trail), and the DOA thresholds used. "
        "KNOWN LIMITATIONS (RESEARCH.md §8.2): not validated as a failure predictor (the "
        "paper's own authors and a 2024 replication both caution against this -- losing "
        "truck-factor developers is not reliably fatal); DOA thresholds (k=0.75, m=3.293) "
        "were tuned on 133 popular GitHub projects, not a JIRA/ASF-governance project like "
        "Cassandra; measures file-authorship concentration, not review/design/institutional "
        "knowledge; DOA is computed per literal file path, not rename-followed; different "
        "reimplementations of this algorithm are known to disagree (Ferreira et al. 2019). "
        "Tier: experimental. Dimension: contributor sustainability. Role: key. Direction of "
        "good: higher. METRICS.md §2."
    ),
    "contributor_absence_factor": (
        "CHAOSS 'Bus Factor' / 'contributor dependency': sort non-bot, identity-resolved "
        "contributors by trailing-12-calendar-month commit count descending, and find the "
        "smallest N whose cumulative commits reach 50% of the window's total commits; N is "
        "the metric's value, output once per completed month (dense: one row per completed "
        "month from the first month any code_commit exists through the last completed month "
        "before the run's as_of date). details_json carries total_commits and the full ranked "
        "contributor list with each one's commits and cumulative_share (D2.3 audit trail), "
        "not just the top N. Simpler and cruder than truck_factor (commit count only, no "
        "per-file authorship), so it's reported alongside it rather than in place of it. "
        "Tier: established. Dimension: contributor sustainability. Role: supporting. "
        "Direction of good: higher. Window: trailing-12m, one row per completed month. "
        "METRICS.md §2."
    ),
    "contributor_hhi": (
        "Herfindahl-Hirschman Index (sum of squared commit shares) per non-bot, "
        "identity-resolved contributor, over trailing-12-calendar-month windows, dense (one "
        "row per completed month from the first month any code_commit exists through the "
        "last completed month before the run's as_of date). Same population and window as "
        "contributor_absence_factor; same HHI math as reviewer_hhi, applied to commit shares "
        "instead of review credits. details_json carries effective_contributor_population "
        "(1/HHI), not emitted as its own metric_value row -- the same convention reviewer_hhi "
        "established for its own reciprocal. Tier: established. Dimension: contributor "
        "sustainability. Role: key. Direction of good: lower. Window: trailing-12m, one row "
        "per completed month. METRICS.md §2."
    ),
    "elephant_factor": (
        "Minimum number of organizations whose combined trailing-12-calendar-month commits "
        "reach 50% of the window's total -- same algorithm as contributor_absence_factor, "
        "applied to organizational affiliation (D6) instead of individual identity. "
        "Organization is resolved per commit via affiliation_period: a curated "
        "affiliations.yaml entry (dated range) wins over a reviewed email-domain map "
        "(org_domains.yaml, some domains dated for an acquisition) wins over a GitHub "
        "profile's public company field (only via a reviewed org_aliases.yaml alias -- "
        "unmatched free text never counts, D6), in that order; the GitHub-company source is "
        "reached via collectors/github_commit_authors.py's platform-asserted commit-author "
        "association, which is what makes it useful for the gmail.com/apache.org/personal-"
        "domain majority the domain map alone can't resolve. A company field is a "
        "current-employer signal only (issue #52 fixup cycle 2): it's bounded to the trailing "
        "details_json.github_company_lookback_months months before that profile was fetched, "
        "never back-filled onto older commits, which fall through to email_domain/curated or "
        "stay unknown. Anything left over is 'unknown', "
        "which is treated as its own organization bucket for the cumulative-sum threshold so "
        "it cannot silently vanish from the denominator -- "
        "details_json.unknown_needed_to_reach_threshold records whether the threshold actually "
        "needed the unknown bucket to be reached. The §0.6 concentration floor (5, known "
        "organizations) AND a >= 50% unknown-share suppression (issue #52 fixup cycle 1: a "
        "window that clears the known-org-count floor while still majority-unaffiliated is not "
        "a trustworthy concentration reading) both gate value/flag; "
        "details_json.raw_value_before_floor carries the computed value even when suppressed. "
        "Marked experimental (not established) specifically because Cassandra's affiliation "
        "data quality -- not the algorithm -- is unproven at M0. "
        "Tier: experimental. Dimension: organizational diversity. Role: key. Direction of "
        "good: higher. Window: trailing-12m, one row per completed month. METRICS.md §5."
    ),
    "organizational_hhi": (
        "Herfindahl-Hirschman Index (sum of squared organizational commit shares), dense "
        "trailing-12-calendar-month windows, same population/window/org-resolution as "
        "elephant_factor. unknown counts as its own bucket in the HHI sum itself (D6, never "
        "redistributed), but the §0.6 known-organization-count floor and a >= 50% "
        "unknown-share suppression (issue #52 fixup cycle 1, see elephant_factor) both gate "
        "value/flag. The GitHub-company source's current-employer-only lookback bound (fixup "
        "cycle 2, see elephant_factor) applies here too. details_json carries "
        "github_company_lookback_months and effective_organizational_population (1/HHI), not "
        "emitted as its own metric_value row -- the same convention reviewer_hhi/"
        "contributor_hhi established for their own reciprocals. Tier: established. Dimension: "
        "organizational diversity. Role: key. Direction of good: lower. Window: trailing-12m, "
        "one row per completed month. METRICS.md §5."
    ),
    "single_org_share": (
        "Share of trailing-12-calendar-month commits from the single largest known "
        "organization -- the published CHAOSS 'Organizational Diversity' ratio, over the same "
        "org resolution and window as elephant_factor/organizational_hhi. unknown is never "
        "eligible to be 'the largest org' and is reported separately in details_json "
        "(unknown_commits/unknown_share) rather than folded into this figure, but the "
        "denominator is still every commit in the window (unknown included), so a "
        "high-unknown-rate window correctly produces a small value here rather than an "
        "inflated one; the §0.6 known-organization-count floor and a >= 50% unknown-share "
        "suppression (issue #52 fixup cycle 1, see elephant_factor) both gate value/flag; the "
        "GitHub-company source's current-employer-only lookback bound (fixup cycle 2, see "
        "elephant_factor) applies here too, recorded in "
        "details_json.github_company_lookback_months. "
        "Tier: established. Dimension: organizational diversity. Role: "
        "supporting -- largely implied by organizational_hhi (already key) and can miss "
        "multi-organization concentration HHI catches. Direction of good: lower. Window: "
        "trailing-12m, one row per completed month. METRICS.md §5."
    ),
    "unknown_affiliation_rate": (
        "Share of trailing-12-calendar-month commits whose author's organization is unknown "
        "per affiliation_period (D6's curated file + reviewed email-domain map + GitHub "
        "profile company field via a reviewed alias map), with the equivalent "
        "distinct-contributor-share version in "
        "details_json (a contributor counts as unknown only if none of their commits in the "
        "window resolved to a known organization). Deliberately has no direction of good -- "
        "this completeness metric doesn't get a health verdict of its own, it's a confidence "
        "modifier shown alongside elephant_factor/organizational_hhi/single_org_share. Tier: "
        "established (of its own denominator) -- it measures the size of the unknown bucket "
        "itself, which is exactly measurable, even though the metrics it caveats are "
        "experimental/established with caveats. Unlike the other three organizational "
        "metrics, its n/floor is total commits in the window (METRICS.md §0.6's default "
        "rate/ratio floor), not a count of known organizations. Dimension: organizational "
        "diversity. Role: supporting. Direction of good: none. Window: trailing-12m, one row "
        "per completed month. METRICS.md §5."
    ),
    "time_to_first_reply_devlist": (
        "Median days from a dev@ thread's first message to the first *qualifying* reply -- "
        "from a different sender than the root, excluding automated senders "
        "(projects/<id>.yaml mailing_lists.automated_senders) -- for threads started in each "
        "completed calendar month; details_json carries p90_days, n and threads_started (total "
        "threads started that month, which can exceed n while some of the month's threads "
        "haven't been answered yet). Threads whose root message itself came from an automated "
        "sender are excluded entirely. Metadata only (D1/D16): sender address, timestamp and "
        "thread structure derived from Message-ID/In-Reply-To/References -- no message body or "
        "subject text. KNOWN LIMITATION: unlike every other M0 metric, this metric's monthly "
        "rows are NOT dense across the whole span of history -- collectors/ponymail.py's "
        "oldest-first, per-run-capped Pony Mail backfill means a not-yet-collected month must "
        "never be reported as a false zero, so rows are emitted only through the dev@ list's "
        "backfill watermark plus the run's latest completed month, with details_json."
        "backfill_in_progress=true while a gap remains. Tier: established. Dimension: "
        "responsiveness. Role: key. Direction of good: lower. Window: monthly. METRICS.md §4."
    ),
    "unanswered_thread_rate_devlist": (
        "Share of dev@ threads started in a completed calendar month that receive zero "
        "qualifying replies (different sender than the root, non-automated) within 30 days of "
        "the thread's first message; details_json carries n_total, n_unanswered and "
        "followup_days. A month is only reported once every one of its threads has had its "
        "full 30-day follow-up window elapse (D5-style completed-period rule) -- so the most "
        "recent 1-2 completed months are typically not yet reportable. Threads whose root "
        "message came from an automated sender are excluded entirely. Metadata only (D1/D16). "
        "Shares the same not-dense-during-backfill caveat and details_json.backfill_in_progress "
        "flag as time_to_first_reply_devlist (collectors/ponymail.py's capped backfill). Tier: "
        "established. Dimension: responsiveness. Role: supporting. Direction of good: lower. "
        "Window: monthly. METRICS.md §4."
    ),
    "pr_merge_lead_time": (
        "Median and P90 days from pr.created_at to pr.merged_at, for PRs merged in each "
        "completed calendar month, bucketed by merge month; dense (one row per completed "
        "month from the first month any PR merged through the last completed month before "
        "as_of). details_json carries p90_days and n. Population: pr.merged = true across "
        "every configured pull_requests.repos (projects/cassandra.yaml lists 7). Tier: proxy "
        "-- GitHub PRs cover only part of Cassandra's actual review activity, which is "
        "JIRA-first (DECISIONS.md 'Cassandra-specific facts'); this metric is honest about "
        "measuring GitHub's slice, not the project's whole merge-lead-time picture. "
        "Dimension: responsiveness. Role: supporting (this dimension's key slots are already "
        "held by time_to_first_response_jira/stale_jira_rate/time_to_first_reply_devlist per "
        "METRICS.md §1's 'Key metrics per dimension' cap). Direction of good: lower. Window: "
        "monthly. No sample-size floor applies below n=5 (METRICS.md §0.6 latency floor) -- "
        "flag='insufficient_data' below that, row still emitted. DECISIONS.md D21 item 2, "
        "METRICS.md §0.6."
    ),
    "pr_time_to_first_review": (
        "Median and P90 days from pr.created_at to the earliest *non-self* "
        "pr_review.submitted_at for that PR, among PRs with at least one qualifying review, "
        "bucketed by the PR's creation month (the 'ready for review' moment); dense monthly. "
        "Excludes reviews where the reviewer is the PR author (orchestrator review fixup: "
        "GitHub records an author's own replies inside review threads as COMMENTED reviews "
        "by that author), compared at the identity level -- resolved_identity (issue #52's "
        "github_login identity_link) when a link exists, falling back to the raw login "
        "otherwise; an unresolvable side is never treated as a self-match. details_json "
        "carries p90_days and n. GitHub-PR-specific analog of METRICS.md §3's review_latency "
        "(which is computed from the git-trailer + JIRA-reviewer-field proxy instead) -- "
        "reported as its own metric rather than merged into review_latency, since the two "
        "draw from different, only partially-overlapping evidence (DECISIONS.md "
        "'Cassandra-specific facts': GitHub PRs cover only part of review activity). Tier: "
        "proxy. Dimension: reviewer capacity. Role: supporting (review_latency already holds "
        "this dimension's latency key slot). Direction of good: lower. Window: monthly. "
        "METRICS.md §0.6 latency floor (n=5) applies. DECISIONS.md D21 item 2."
    ),
    "pr_time_to_close": (
        "Median and P90 days from pr.created_at to pr.closed_at, for PRs closed (merged or "
        "not) in each completed calendar month, bucketed by close month; dense monthly. "
        "details_json carries p90_days, n, and n_merged (the subset of closed PRs that were "
        "also merged, vs. closed without merging). Tier: proxy (GitHub-only slice, same "
        "caveat as pr_merge_lead_time). Dimension: responsiveness. Role: supporting. "
        "Direction of good: lower. Window: monthly. METRICS.md §0.6 latency floor (n=5) "
        "applies. DECISIONS.md D21 item 2."
    ),
    "pr_review_engagement": (
        "Review engagement across GitHub PRs that received at least one *non-self* review "
        "in a completed calendar month (bucketed by pr_review.submitted_at): value = mean "
        "unique non-self reviewers per PR; details_json carries mean_reviews_per_pr, "
        "median_reviews_per_pr, n_prs (the population n), n_reviews and "
        "n_unique_reviewers_total (per-source counts, D2.3 audit trail) -- all counting only "
        "non-self reviews. Excludes the PR author's own review events from every count here "
        "(orchestrator review fixup, same identity-level self-review exclusion as "
        "pr_time_to_first_review); a PR whose only review activity is a self-review does not "
        "enter the population at all. Two related but distinct numbers reported together, "
        "matching METRICS.md §3's reviewer_top_k_share convention of never collapsing a "
        "multi-number concept into one. Tier: proxy. Dimension: reviewer capacity. Role: "
        "supporting (unique_reviewers_monthly already holds this dimension's headcount key "
        "slot; this metric is GitHub-PR-specific and narrower). Direction of good: none for "
        "the per-PR averages individually -- rising mean reviewers/PR is not obviously "
        "good or bad on its own (could mean healthy collaborative review, or could mean a "
        "few PRs are contentious) -- reported for context alongside reviewer_hhi/"
        "unique_reviewers_monthly, per METRICS.md's 'no metric alone drives status' pattern "
        "for composed/context metrics. Window: monthly. METRICS.md §0.6 rate/ratio floor "
        "(n=5 PRs) applies. DECISIONS.md D21 item 2."
    ),
    "time_to_first_response_jira": (
        "Median and P90 days from issue.created_at to the first issue_comment authored by "
        "someone other than the issue's reporter and not matching a bot_patterns "
        "jira_username pattern, for issues opened in each completed calendar month "
        "(the 'opened in window' framing -- METRICS.md §4 asks for both opened- and "
        "closed-in-window side by side; details_json's closed_in_window carries the same "
        "statistic bucketed by the qualifying comment's own month instead, as the "
        "closed-in-window cross-check). details_json also carries n_opened_in_window (every "
        "issue opened that month, for context) vs. n (the subset with a computable first "
        "response as of this run -- an issue with no comment yet, or whose only comments are "
        "by the reporter/a bot, simply isn't counted yet; it is not treated as a response of "
        "0 days). KNOWN LIMITATION: only comment-based responses are captured (a reviewer "
        "moving an issue straight to a new status without commenting is not detected -- M0 "
        "has no JIRA changelog collection); comment metadata itself is bounded to each "
        "issue's earliest 20 comments (collectors/jira.py MAX_COMMENTS_PER_ISSUE_STORED), "
        "which is a low-risk truncation for a first-response statistic. KNOWN LIMITATION "
        "(issue #79): production's JIRA watermark was already current when issue #54 added "
        "comment collection, so the ~21.5k pre-existing issues have no issue_comment rows "
        "until pipeline.py's budgeted, resumable historical backfill "
        "(_collect_jira_comment_backfill, ~1,000 issues/run newest-created-first) reaches "
        "them -- a month with any issue not yet covered by that backfill gets details_json."
        "backfill_in_progress=true (same disclosure pattern as time_to_first_reply_devlist's "
        "Pony Mail backfill), never a false zero or an unexplained insufficient_data gap. "
        "Tier: established. Dimension: responsiveness. Role: key (one of this dimension's key "
        "metrics, METRICS.md §1 -- JIRA, not GitHub PRs, is where most Cassandra review "
        "discussion happens). Direction of good: lower. Window: monthly. METRICS.md §0.6 "
        "latency floor (n=5) applies. METRICS.md §4."
    ),
    "stale_pr_rate": (
        "Share of currently-open GitHub PRs (state='OPEN') with no update in the last 90 "
        "days (configurable, details_json.threshold_days), as of the run's as_of date, "
        "across every configured pull_requests.repos. Reuses pr.updated_at as the staleness "
        "signal (GitHub's own updatedAt bumps on any review/comment/label/CI activity) -- "
        "same simplification stale_jira_rate makes for issue.updated_at, same disclosed "
        "limitation (a label or CI-triggered update, not necessarily human activity, can "
        "reset the clock). One snapshot row per run (window_start == window_end == as_of), "
        "not a monthly time series -- history accumulates from nightly snapshots going "
        "forward, same as stale_jira_rate. details_json carries n_open, n_stale, "
        "threshold_days, and a per-repo breakdown. Tier: established. Dimension: "
        "responsiveness. Role: supporting (stale_jira_rate already holds this dimension's "
        "staleness key slot, for the same JIRA-is-primary-venue reason as "
        "time_to_first_response_jira). Direction of good: lower. METRICS.md §0.6 rate/ratio "
        "floor (n=5 open PRs) applies. METRICS.md §4."
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
