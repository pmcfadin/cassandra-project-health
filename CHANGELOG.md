# Changelog

Metric-definition changelog (ARCHITECTURE.md §4.4 / D2 rule 6: "nothing changes silently"). Entries are ordered
newest-first; a future version bump adds a new dated entry at the top.

## 2026-09-25

### active_contributors_monthly, new_contributors_monthly, unique_reviewers_monthly: 1.0 -> 1.1

Headcount metrics (active/new contributors, unique reviewers) now report their value for
any sample size n, including 0, always with flag='ok'. Previously these three metrics
incorrectly inherited METRICS.md §0.6's rate/ratio sample-size floor (n >= 5), which
suppressed real, meaningful low-n months (e.g. 3 new contributors in a month) as
`insufficient_data` -- hiding exactly the onboarding/attrition trend the dashboard exists
to surface. Floors continue to apply to rate/ratio metrics (`stale_jira_rate`),
concentration metrics (`reviewer_hhi`), and latency statistics
(`median_resolution_latency_jira`), where small n genuinely makes the statistic unstable.

Closes #27.
