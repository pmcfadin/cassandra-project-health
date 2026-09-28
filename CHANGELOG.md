# Changelog

Metric-definition changelog (ARCHITECTURE.md §4.4 / D2 rule 6: "nothing changes silently"). Entries are ordered
newest-first; a future version bump adds a new dated entry at the top.

## 2026-09-27

### governance-policy.yaml v1 -> v2: every rule and exemption sourced from official Cassandra rules (D24, issue #93)

Implements DECISIONS.md D24 ("We should always follow the rules set by the PMC and not make up our own"):
`governance-policy.yaml` version 2 removes every rule/exemption this project invented rather than sourced from
the Cassandra project's own published rules, sources the ones that remain, and adds one new rule the official
docs now require. `rescore_history: true` -- the entire commit history is rescored under v2, per each rule's
own `effective_from` (D14: "no commit is judged against a rule the project had not yet published"). See
`docs/spec/GOVERNANCE.md`'s "Approved v2" section for the full rationale and every source quote.

**What changed:**

- **Removed** (no official source found): the `ninja` exemption (`reviewer-present`,
  `jira-ticket-referenced`) and the `submodule-repin` sub-pattern of `release-housekeeping`. Both were audited
  project *conventions*, never a PMC-published rule. The ninja-count-trend stays as a purely descriptive,
  unscored signal (`commit_fact.ninja_declared`, `governance/checks.is_ninja_declared`), computed independently
  of scoring.
- **Added**, sourced to the ratified cwiki governance page: `commit-then-review` (a measurement-choice
  docs-only-change detector, `docs_only_paths` glob matching, replaces the old ninja/release-housekeeping
  pattern match).
- **Sourced**, replacing the unsourced `release-housekeeping` group: `release-process` (`version-increment` ->
  `release_process.html`'s exact `git commit` invocation; `debian-changelog` -> the exact
  `cassandra-builds/cassandra-release/prepare_release.sh` line that produces it).
- **Extended**: `pre-commit-ci-evidence` now also scores from JIRA attachments (`ci_summary*`/
  `results_details*`), and evidence must be dated at or before the commit (evidence found only after the
  commit is its own distinct `unknown`, not folded into "no evidence").
- **New rule**: `ci-artefacts-attached` (effective 2026-08-19, `fail_allowed: true`) -- both pre-commit CI
  artefacts attached to the referenced issue at or before the commit, sourced to `patches.html`/`ci.html`.
  Only fails an issue whose attachment list has actually been fetched (JIRA attachment metadata backfill,
  filename/created/id only, never content, budgeted/resumable/newest-first, one combined JIRA call per issue
  alongside the existing comment-evidence fetch).
- **Enforcement**: `tests/test_governance_policy_sources.py` fails the suite if any scored rule/exemption/
  sub_pattern lacks a source or cites an unlisted `source_type`; `project-health verify-policy-sources`
  re-fetches every source live and confirms every quote still appears (opt-in live test,
  `RUN_LIVE_NETWORK_TESTS=1`, run live during implementation -- all sourced items passed).

**v1 -> v2 counts per check x result**, real data, full rescored history (`apache/cassandra`, all branches this
project tracks, as of 2026-09-28; git+JIRA+GitHub-checks evidence already cached from prior runs plus a fresh
JIRA attachment backfill this run):

