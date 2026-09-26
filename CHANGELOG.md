# Changelog

Metric-definition changelog (ARCHITECTURE.md §4.4 / D2 rule 6: "nothing changes silently"). Entries are ordered
newest-first; a future version bump adds a new dated entry at the top.

## 2026-09-26

### scoring_version 1.0.0 (new): baseline statuses + versioned composite health score

Implements `docs/spec/SCORING.md`'s baseline-status math (§4-§5: trailing-24-completed-month median/MAD, the
modified z-score, the 1.5/3.0-sigma thresholds, the 2-of-3-completed-months sustained-trend confirmation, and
the `insufficient_data` floor below 12 completed baseline months) for every Phase-1 metric, and each dimension's
status via the worst-key-metric rule (§5.3): a dimension reads `declining` if any `key` metric is, `improving`
only if none are declining and at least one key metric is improving, `stable` otherwise, and `insufficient_data`
only when every key metric is. Results persist to each run's snapshot as three new tables
(`metric_baseline_status`, `dimension_status`, `composite_score`).

Adds DECISIONS.md D20's versioned 0-100 composite health score (amending D4's original "no composite, ever"):
weights, per-metric normalization functions and dimension inputs are published in `scoring.yaml` at
`scoring_version` 1.0.0. A dimension's composite score is the mean of its own `key` metrics' 0-100 normalized
scores (never its supporting metrics); a dimension with no classifiable key metric this month is dropped and the
remaining dimensions' weights are re-normalized to sum to 1.0, disclosed on the home page every time it applies.
The home page never shows the composite alone -- it always renders with its full per-dimension breakdown and a
visible flag beside it whenever any dimension's key metric is `declining`. Classified (Phase 2) metrics and
governance compliance (D14/D15) are both excluded from the composite; `scoring.yaml`'s
`composite.excluded_dimensions` states why for each. `docs/spec/SCORING.md` §9 ("what must never be combined")
is revised to carve out this one, narrowly-scoped exception and add the two new exclusions; a new §12 documents
the composite mechanism in full.

Closes #57.

## 2026-09-25

### reviewer_hhi, unique_reviewers_monthly, contributor leaderboard (issue #56): collector data correction, no definition_version change

Fixed `collectors/reviewer_trailer.py`'s "patch by"/"reviewed by" trailer parser (issue #77):
real apache/cassandra trunk history line-wraps a trailer paragraph at ~72 columns (e.g.
"reviewed by Dmitry Konstantinov and Sam\nTunnicliffe for CASSANDRA-21189"), and the parser's
per-line regex silently truncated a name split across the wrap -- "Sam Tunnicliffe" became
"Sam". Roughly 1,177 of ~11,650 "reviewed by" lines on trunk looked wrapped by this pattern,
and the raw `review_event` table had 4,790 single-token reviewer names (163 distinct), many
of them truncations of this kind.

This is a collector bug fix, not a metric-definition change (ARCHITECTURE.md §4.4: "a bug fix
that doesn't change the formula's meaning bumps the [pipeline_code_sha] but not necessarily
the [definition_]version; a formula change bumps both") -- `reviewer_hhi`'s and
`unique_reviewers_monthly`'s formulas are unchanged, only their `commit_trailer` input data is
corrected, so `definition_version` stays at its current value for both. The corrected values
still change, because D3 recomputes every metric from the full raw cache on every run:

- `reviewer_trailer.PARSER_VERSION` bumped 1 -> 2 (the "1" is implicit/pre-issue-#77 -- no
  such column existed before).
- Every historical `commit_trailer` `review_event` row (`raw/git/review_event`) and governance
  `commit_record` row (`raw/governance/commit_record`) is re-derived once, in place of the
  ordinary incremental collector's `since_sha` watermark (which would otherwise never revisit
  an already-collected commit): a new partition is appended stamping every row with
  `parser_version = 2` (`pipeline._reparse_commit_trailer_review_events_if_needed`,
  `pipeline._reparse_governance_commit_records_if_needed`). The original, stale-parser rows
  are never rewritten or deleted (append-only, D3) -- `pipeline._dedupe_commit_trailer_review_events`
  / `pipeline._dedupe_governance_commit_records` keep only each commit's *highest*
  `parser_version` row at metrics/leaderboard/governance read time, so the corrected
  attribution replaces the truncated one without an in-place rewrite.
- Real-data verification (a copy of the production data dir, re-run through the corrected
  pipeline, exit 0 / status `ok` / `metrics_missing: []`): Sam Tunnicliffe's trailing-12-month
  review count on the contributor leaderboard (D19, issue #56) rose from 27 to 31, independently
  confirmed against real `git log`/`git show` output for the underlying commits, including the
  four regression commits named in issue #77. Single-token `commit_trailer` reviewer names
  dropped from 4,790 (163 distinct) to 4,773 (154 distinct) at metrics-read time; no truncated
  row remained readable by the metrics after the fix (the raw partitions still hold both the
  original and reparsed rows, per D3's append-only rule -- only the read-time dedup changed).
- Governance's `reviewer-present` and `jira-ticket-referenced` checks (docs/spec/GOVERNANCE.md
  §8) were **not** affected by this bug and show identical pass/fail/unknown counts before and
  after: both checks' boolean presence semantics (any reviewer found at all; any issue key found
  anywhere in the commit message) already tolerated a wrapped trailer, so a wrapped commit that
  used to show `reviewers: ['Dmitry Konstantinov', 'Sam', ...]` now shows `['Dmitry Konstantinov',
  'Sam Tunnicliffe', ...]` -- the same `pass` result, with corrected evidence text. The bug's real
  governance-adjacent effect was confined to `commit_record.trailer_reviewers`' individual names,
  now corrected by the same reparse/dedup mechanism as `review_event`.

Closes #77.

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