| Check | Result | v1 | v2 | Delta |
|---|---|---:|---:|---:|
| `reviewer-present` | pass | 3,089 | 3,030 | -59 |
| `reviewer-present` | fail | 11 | 15 | +4 |
| `reviewer-present` | unknown | 3,493 | 3,554 | +61 |
| `reviewer-present` | exempt | 420 | 414 | -6 |
| `reviewer-present` | not_in_force | 25,363 | 25,363 | 0 |
| `jira-ticket-referenced` | pass | 12,612 | 12,821 | +209 |
| `jira-ticket-referenced` | unknown | 18,885 | 19,314 | +429 |
| `jira-ticket-referenced` | exempt | 879 | 241 | -638 |
| `pre-commit-ci-evidence` | pass | 876 | 923 | +47 |
| `pre-commit-ci-evidence` | unknown | 6,137 | 5,676 | -461 |
| `pre-commit-ci-evidence` | exempt | 0 | 414 | +414 |
| `pre-commit-ci-evidence` | not_in_force | 25,363 | 25,363 | 0 |
| `code-style-checkstyle` | pass | 389 | 389 | 0 |
| `code-style-checkstyle` | fail | 10 | 10 | 0 |
| `code-style-checkstyle` | unknown | 31,977 | 31,977 | 0 |
| `ci-artefacts-attached` (new) | pass | 0 | 37 | +37 |
| `ci-artefacts-attached` (new) | fail | 0 | 30 | +30 |
| `ci-artefacts-attached` (new) | unknown | 0 | 97 | +97 |
| `ci-artefacts-attached` (new) | exempt | 0 | 6 | +6 |
| `ci-artefacts-attached` (new) | not_in_force | 0 | 32,206 | +32,206 |

32,376 commits scored (`apache/cassandra`, all tracked branches), real data, 2026-09-28. `reviewer-present`'s
`exempt` count barely moved (420 -> 414: `commit-then-review`'s docs-only-paths detector catches almost the
same commits `ninja` used to, since most self-declared "ninja" fixes were themselves docs-only) but its
composition changed completely -- v1's 420 were mostly `ninja`-pattern matches; v2's 414 are entirely
`commit-then-review` (docs-only-change) and `release-process`, since `ninja` is gone. `jira-ticket-referenced`
`exempt` dropped sharply (879 -> 241) because v1's `release-housekeeping` group (which included the
`submodule-repin` pattern) matched more commits by regex than v2's dated `release-process` sub-patterns do --
`submodule-repin` had no official source and its commits now fall through to `pass`/`unknown` based on whether
they reference an issue key, which is why `jira-ticket-referenced`'s `pass`/`unknown` both rose.
`pre-commit-ci-evidence`'s new `exempt` count (414) is entirely the `not-code`/`release-process` exemptions v1
never had (v1 scored docs-only commits as `unknown`, same as any other commit with no CI comment).
`ci-artefacts-attached`'s 170 commits scored so far (attachment backfill is budgeted/incremental, D14/D15 --
`unknown` will keep falling in later runs as the backlog clears) since it went into force on 2026-08-19: 37
pass, 30 fail, 97 unknown, 6 exempt.

**Follow-up correction (same day):** live spot-checks of 3 real fails against the JIRA attachments API
surfaced a filename-match defect -- real `.build/run-ci` attachments are sometimes named
`result_details.tar.gz` (no "s") rather than `results_details*`, and sometimes prefixed
`<ISSUE-KEY>-<branch>-ci_summary.html` rather than a bare `ci_summary*` -- in the *measurement* (the
`check_method`/code filename match), not the sourced rule itself. Per D24, `check_method.detail` prose is
this project's own measurement choice, not protected rule text, so it was corrected: `governance/checks.py`'s
filename match is now an unanchored substring search accepting either `results_details` or `result_details`,
and `governance-policy.yaml`'s two `check_method.detail` texts (only) now describe that match with both real
filenames as examples -- no `source_quote`/`source_url`/`source_type`/`effective_from`/`exemptions`/
`result_semantics` changed. Re-scored from the same cached raw evidence (no re-collection):
`ci-artefacts-attached`'s breakdown went from 16 pass / 51 fail / 97 unknown / 6 exempt to **37 pass / 30
fail / 97 unknown / 6 exempt** -- 21 of the original 51 fails were the filename-match defect, 3 have the
artefacts attached only after the commit, and 27 are real misses. Fail evidence now distinguishes "no CI
artefacts attached", "results_details missing (ci_summary attached)" (or the symmetric case), and "artefacts
attached only after commit".

Governance page: each rule's row now shows its `source_type` and `source_quote` linked to `source_url`; a
small note lists the exemptions removed in v2 with the reason; the new rule's trend/headline card renders via
the existing `SCORED_CHECK_IDS`-generic drift/headline machinery (issue #69), no page-specific code needed.

Closes #93.

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
