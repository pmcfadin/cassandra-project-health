# METRICS.md — Candidate Metric Catalog

Status: draft for review. Companion to `docs/spec/DECISIONS.md` (binding) and `docs/research.md` (original brief).
Phase-2 classified/interaction metrics are listed only briefly here; their taxonomy, thread model, and validation
strategy belong in `COMMUNITY-HEALTH.md` (written separately).

## 0. Conventions

### 0.1 Tiers (D2.1)

Every metric on this project's dashboard is tagged with exactly one tier, shown wherever the metric appears:

| Tier | Meaning |
|---|---|
| `established` | Widely used in CHAOSS and/or peer-reviewed literature, with a standard formula and known validity limits. |
| `proxy` | A reasonable, defensible stand-in for something we cannot observe directly (e.g., "reviewer" approximated from commit-message conventions rather than a first-class review object). Named as a proxy so nobody mistakes it for the thing itself. |
| `experimental` | Mathematically well-defined but not yet validated for this project or this domain (e.g., survival-analysis churn definitions, elephant factor applied to a single-repo ASF project). Shown with a visible caveat. |
| `classified` | Derived from an LLM/NLP classification step over communication text. Phase 2 only; governed by `COMMUNITY-HEALTH.md`'s validation gates before it ships. |

### 0.2 Phases

- **Phase 1** — computable today from git, ASF JIRA, GitHub PRs, mailing-list *metadata*, and ASF roster data (per D1).
- **Phase 2** — requires classification of message content (2a) or Slack access (2b), gated per D1/D7.

### 0.3 What counts as a "contribution" — do not mash activity types together

"Contribution" is not a single countable thing in this project. Each activity type is counted, reported, and
thresholded **separately**, and any metric that combines them must say so explicitly in its own definition and
show the components on request. The activity types this project instruments in Phase 1:

| Activity type | Unit | Source | Notes |
|---|---|---|---|
| `code_commit` | one git commit on a tracked branch | git log | Attributed to the commit author (not committer) email. **All merge commits are excluded**, not just no-diff ones: Cassandra merges each fix forward across release branches (e.g. cassandra-5.0 → cassandra-6.0 → trunk), so merge commits are a structural artifact of the branching model, not new contribution, and counting them double- and triple-counts the same underlying patch. Collectors use `git log --no-merges`. Verified against the actual repository (`docs/spec/data-probe.md`): of 33,429 total commits, a large share are forward-merges; the reviewer-trailer share of *all* commits (35.9%) is materially different from its share of *non-merge* commits (43–87%, rising over time — see §0.4), which is exactly the distortion merge-commit inclusion causes. |
| `code_review` | one instance of `reviewed by X` in a non-merge commit trailer, cross-checked against the ASF JIRA `Reviewer(s)` custom fields | git commit trailer parse (non-merge commits only) + ASF JIRA REST API (`customfield_12313420` "Reviewers", `customfield_10022` "Reviewer") + GitHub Reviews API | See §0.4 — two independent, cross-checkable proxy sources, not a first-class review record. |
| `jira_issue_authored` | one JIRA issue created | ASF JIRA REST API | Bug reports, improvements, tasks — not sub-task noise unless configured. |
| `jira_comment` | one JIRA issue comment | ASF JIRA REST API | Excludes automated bot comments (CI, JIRA-GitHub bridge). |
| `github_pr` | one pull request opened against `apache/cassandra` | GitHub REST/GraphQL API | Distinct from JIRA issues; Cassandra's PR volume is a subset of total review activity (DECISIONS.md, "Cassandra-specific facts"). |
| `github_pr_review` | one GitHub review event (approve/request-changes/comment) | GitHub REST API | |
| `mailing_list_post` | one message to dev@ or user@ | Pony Mail JSON API (metadata only in Phase 1: sender, timestamp, thread id, subject — never body) | |
| `release` | one published Cassandra release (GA, not RC unless configured) | ASF `dist.apache.org` / GitHub Releases / git tags | |

A metric labeled e.g. `active_contributors_monthly` must state, in its own definition, which of the above unit
types it counts as qualifying activity, so a reader can tell whether "contributor" means "committed code" or
"committed code OR commented on JIRA."

### 0.4 The Cassandra reviewer proxy

Cassandra commits conventionally carry a trailer such as `patch by A; reviewed by B, C for CASSANDRA-NNNNN` (per
DECISIONS.md), and ASF JIRA has two custom fields that independently record reviewers on an issue:
`customfield_12313420` ("Reviewers", multi-user) and `customfield_10022` ("Reviewer", single-user). This project
uses **both sources and cross-checks them** — a genuine reviewer credit should normally show up in the commit
trailer of the merging commit *and* on the corresponding JIRA issue's reviewer field(s); where they disagree,
both are shown rather than silently preferring one (D2.3 — auditability). This is still a **proxy**, not an
established measurement, because:

- The commit trailer is free text; parsing is heuristic (name variants, "reviewed by" vs "review by" vs missing
  entirely).
- Both sources record *credit*, not necessarily *effort* — a reviewer named may have done a five-minute sanity
  check or a two-week deep review; neither source distinguishes these.
- Committers sometimes omit the trailer or add it retroactively; the JIRA field is filled in by whoever updates
  the issue and can likewise be missed.

**Verified coverage (`docs/spec/data-probe.md`, corrected 2026-09-25):** counting only non-merge commits (merge
commits must always be excluded — see §0.3 `code_commit`), the share of Cassandra trunk commits carrying a
`reviewed by` trailer rose from roughly 43–57% in 2009–2014 to 71–77% in 2016–2017 and has held at 79–87% every
year from 2018 onward. On the JIRA side, the Reviewers/Reviewer fields are populated on 11,125 and 7,104
CASSANDRA issues respectively (out of 21,483 total issues), but that population skews toward more recent issues
since the fields were not always in use. **Practical consequence:** reviewer-proxy metrics (`unique_reviewers_monthly`,
`reviewer_top_k_share`, `reviewer_hhi`, `effective_reviewer_population`, `review_latency`, `review_load_per_reviewer`,
`contributor_reviewer_ratio`) should set their trailing-baseline window to start no earlier than **2017**; years
before that have materially patchier trailer coverage and would understate reviewer activity relative to true
review effort, not because review didn't happen, but because it wasn't recorded in a form these collectors can
see. This is a data-availability constraint, documented here and reiterated in `SCORING.md` §6 as a structural
break to apply consistently rather than something each metric handles ad hoc.

Where GitHub PR review events exist for the same change, they are captured as a third signal and reconciled the
same way.

### 0.5 Population and exclusions (applies to all metrics unless a metric overrides it)

- **Bots excluded.** A contributor is classified as a bot if its identity matches a maintained bot list (GitHub
  `[bot]` suffix accounts, `Hudson`/Jenkins/ASF CI service accounts, `jira-bot`, ASF INFRA automation) or is added
  to a manual bot-exclusion list reviewed alongside `affiliations.yaml`. Bot exclusion is itself audit-logged: which
  identities were excluded, and why, per run.
- **Identity-uncertain contributors are not merged.** Per D2.5 and the research brief's identity-resolution
  guidance, two git emails, a GitHub login, and a JIRA username are only treated as one person when the match is
  high-confidence (exact email match, or an explicit mapping the contributor or a committer confirms). Anything
  below that confidence threshold is counted as separate identities and flagged `identity: unresolved` — it is
  never silently merged and never silently dropped.
- **Affiliation defaults to `unknown` (D6).** No metric imputes or guesses an organization from a name or writing
  style. Unresolved affiliation is its own bucket, shown in the denominator, never redistributed across known
  organizations.

### 0.6 Minimum sample size rule (applies per metric; specific overrides noted per metric)

**Default rule:** if the population contributing to a metric's numerator or denominator in the current window is
below the metric's stated floor, the dashboard renders `insufficient data` instead of a number, and no
improving/stable/declining status is computed for it that period (consistent with D5's "partial months never
drive status" and this project's broader anti-false-precision stance; see `SCORING.md` §5 for how this propagates
to dimension status). Default floors, absent a metric-specific override:

- Rate/ratio metrics over a monthly window: floor = **5** qualifying events in the window.
- Concentration metrics (HHI, top-k share, bus/elephant factor): floor = **5** distinct contributors/organizations
  in the window.
- Cohort/survival metrics: floor = **30** individuals in the cohort for a reported Kaplan–Meier curve; smaller
  cohorts may be shown as raw counts only, never as a smoothed curve.
- Latency metrics (medians): floor = **5** closed events in the window; below that, show the raw list of events
  instead of a summary statistic.

**Scope of this rule (owner decision, 2026-09-25, issue #27):** this floor applies to **rate/ratio metrics,
concentration metrics, and latency statistics** — metrics where the reported number is a *statistic computed over*
a population, so a small population makes that statistic unstable (a rate over 2 events, a median of 2 latencies,
or an HHI over 2 contributors is not a trustworthy estimate). It does **not** apply to plain headcounts/counts. A
raw count of "how many people did X in this window" is already the complete, meaningful statistic at any `n`,
including 0 — it is not an estimate whose variance shrinks as `n` grows the way a rate, an HHI share, or a median
is. Suppressing a true low-n headcount as `insufficient data` hides real information (e.g., a month with 3 new
contributors is real, meaningful signal about the onboarding trend), rather than protecting against instability.

`active_contributors_monthly`, `new_contributors_monthly`, and `unique_reviewers_monthly` are exempted from this
floor for exactly that reason: they always report `flag = 'ok'` and `value = n`, for any `n` including 0
(`definition_version` 1.1 as of this decision).

---

## 1. Summary Table

`direction_of_good` is one of `higher` (rising = improving), `lower` (rising = declining), `target-range` (neither
extreme is healthy — judged against the project's own historical range, not monotonic), or `none` (movement is
informative but not classifiable as good/bad on its own; never drives a status). `role` is `key` (can, on its own,
set its dimension's status to `declining` — SCORING.md §5.3) or `supporting` (contributes evidence but cannot
veto). Every dimension is capped at 1–3 `key` metrics by design (SCORING.md §5.3); the full one-line rationale for
each `none`/debatable assignment is in that metric's own section below, not just here.

| id | name | dimension | tier | phase | window | direction_of_good | role |
|---|---|---|---|---|---|---|---|
| `active_contributors_monthly` | Active Contributors | contributor sustainability | established | 1 | monthly | higher | supporting |
| `new_contributors_monthly` | New Contributors | contributor sustainability | established | 1 | monthly | higher | supporting |
| `first_to_second_conversion_rate` | First → Second Contribution Conversion | contributor sustainability | proxy | 1 | cohort, trailing-12m | higher | supporting |
| `funnel_stage_conversion` | Contributor Funnel Conversion (multi-stage) | contributor sustainability | proxy | 1 | cohort, trailing-12m | higher | supporting |
| `contributor_tenure_survival` | Contributor Tenure (Kaplan–Meier) | contributor sustainability | experimental | 1 | trailing-24m, updated monthly | higher | supporting |
| `contributor_churn_rate` | Contributor Churn Rate | contributor sustainability | proxy | 1 | trailing-12m | lower | supporting |
| `sustained_contributor_count` | Sustained Contributors | contributor sustainability | established | 1 | trailing-12m | higher | **key** |
| `committer_pmc_growth` | Committer/PMC Growth | contributor sustainability | established | 1 | quarterly | higher | supporting |
| `truck_factor` | Truck / Bus Factor (Avelino DOA) | contributor sustainability | experimental | 1 | trailing-12m, updated monthly | higher | **key** |
| `contributor_absence_factor` | Contributor Absence Factor (CHAOSS, commit-count) | contributor sustainability | established | 1 | trailing-12m | higher | supporting |
| `contributor_hhi` | Contributor Concentration (HHI) | contributor sustainability | established | 1 | trailing-12m | lower | **key** |
| `effective_contributor_population` | Effective Contributor Population (1/HHI) | contributor sustainability | established | 1 | trailing-12m | higher | supporting |
| `unique_reviewers_monthly` | Unique Reviewers | reviewer capacity | proxy | 1 | monthly | higher | **key** |
| `reviewer_top_k_share` | Reviewer Top-K Share (k=1,3,5,10) | reviewer capacity | proxy | 1 | trailing-12m | lower | supporting |
| `reviewer_hhi` | Reviewer Concentration (HHI) | reviewer capacity | proxy | 1 | trailing-12m | lower | **key** |
| `effective_reviewer_population` | Effective Reviewer Population (1/HHI) | reviewer capacity | proxy | 1 | trailing-12m | higher | supporting |
| `merge_authority_concentration` | Merge-Authority Concentration | reviewer capacity | established | 1 | trailing-12m | none — see §3 | supporting |
| `review_latency` | Review Latency (time to first review) | reviewer capacity | proxy | 1 | monthly | lower | **key** |
| `review_load_per_reviewer` | Review Load per Reviewer | reviewer capacity | proxy | 1 | monthly | none — see §3 | supporting |
| `contributor_reviewer_ratio` | Contributor:Reviewer Ratio | reviewer capacity | proxy | 1 | monthly | lower | supporting |
| `time_to_first_response_pr` | Time to First Response — GitHub PR | responsiveness | established | 1 | monthly | lower | supporting |
| `time_to_first_response_jira` | Time to First Response — JIRA Issue | responsiveness | established | 1 | monthly | lower | **key** |
| `time_to_first_reply_devlist` | Time to First Reply — dev@ Thread | responsiveness | established | 1 | monthly | lower | **key** |
| `unanswered_thread_rate_devlist` | Unanswered Thread Rate — dev@ | responsiveness | established | 1 | monthly | lower | supporting |
| `change_request_closure_ratio` | Change Request Closure Ratio | responsiveness | established | 1 | monthly | higher | supporting |
| `stale_pr_rate` | Stale PR Rate | responsiveness | established | 1 | monthly | lower | supporting |
| `stale_jira_rate` | Stale JIRA Issue Rate | responsiveness | established | 1 | monthly | lower | **key** |
| `median_resolution_latency_jira` | Median JIRA Resolution Latency | responsiveness | established | 1 | monthly | lower | supporting |
| `elephant_factor` | Elephant Factor | organizational diversity | experimental | 1 | trailing-12m | higher | **key** |
| `organizational_hhi` | Organizational Concentration (HHI) | organizational diversity | established | 1 | trailing-12m | lower | **key** |
| `effective_organizational_population` | Effective Organizational Population (1/HHI) | organizational diversity | established | 1 | trailing-12m | higher | supporting |
| `single_org_share` | Single-Organization Share | organizational diversity | established | 1 | trailing-12m | lower | supporting |
| `unknown_affiliation_rate` | Unknown-Affiliation Rate | organizational diversity | established (of its own denominator) | 1 | trailing-12m | none — see §5 | supporting |
| `release_frequency` | Release Frequency | release cadence | established | 1 | trailing-24m | target-range | **key** |
| `release_regularity` | Release Interval Regularity (CoV) | release cadence | proxy | 1 | trailing-24m | none — see §6 | supporting |
| `time_since_last_release` | Time Since Last Release | release cadence | established | 1 | snapshot | none — see §6 | supporting |
| `dismissive_interaction_rate` | Dismissive Interactions / 1,000 messages | interaction health | classified | 2a | see COMMUNITY-HEALTH.md | lower | supporting |
| `escalation_rate` | Escalation Rate | interaction health | classified | 2a | see COMMUNITY-HEALTH.md | lower | **key** |
| `constructive_resolution_rate` | Constructive Resolution Rate | interaction health | classified | 2a | see COMMUNITY-HEALTH.md | higher | **key** |
| `newcomer_interaction_quality` | Newcomer Interaction Quality | interaction health | classified | 2a | see COMMUNITY-HEALTH.md | higher | supporting |

**Key metrics per dimension (1–3 each, per SCORING.md §5.3's "worst key metric wins" rule):**
- Contributor sustainability: `truck_factor`, `contributor_hhi`, `sustained_contributor_count`
- Reviewer capacity: `reviewer_hhi`, `review_latency`, `unique_reviewers_monthly`
- Responsiveness: `time_to_first_response_jira`, `stale_jira_rate`, `time_to_first_reply_devlist`
- Organizational diversity: `elephant_factor`, `organizational_hhi`
- Release cadence: `release_frequency`
- Interaction health (Phase 2, brief pointer only — see §7): `escalation_rate`, `constructive_resolution_rate`

---

## 2. Contributor Sustainability

### `active_contributors_monthly`
- **Name:** Active Contributors
- **Dimension:** contributor sustainability
- **Tier:** established
- **Phase:** 1
- **Definition:** Count of distinct non-bot, identity-resolved individuals who performed at least one
  `code_commit` in the calendar month. Reported alongside (not merged with) a parallel count that also includes
  `jira_issue_authored` + `jira_comment` + `github_pr` + `mailing_list_post`, so "active in code" and "active in
  any tracked channel" are never conflated (§0.3).
- **Direction of good:** higher. **Role:** supporting (contributor sustainability).
- **Formula:** `A(m) = |{ contributors c : commits(c, m) ≥ 1 }|` for calendar month `m`.
- **Population & exclusions:** §0.5. Excludes bots. Includes identity-unresolved individuals as separate entries.
  No §0.6 sample-size floor applies (issue #27, `definition_version` 1.1): reports its value for any `n`, including
  0, always with `flag = 'ok'`.
- **Window:** monthly, also rolled up trailing-12m as a smoothing view.
- **Required data / source:** git log (author email, timestamp, commit hash) for `apache/cassandra`.
- **Strengths:** Simple, fully deterministic, matches CHAOSS "Contributors" intent (any qualifying activity in a
  window).
- **Weaknesses / gaming risk:** Trivially inflated by no-op commits (whitespace, formatting bots); mitigated by
  requiring a non-empty diff. Does not distinguish a one-line typo fix from a major patch — see `sustained_contributor_count`
  for a magnitude-aware companion.
- **CHAOSS equivalent:** [Contributors](https://chaoss.community/kb/metric-contributors/).

### `new_contributors_monthly`
- **Name:** New Contributors
- **Dimension:** contributor sustainability
- **Tier:** established
- **Phase:** 1
- **Definition:** Count of contributors whose **first-ever** `code_commit` to `apache/cassandra` (across full
  project history, not just the window) falls within calendar month `m`.
- **Direction of good:** higher. **Role:** supporting (contributor sustainability).
- **Formula:** `N(m) = |{ c : min(commit_date(c)) ∈ m }|`.
- **Population & exclusions:** §0.5. Requires full-history first-seen date, which requires the raw cache to hold
  complete git history, not just a rolling window (see D3 — full raw cache, recomputed each run). No §0.6
  sample-size floor applies (issue #27, `definition_version` 1.1): reports its value for any `n`, including 0,
  always with `flag = 'ok'` — a month with 3 (or 0) new contributors is real, meaningful onboarding/attrition
  signal, not a value to suppress.
- **Window:** monthly.
- **Required data / source:** git log, full history.
- **Strengths:** Direct CHAOSS analog; simple onboarding-funnel entry point.
- **Weaknesses / gaming risk:** Sensitive to identity resolution — a contributor who switches git email without a
  confirmed identity link will appear as "new" twice. This is a known, disclosed limitation, not silently patched
  over (§0.5).
- **CHAOSS equivalent:** [New Contributors](https://www.chaoss.community/kb/metric-new-contributors/).

### `first_to_second_conversion_rate`
- **Name:** First → Second Contribution Conversion
- **Dimension:** contributor sustainability
- **Tier:** proxy
- **Phase:** 1
- **Definition:** Of contributors whose first commit landed in cohort month `m`, the share who land a second
  commit (to any file, any time) within a fixed follow-up window (default 180 days) after the first.
- **Direction of good:** higher. **Role:** supporting (contributor sustainability).
- **Formula:** `R2(m) = |{ c ∈ cohort(m) : ∃ second commit within 180d of first }| / |cohort(m)|`.
- **Population & exclusions:** §0.5. Cohort floor: §0.6 (5 minimum; cohorts below 30 are shown but flagged
  low-confidence per the funnel-wide minimum in §0.6).
- **Window:** cohort-based, evaluated only once the 180-day follow-up window has fully elapsed (mirrors D5's
  "completed periods only" rule for status).
- **Required data / source:** git log.
- **Strengths:** This is the CHAOSS "Conversion Rate" idea (first-timer → second-timer) applied with a fixed
  follow-up window instead of an open-ended one, which keeps it computable without waiting indefinitely.
- **Weaknesses / gaming risk:** The 180-day window is an arbitrary but disclosed choice; changing it is a
  metric-definition version bump (D3) with a full-history recompute. Says nothing about *why* someone didn't
  return.
- **CHAOSS equivalent:** [Conversion Rate](https://chaoss.community/kb/metric-conversion-rate/).

### `funnel_stage_conversion`
- **Name:** Contributor Funnel Conversion
- **Dimension:** contributor sustainability
- **Tier:** proxy
- **Phase:** 1
- **Definition:** Cohort-based conversion through the six stages named in the research brief: first contribution →
  second contribution → 5th contribution → sustained contributor (see `sustained_contributor_count` for the
  exact threshold) → committer → PMC member. For a monthly cohort of first-time contributors, report the fraction
  reaching each later stage within a fixed observation horizon (default 24 months, chosen to give Cassandra's
  slower-moving governance stages room to occur), plus the fraction still "in flight" (not yet at the horizon) —
  shown separately from the fraction that reached vs. did not reach a stage, never merged into a single ratio.
- **Direction of good:** higher. **Role:** supporting (contributor sustainability).
- **Formula:** For stage `k` and cohort month `m`: `C_k(m) = |{ c ∈ cohort(m) : reached stage k within horizon }| / |cohort(m)|`,
  reported as a funnel (stage-over-stage ratios) and as cohort-over-cohort-absolute (stage k reached / cohort
  size) side by side.
- **Population & exclusions:** §0.5. Committer/PMC stages sourced from the ASF roster (`committee-info.json` via
  the Whimsy public API), joined to git/JIRA identity by the same confirmed-identity rule as everything else —
  an ASF `availid` is not auto-matched to a git email without a confirmed link.
- **Window:** cohort, trailing-24m horizon, updated monthly; only cohorts old enough to have completed the horizon
  get a "final" reading — younger cohorts show partial/in-flight numbers explicitly labeled as such.
- **Required data / source:** git log; ASF JIRA REST API (issue authorship/comments); ASF Whimsy roster
  (`https://whimsy.apache.org/public/committee-info.json`).
- **Strengths:** Makes the funnel in the research brief concrete and reproducible; committer/PMC stages are
  ground-truth (ASF roster), not inferred.
- **Weaknesses / gaming risk:** Long horizon means recent cohorts are uninformative for years; committer/PMC
  promotion is a governance decision, not purely a function of contribution volume, so low conversion at that
  stage does not necessarily mean unhealthy — this metric measures the funnel, not causes.
- **CHAOSS equivalent:** No single CHAOSS metric covers the full funnel; composed from [Conversion Rate](https://chaoss.community/kb/metric-conversion-rate/)
  and [New Contributors](https://www.chaoss.community/kb/metric-new-contributors/) concepts.

### `contributor_tenure_survival`
- **Name:** Contributor Tenure (Kaplan–Meier survival)
- **Dimension:** contributor sustainability
- **Tier:** experimental
- **Phase:** 1
- **Definition:** Time-to-event model of contributor "churn," using the Kaplan–Meier estimator, following the
  method used in OSS-retention survival studies (Lin, Robles & Serebrenik 2017; Foucault et al. 2015 — see
  citations below).
  - **Event (churn):** a contributor is treated as having churned at time `t` if they have made **no** qualifying
    activity (any of §0.3's activity types) for a fixed inactivity window of **12 months**, dated to their last
    observed activity. This mirrors the inactivity-window operationalization used in the survival-analysis OSS
    literature, where an explicit "leave" event is rarely observable and a dwell-time cutoff is used instead.
  - **Censoring:** a contributor who has been active within the last 12 months as of the current run is
    **right-censored** at "now" — they have not (yet) churned, and their true departure time is unknown. A
    contributor whose only activity predates the raw cache's coverage start is **left-truncated**, not treated as
    a tenure of zero.
  - **Time origin:** each contributor's clock starts at their first qualifying activity (`new_contributors_monthly`'s
    trigger event).
- **Direction of good:** higher (a higher survival probability at a given tenure is the healthy reading). **Role:**
  supporting (contributor sustainability) — kept out of the 3 `key` slots because it is `experimental`, frequently
  gated to `insufficient data` by its own §0.6 cohort floor, and would make the dimension's status depend on a
  metric that is often unavailable; `truck_factor`, `contributor_hhi`, and `sustained_contributor_count` are the
  dimension's key metrics instead.
- **Formula:** Standard Kaplan–Meier estimator: `Ŝ(t) = Π_{t_i ≤ t} (1 − d_i / n_i)` where `t_i` are observed
  event times, `d_i` the number of churn events at `t_i`, and `n_i` the number of contributors still at risk
  (active and not yet censored) just before `t_i`.
- **Population & exclusions:** §0.5. Minimum cohort per §0.6 (30) to render a curve; smaller populations (e.g., a
  single quarter's cohort) are shown as a simple "still active at N months" count table instead.
- **Window:** trailing-24m cohort intake, survival curve recomputed monthly as more follow-up time accrues.
- **Required data / source:** git log, JIRA, GitHub PR, mailing-list metadata (activity timestamps only).
- **Strengths:** Explicitly handles the fact that we never observe "why someone left" and correctly treats
  currently-active people as censored rather than as "survived forever" or dropping them, which naive
  retention-rate metrics get wrong.
- **Weaknesses / gaming risk:** The 12-month inactivity threshold defining "churn" is a modeling choice, not an
  observed fact — a contributor who returns after 13 months is counted as churned-then-new, which the literature
  itself flags as a limitation of dwell-time-based churn definitions. Results are sensitive to this threshold;
  changing it triggers a version bump and full recompute (D3). This is `experimental` specifically because it has
  not yet been validated against Cassandra's actual contributor behavior (e.g., whether 12 months is the right
  cutoff for a project with an unusually long 4.0 release freeze — see `SCORING.md` §6 on structural breaks).
- **CHAOSS equivalent:** none directly; grounded in academic survival-analysis literature rather than CHAOSS:
  - Lin, B., Robles, G., & Serebrenik, A. (2017). *Developer Turnover in Global, Industrial Open Source Projects:
    Insights from Applying Survival Analysis.* IEEE 12th International Conference on Global Software Engineering
    (ICGSE), 66–75. https://doi.org/10.1109/ICGSE.2017.11
  - Foucault, M., Palyart, M., Blanc, X., Murphy, G. C., & Falleri, J.-R. (2015). *Impact of Developer Turnover on
    Quality in Open-Source Software.* ESEC/FSE 2015, 829–841. https://doi.org/10.1145/2786805.2786870

### `contributor_churn_rate`
- **Name:** Contributor Churn Rate
- **Dimension:** contributor sustainability
- **Tier:** proxy
- **Phase:** 1
- **Definition:** Simple, non-survival companion to `contributor_tenure_survival` for a quick monthly read: the
  share of contributors who were active in the trailing-12m window ending 12 months ago and have had **no**
  qualifying activity since. Uses the same churn/inactivity definition as `contributor_tenure_survival` §Event,
  so the two metrics stay consistent.
- **Direction of good:** lower. **Role:** supporting (contributor sustainability).
- **Formula:** `Churn(m) = |{ c : last_active(c) ∈ [m-24, m-12) }| / |{ c : active in [m-24, m-12) }|`.
- **Population & exclusions:** §0.5, §0.6.
- **Window:** trailing-12m, evaluated on completed months only (D5).
- **Required data / source:** same as `contributor_tenure_survival`.
- **Strengths:** Cheap, immediately legible complement to the KM curve; good for a dashboard tile.
- **Weaknesses / gaming risk:** Point-in-time churn rate does not distinguish "many short-tenure people churned"
  from "a few long-tenure core people churned" — severity is invisible here; that is what `truck_factor` and
  `contributor_hhi` are for. Never combine this rate with concentration metrics into one number.
- **CHAOSS equivalent:** none directly (adjacent to CHAOSS Contributors/Retention discussion, no fixed CHAOSS formula).

### `sustained_contributor_count`
- **Name:** Sustained Contributors
- **Dimension:** contributor sustainability
- **Tier:** established
- **Phase:** 1
- **Definition:** Count of contributors with **5 or more** `code_commit`s in the trailing 12 months — the "5
  contributions" funnel stage from the research brief, operationalized with an explicit, disclosed threshold.
- **Direction of good:** higher. **Role:** **key** (contributor sustainability) — one of this dimension's 1–3 key
  metrics (SCORING.md §5.3): it is the most direct, fully deterministic read of the funnel actually producing
  repeat contributors, so a sustained decline here can, on its own, mark contributor sustainability `declining`.
- **Formula:** `S(m) = |{ c : commits(c, trailing12m ending m) ≥ 5 }|`.
- **Population & exclusions:** §0.5.
- **Window:** trailing-12m, updated monthly.
- **Required data / source:** git log.
- **Strengths:** Deterministic, threshold is explicit and versioned.
- **Weaknesses / gaming risk:** Five trivial commits count the same as five substantial patches; volume is not
  quality. The threshold (5) is arbitrary but disclosed; changing it is a version bump.
- **CHAOSS equivalent:** adjacent to [Contributors](https://chaoss.community/kb/metric-contributors/) with a
  custom activity-count filter; CHAOSS does not standardize a "5 contributions" threshold.

### `committer_pmc_growth`
- **Name:** Committer / PMC Growth
- **Dimension:** contributor sustainability
- **Tier:** established
- **Phase:** 1
- **Definition:** Net change in ASF committer count and PMC member count for the Cassandra project committee,
  quarter over quarter, sourced directly from the ASF roster (ground truth, not inferred).
- **Direction of good:** higher. **Role:** supporting (contributor sustainability) — ground-truth but too
  low-frequency/small-n (§0.6) to safely veto a dimension's status on its own.
- **Formula:** `ΔCommitters(q) = |committers(q)| − |committers(q−1)|`; same for PMC.
- **Population & exclusions:** none beyond roster scope; this is the one metric in this catalog with no bot/identity
  ambiguity because ASF roster entries are already resolved identities.
- **Window:** quarterly (roster changes are infrequent; monthly would be mostly zeros).
- **Required data / source:** ASF Whimsy public roster API — `committee-info.json` (current) and
  `committee-retired.json` (https://whimsy.apache.org/public/).
- **Strengths:** Ground truth, no identity-resolution risk, directly reflects governance decisions.
- **Weaknesses / gaming risk:** Low frequency, small numbers — a single retirement can swing the metric; needs the
  §0.6 small-n handling and should never be the sole signal for a dimension status.
- **CHAOSS equivalent:** none standardized; ASF-specific ground-truth measurement.

### `truck_factor`
- **Name:** Truck / Bus Factor (Avelino Degree-of-Authorship algorithm)
- **Dimension:** contributor sustainability
- **Tier:** experimental
- **Phase:** 1
- **Definition:** The minimum number of contributors whose simultaneous departure would leave more than 50% of
  the project's files with no remaining "author" capable of maintaining them, computed using the Degree-of-Authorship
  (DOA) algorithm from Avelino et al. (2016).
  - **DOA per (file, developer) pair** combines: whether the developer is the file's first author, number of
    commits the developer made to the file, and number of lines added/removed by the developer to the file
    (weighted per the original paper's regression-fit coefficients).
  - **Author** = developer whose DOA for a file exceeds a fixed threshold (paper default: `DOA > threshold`, with
    developers with the highest DOA per file being that file's "key" authors).
  - **Algorithm:** iteratively remove the single developer who is the top author of the most files from the
    "available" pool, adding them to the "key developer" list, until more than 50% of files have no remaining
    author. Truck factor = size of the removed set at that point.
- **Direction of good:** higher. **Role:** **key** (contributor sustainability) — one of this dimension's 1–3 key
  metrics (SCORING.md §5.3): file-authorship concentration is exactly the resilience risk the research brief
  flags first, so a sustained decline here can, on its own, mark the dimension `declining` even if headcount
  metrics look fine.
- **Formula:** as specified in Avelino et al. (2016), §3; see citation below for the full DOA regression formula
  (this project reuses the published coefficients rather than re-deriving them from scratch — re-fitting would
  itself need validation this project has not done).
- **Population & exclusions:** §0.5. Computed at a per-file granularity across the full repository at the time of
  the run — a snapshot metric, not a windowed rate, though it is recomputed monthly and its trend over time is
  the useful signal (D2.2 — trends over snapshots).
- **Window:** point-in-time, recomputed monthly to build a trend.
- **Required data / source:** git log with per-file, per-commit line-level author attribution (`git log --no-merges
  --numstat` or equivalent — merge commits excluded per §0.3, since Cassandra's forward-merges across release
  branches would otherwise attribute the same underlying patch's lines to whoever ran the merge).
- **Strengths:** Most empirically validated truck-factor estimator in the literature — Avelino et al. validated it
  against 114 developer surveys across 133 GitHub projects and found it one of the more accurate estimators in a
  later comparative study (Ferreira, Avelino, et al., *Algorithms for estimating truck factors: a comparative
  study*, Software Quality Journal, 2019 — https://doi.org/10.1007/s11219-019-09457-2).
- **Weaknesses / gaming risk:** (a) It is file-authorship concentration, not "who could review/merge/design" —
  Cassandra's real bus-factor risk may live more in review/JIRA/mailing-list institutional knowledge than in raw
  git line authorship. (b) The algorithm was validated on GitHub-centric projects; it has not been validated
  against an ASF project with Cassandra's patch-by-email/JIRA-centric history, especially older history that
  predates GitHub mirroring. (c) The 50%-of-files threshold is itself a convention, not a law. This project marks
  the metric `experimental` for exactly these reasons rather than presenting it as ground truth. See also the
  faster/approximate heuristics survey: *Fast and Accurate Heuristics for Bus-Factor Estimation*
  (https://arxiv.org/html/2508.09828) and *Bus Factor In Practice* (https://arxiv.org/pdf/2202.01523) for further
  limitations discussion this project should revisit before promoting the tier.
- **CHAOSS equivalent:** [Contributor Absence Factor / Bus Factor](https://chaoss.community/kb/metric-bus-factor/)
  — CHAOSS's own formula (see `contributor_absence_factor` below) is a simpler commit-count threshold, not the DOA
  algorithm; the two are reported side by side because they answer related but different questions (commit-share
  concentration vs. file-maintainability concentration).
  - Avelino, G., Passos, L., Hora, A., & Valente, M. T. (2016). *A Novel Approach for Estimating Truck Factors.*
    24th IEEE/ACM International Conference on Program Comprehension (ICPC). https://arxiv.org/abs/1604.06766

### `contributor_absence_factor`
- **Name:** Contributor Absence Factor (CHAOSS formula)
- **Dimension:** contributor sustainability
- **Tier:** established
- **Phase:** 1
- **Definition:** The CHAOSS "Bus Factor" formula, distinct from and simpler than `truck_factor`: the smallest
  number of contributors, ranked by commit count, whose combined commits reach 50% of total commits in the
  window.
- **Direction of good:** higher. **Role:** supporting (contributor sustainability) — deliberately not `key`: it
  answers a question closely related to `truck_factor`'s (which is `key`) using a cruder commit-count-only
  algorithm; keeping both as key metrics would let the same underlying concentration signal veto the dimension
  twice.
- **Formula:** Sort contributors by commit count descending; cumulatively sum until the running total ≥ 50% of
  total commits; the count of contributors used is the metric value.
- **Population & exclusions:** §0.5, §0.6.
- **Window:** trailing-12m.
- **Required data / source:** git log.
- **Strengths:** Simple, transparent, matches the published CHAOSS definition exactly — easy for the community to
  verify by hand.
- **Weaknesses / gaming risk:** Ignores which *files* those commits touch, so it can overstate resilience (many
  commits from few people, but spread thin across the codebase) or understate it (concentrated on a redundant
  module) relative to `truck_factor`. This is exactly why both are reported, not merged.
- **CHAOSS equivalent:** [Contributor Absence Factor](https://chaoss.community/kb/metric-bus-factor/) — same
  formula, reproduced above.

### `contributor_hhi` / `effective_contributor_population`
- **Name:** Contributor Concentration (HHI) / Effective Contributor Population
- **Dimension:** contributor sustainability
- **Tier:** established
- **Phase:** 1
- **Definition:** The Herfindahl–Hirschman Index applied to commit-share distribution across contributors, and its
  reciprocal (the "effective number" of equally-sized contributors that would produce the same concentration).
  HHI is a standard economics concentration measure (market-share sum-of-squares); its application here treats
  each contributor's share of commits like a firm's market share.
- **Direction of good:** `contributor_hhi` is **lower**; `effective_contributor_population` is **higher** (its
  reciprocal, so the two always move in mirror-image directions — this is expected, not a contradiction).
  **Role:** `contributor_hhi` is **key** (contributor sustainability) — the dimension's third key metric,
  chosen over its reciprocal because a concentration index reads more directly as a risk signal than an
  "effective population" count does; `effective_contributor_population` is supporting, reported for
  interpretability, not as an independent veto (combining both as key would double-count one signal).
- **Formula:** `HHI = Σ s_i²` where `s_i` is contributor `i`'s share of total commits in the window (`s_i ∈ [0,1]`,
  so `HHI ∈ (0, 1]`; some conventions use percentage shares scaled 0–10,000 — this project uses the 0–1 fractional
  form and states so on every chart to avoid unit confusion). `EffectivePopulation = 1 / HHI`.
- **Population & exclusions:** §0.5, §0.6 (floor 5 distinct contributors).
- **Window:** trailing-12m.
- **Required data / source:** git log.
- **Strengths:** Continuous (unlike the discrete bus-factor "count of people"), standard, well-understood outside
  software too (used by US DOJ/FTC for market concentration — https://www.justice.gov/atr/herfindahl-hirschman-index),
  and directly interpretable via its reciprocal as "how many equal-sized contributors this behaves like."
- **Weaknesses / gaming risk:** Like all concentration indices, sensitive to how the population is scoped (repo
  vs. subsystem vs. whole project); says nothing about *who* is concentrated (see `elephant_factor` for the
  organizational analog) or about file-level risk (see `truck_factor`).
- **CHAOSS equivalent:** none with this exact name; conceptually related to
  [Contributor Absence Factor](https://chaoss.community/kb/metric-bus-factor/) but HHI itself is general-purpose
  economics literature, not CHAOSS-specific: Rhoades, S. A. (1993). *The Herfindahl-Hirschman Index.* Federal
  Reserve Bulletin. https://www.federalreserve.gov/pubs/bulletin/1993/pdf/index2.pdf (see also the general
  discussion at https://www.semanticscholar.org/paper/The-Herfindahl-Hirschman-index-Rhoades/451c89b36509a046fa501c8722338aee56fc37d7).

---

## 3. Reviewer Capacity

All reviewer-capacity metrics for Cassandra rely on **two independent, cross-checked proxy sources** — the git
commit trailer and the ASF JIRA `Reviewers`/`Reviewer` custom fields (§0.4) — reconciled against GitHub PR review
events where those exist. Per §0.4's verified coverage data, **trailing baselines for every metric in this
section start no earlier than 2017**; earlier history is shown for context only, never used to compute a
baseline or an improving/stable/declining status (see `SCORING.md` §6). All are tier `proxy` unless noted.

### `unique_reviewers_monthly`
- **Definition:** Count of distinct non-bot individuals credited as a reviewer — via non-merge commit trailer
  parse, JIRA `Reviewers`/`Reviewer` field, or GitHub review event — on at least one change in the month. A
  reviewer credited by only one of the three sources still counts once; the per-source breakdown is retained for
  audit (D2.3) so a reader can see whether a given month's number rests on trailer data, JIRA data, or both.
- **Direction of good:** higher. **Role:** **key** (reviewer capacity) — one of this dimension's 1–3 key metrics
  (SCORING.md §5.3): headcount is the most direct, hardest-to-game read of whether the reviewer pool itself is
  shrinking.
- **Formula:** `UR(m) = |{ reviewers credited in m across any source }|`.
- **Population & exclusions:** §0.5. Baseline window starts 2017 (§0.4). No §0.6 sample-size floor applies (issue
  #27, `definition_version` 1.1): reports its value for any `n`, including 0, always with `flag = 'ok'`.
- **Window:** monthly.
- **Required data / source:** git commit trailers (non-merge commits only); ASF JIRA REST API
  (`customfield_12313420`, `customfield_10022`); GitHub Reviews API.
- **Strengths:** Direct visibility into review-capacity headcount, separate from contributor headcount; the
  two-source cross-check catches gaps either source alone would miss.
- **Weaknesses / gaming risk:** §0.4's proxy limitations apply in full — neither source captures review effort
  that produced no formal credit (e.g., informal dev@ feedback that never reached the trailer or the JIRA field).
- **CHAOSS equivalent:** adjacent to [Change Request Reviews](https://chaoss.community/kb/metric-change-request-reviews/).

### `reviewer_top_k_share`
- **Definition:** Share of total credited reviews performed by the top 1, top 3, top 5, and top 10 reviewers
  (four numbers reported together, not one).
- **Direction of good:** lower. **Role:** supporting (reviewer capacity) — closely related to `reviewer_hhi`
  (which is `key`); kept supporting so the same concentration signal cannot veto the dimension through two
  metrics at once.
- **Formula:** `TopK(m) = Σ_{i=1..k} reviews_i / Σ_all reviews`, reviewers ranked descending by review count.
- **Population & exclusions:** §0.5, §0.6. Baseline window starts 2017 (§0.4).
- **Window:** trailing-12m.
- **Required data / source:** same as `unique_reviewers_monthly` (git trailer + JIRA reviewer fields + GitHub reviews).
- **Strengths:** Simple, intuitive concentration read, direct request from the research brief.
- **Weaknesses / gaming risk:** Same proxy risk as §0.4; also double-counts a change reviewed by multiple people
  as one "review credit" per reviewer named, which can inflate load appearance for changes with many co-reviewers.
- **CHAOSS equivalent:** none exact; composed from [Change Request Reviews](https://chaoss.community/kb/metric-change-request-reviews/) data.

### `reviewer_hhi` / `effective_reviewer_population`
- **Definition:** HHI and its reciprocal (§ contributor_hhi formula, same math) applied to review-credit shares
  instead of commit shares.
- **Direction of good:** `reviewer_hhi` is **lower**; `effective_reviewer_population` is **higher** (its
  reciprocal). **Role:** `reviewer_hhi` is **key** (reviewer capacity) — the dimension's concentration-risk key
  metric, chosen over its reciprocal for the same reason as `contributor_hhi` above;
  `effective_reviewer_population` is supporting.
- **Formula:** identical to `contributor_hhi`, substituting review-credit share for commit share.
- **Population & exclusions:** §0.5, §0.6. Baseline window starts 2017 (§0.4).
- **Window:** trailing-12m.
- **Required data / source:** same as `unique_reviewers_monthly`.
- **Strengths/weaknesses:** as `contributor_hhi`, plus the §0.4 proxy caveat.
- **CHAOSS equivalent:** none exact.

### `merge_authority_concentration`
- **Definition:** HHI (and top-k share) computed over **who actually merges/commits changes to the mainline**
  (i.e., has commit-bit / merge rights and exercises it), as distinct from who reviews. In Cassandra, committers
  with ASF commit karma are the ground-truth population (roster-sourced); this metric measures how concentrated
  *actual merges* are among that population, not just who is entitled to merge.
- **Direction of good:** **none** — deliberately not assigned, because the direction is genuinely debatable: in a
  meritocratic ASF project, concentration of *actual* merges among a small set of earned committers is expected
  and not obviously unhealthy the way `reviewer_hhi` concentration is (see this metric's own Weaknesses note
  below), so this project shows its trend for context rather than classifying it improving/declining. **Role:**
  supporting (reviewer capacity).
- **Formula:** HHI formula (as above) over counts of non-merge commits per `committer` identity (the git
  `committer` field, distinct from the `author` field — the person who actually pushed the change to the
  canonical branch, which is the closest git-native signal for "exercised merge/commit rights"). Cassandra's
  actual `git merge` commits (forward-merging a fix from e.g. `cassandra-5.0` into `cassandra-6.0` into `trunk`)
  are **excluded from this count**, consistent with §0.3's merge-commit rule — those are a branching-workflow
  artifact and would otherwise make whoever runs the forward-merge look artificially dominant.
- **Population & exclusions:** §0.5; population restricted to individuals with confirmed committer status per the
  ASF roster (ground truth, no identity ambiguity for this specific population, per `committer_pmc_growth`).
- **Window:** trailing-12m.
- **Required data / source:** git log, non-merge commits, `committer` field (distinct from `author` field)
  cross-referenced with ASF roster.
- **Strengths:** Distinguishes "who can theoretically merge" (roster) from "who actually does" (activity) — a
  named-but-inactive committer doesn't hide a real concentration problem.
- **Weaknesses / gaming risk:** A small number of very active committers is *expected and healthy* in a
  meritocratic ASF project (committer status is earned); low values here should not be read the same way as low
  values in `reviewer_hhi` — this is flagged explicitly in the metric's own documentation, not left to the reader
  to infer, per D2's transparency principle.
- **CHAOSS equivalent:** established as a concept (concentration of merge rights) but no single named CHAOSS metric.

### `review_latency`
- **Definition:** Time from when a change is ready for review (PR opened, or patch attached to a JIRA issue and
  moved to "Patch Available"/equivalent status) to first review activity (first GitHub review event, or first
  JIRA comment from someone other than the author on that issue).
- **Direction of good:** lower. **Role:** **key** (reviewer capacity) — one of this dimension's 1–3 key metrics
  (SCORING.md §5.3): rising review latency is the most direct operational symptom of reviewer-capacity strain.
- **Formula:** Median and P90 of `t_first_review − t_ready` over changes closed in the window.
- **Population & exclusions:** §0.5, §0.6 (floor 5 closed changes; below that, show raw list). Baseline window
  starts 2017 (§0.4) since "ready for review" state and reviewer-field usage are sparse before then.
- **Window:** monthly.
- **Required data / source:** GitHub PR/Reviews API; ASF JIRA REST API (status transition history, comments, and
  the `Reviewers`/`Reviewer` custom fields as a same-issue cross-check per §0.4).
- **Strengths:** Directly actionable — a rising median is an early operational warning.
- **Weaknesses / gaming risk:** A one-line "looks fine, +1" counts identically to a substantive review; latency
  says nothing about review depth.
- **CHAOSS equivalent:** [Time to First Response](https://www.chaoss.community/kb/metric-time-to-first-response/)
  applied specifically to the reviewer-capacity dimension rather than general responsiveness (see
  `time_to_first_response_pr`/`time_to_first_response_jira` in §4 for the general-responsiveness version of the
  same underlying data — reported once, used in two dimension contexts, never double-weighted into a combined
  score, per `SCORING.md`).

### `review_load_per_reviewer`
- **Definition:** Distribution (median, P90) of review credits per active reviewer per month — the flip side of
  concentration: how much work the median/busiest reviewer is actually carrying.
- **Direction of good:** **none** — deliberately not assigned: rising load can mean healthy growth in review
  throughput (more work getting reviewed) or overload risk (too few reviewers absorbing more), and which one it
  is depends on the concurrent reading of `reviewer_hhi`/`contributor_reviewer_ratio`, not on this metric alone.
  **Role:** supporting (reviewer capacity).
- **Formula:** For reviewers active in month `m`, distribution of `reviews_credited(reviewer, m)`.
- **Population & exclusions:** §0.5, §0.6. Baseline window starts 2017 (§0.4).
- **Window:** monthly.
- **Required data / source:** same as `unique_reviewers_monthly`.
- **Strengths:** Complements concentration ratios with an absolute-load read — a project can have low
  concentration but still be overloading its whole reviewer pool.
- **Weaknesses / gaming risk:** Same §0.4 proxy caveats.
- **CHAOSS equivalent:** none exact; adjacent to [Change Request Reviews](https://chaoss.community/kb/metric-change-request-reviews/).

### `contributor_reviewer_ratio`
- **Definition:** Ratio of monthly active contributors (`active_contributors_monthly`, code-commit definition) to
  monthly unique reviewers (`unique_reviewers_monthly`) — the "how many people are producing patches per person
  capable of reviewing them" signal called out explicitly in the research brief.
- **Direction of good:** lower. **Role:** supporting (reviewer capacity) — a useful early-warning composite, but
  deliberately not `key` since it is derived entirely from two metrics already in the catalog
  (`active_contributors_monthly`, supporting; `unique_reviewers_monthly`, key), so giving it its own veto power
  would let the same underlying signal count twice.
- **Formula:** `Ratio(m) = active_contributors_monthly(m) / unique_reviewers_monthly(m)`.
- **Population & exclusions:** requires both component metrics to individually clear their minimum sample size;
  otherwise `insufficient data`. Because `unique_reviewers_monthly` uses a 2017+ baseline (§0.4) but
  `active_contributors_monthly` does not, this ratio's own trend baseline also starts 2017 to keep both sides
  comparable over the same window.
- **Window:** monthly.
- **Required data / source:** derived from the two component metrics above.
- **Strengths:** Cheap, legible early-warning signal for reviewer-capacity strain.
- **Weaknesses / gaming risk:** A ratio hides which side moved — always shown with its two components alongside
  it (D2.3 auditability), never as a bare number.
- **CHAOSS equivalent:** none exact; composed metric.

---

## 4. Responsiveness

### `time_to_first_response_pr` / `time_to_first_response_jira`
- **Definition:** Time between when an activity (GitHub PR, JIRA issue) is opened and the first **human** response
  (comment, review, or status change by someone other than the author). Bot responses (CI, JIRA-GitHub bridge,
  auto-labelers) are excluded from qualifying as "first response."
- **Direction of good:** lower, for both. **Role:** `time_to_first_response_pr` is supporting (responsiveness);
  `time_to_first_response_jira` is **key** (responsiveness) — one of this dimension's 1–3 key metrics
  (SCORING.md §5.3), chosen over the PR variant because JIRA, not GitHub PRs, is where most Cassandra review
  discussion actually happens (DECISIONS.md, "Cassandra-specific facts"; `docs/spec/data-probe.md` shows 21,483
  JIRA issues against 5,207 GitHub PRs).
- **Formula:** Median and P90 of `t_first_human_response − t_opened` over items opened (or, alternatively,
  closed) in the window — this project reports both the "opened in window" and "closed in window" framings side
  by side, since they answer slightly different questions (current backlog behavior vs. historical resolution
  behavior) and CHAOSS's own guidance notes multiple valid framings.
- **Population & exclusions:** §0.5, §0.6.
- **Window:** monthly.
- **Required data / source:** GitHub Issues/PR API (timestamps, comment authorship); ASF JIRA REST API (comment
  and status-transition history).
- **Strengths:** Core CHAOSS-established responsiveness metric; strong face validity as a contributor-experience
  signal.
- **Weaknesses / gaming risk:** A drive-by "thanks, will look later" counts as a response even if substantive
  engagement takes weeks longer — this metric measures acknowledgment speed, not resolution speed
  (`median_resolution_latency_jira` covers the latter).
- **CHAOSS equivalent:** [Time to First Response](https://www.chaoss.community/kb/metric-time-to-first-response/);
  see also the [Responsiveness practitioner guide](https://www.chaoss.community/practitioner-guide-responsiveness/).

### `time_to_first_reply_devlist`
- **Definition:** Time between the first message of a dev@ thread and the first reply from a different sender.
- **Direction of good:** lower. **Role:** **key** (responsiveness) — one of this dimension's 1–3 key metrics
  (SCORING.md §5.3): design discussion and CEPs happen on dev@ (DECISIONS.md), so this is the dimension's
  mailing-list-channel representative, distinct in kind from the two JIRA/PR-channel supporting-or-key metrics.
- **Formula:** as above, restricted to dev@ (and optionally user@, reported separately) threads.
- **Population & exclusions:** §0.5, §0.6. Metadata-only per D1/Phase-1 scope — sender, timestamp, thread
  structure; message bodies are not collected in Phase 1.
- **Window:** monthly.
- **Required data / source:** Apache Pony Mail JSON API for `dev@cassandra.apache.org` (metadata fields only).
- **Strengths:** Mailing-list-native analog of PR/JIRA responsiveness; important because design discussion and
  CEPs live on dev@ (DECISIONS.md).
- **Weaknesses / gaming risk:** A reply is not necessarily a substantive reply; "metadata only" means this project
  cannot (in Phase 1) distinguish a one-word reply from a thoughtful one — that distinction, if pursued at all,
  is Phase 2a classification territory, gated as in D1/D7.
- **CHAOSS equivalent:** [Time to First Response](https://www.chaoss.community/kb/metric-time-to-first-response/) applied to email.

### `unanswered_thread_rate_devlist`
- **Definition:** Share of dev@ threads started in the window that receive **zero** replies within a fixed
  follow-up window (default 30 days).
- **Direction of good:** lower. **Role:** supporting (responsiveness) — closely related to
  `time_to_first_reply_devlist` (which is `key`); kept supporting to avoid double-counting the same mailing-list
  responsiveness signal.
- **Formula:** `UnansweredRate(m) = |{ threads started in m with 0 replies within 30d }| / |{ threads started in m }|`.
- **Population & exclusions:** §0.5, §0.6; excludes threads that are themselves auto-generated (e.g., automated
  JIRA-to-list notifications, if such a list is in scope — Cassandra's dev@ carries some automated traffic that
  should be filtered by a maintained sender allow/deny list, auditable like the bot list).
- **Window:** monthly, evaluated only once the 30-day follow-up has elapsed (D5-style completed-period rule).
- **Required data / source:** Pony Mail JSON API metadata.
- **Strengths:** Direct, interpretable, complements latency (a thread can get a fast reply and still not really
  be "answered" — this project does not claim to detect that in Phase 1, only zero-reply abandonment).
- **Weaknesses / gaming risk:** Any reply counts, including an off-topic one; "answered" is a metadata proxy for
  "received a reply," not "received a satisfying resolution."
- **CHAOSS equivalent:** adjacent to CHAOSS responsiveness metrics; no exact named equivalent for "unanswered
  thread rate" specifically.

### `change_request_closure_ratio`
- **Definition:** Ratio of change requests (PRs, and JIRA issues of type bug/improvement in Cassandra's tracker)
  closed to those opened in the window — is the project keeping up with incoming change requests, per CHAOSS's
  own framing.
- **Direction of good:** higher. **Role:** supporting (responsiveness) — informative but excluded from `key`
  because, as its own Weaknesses note says, a ratio above 1 can reflect a one-time backlog cleanup rather than a
  sustained change, making it a noisier veto candidate than `stale_jira_rate`.
- **Formula:** `ClosureRatio(m) = closed(m) / opened(m)`.
- **Population & exclusions:** §0.5, §0.6; "closed" includes both merged/resolved and explicitly declined/won't-fix
  — CHAOSS's guidance explicitly credits maintainers for closing out things that won't be merged, not only merges.
- **Window:** monthly.
- **Required data / source:** GitHub PR API; ASF JIRA REST API.
- **Strengths:** Established CHAOSS metric with clear guidance on including declines as legitimate closure activity.
- **Weaknesses / gaming risk:** A ratio > 1 in one month can reflect closing an old backlog, not current health;
  always shown with the raw opened/closed counts (D2.3).
- **CHAOSS equivalent:** [Change Request Closure Ratio](https://chaoss.community/kb/metric-change-request-closure-ratio/).

### `stale_pr_rate` / `stale_jira_rate`
- **Definition:** Share of open PRs/JIRA issues with no human activity for more than a fixed threshold (default
  90 days), among all currently open items.
- **Direction of good:** lower, for both. **Role:** `stale_pr_rate` is supporting (responsiveness);
  `stale_jira_rate` is **key** (responsiveness) — one of this dimension's 1–3 key metrics (SCORING.md §5.3),
  chosen over the PR variant for the same JIRA-is-primary-venue reason as `time_to_first_response_jira` above.
- **Formula:** `StaleRate = |{ open items with no activity in last 90d }| / |{ open items }|`.
- **Population & exclusions:** §0.5, §0.6.
- **Window:** monthly snapshot.
- **Required data / source:** GitHub PR API; ASF JIRA REST API.
- **Strengths:** Simple, direct backlog-health signal frequently cited in the research brief's deterministic list.
- **Weaknesses / gaming risk:** Threshold (90 days) is a disclosed convention, not a universal truth; a project
  that intentionally keeps long-lived "won't fix soon but won't close" issues open will read as worse than it is
  — again, this is why status is judged against the project's *own* baseline (D2.2), not an absolute threshold.
- **CHAOSS equivalent:** adjacent to [Change Requests](https://chaoss.community/kb/metric-change-requests/) /
  [Change Requests Declined](https://chaoss.community/kb/metric-change-requests-declined/).

### `median_resolution_latency_jira`
- **Definition:** Median (and P90) time from JIRA issue creation to resolution, for issues resolved in the window.
- **Direction of good:** lower. **Role:** supporting (responsiveness) — end-to-end resolution speed is valuable
  context but overlaps with `time_to_first_response_jira`/`stale_jira_rate` (both `key`); kept supporting to avoid
  a third JIRA-channel metric holding veto power.
- **Formula:** median/P90 of `t_resolved − t_created`.
- **Population & exclusions:** §0.5, §0.6.
- **Window:** monthly.
- **Required data / source:** ASF JIRA REST API.
- **Strengths:** Captures end-to-end resolution speed, complementary to first-response latency.
- **Weaknesses / gaming risk:** Skewed heavily by a few very old issues resolved in a batch cleanup; median (not
  mean) mitigates but does not eliminate this — reported with P90 alongside to show tail behavior.
- **CHAOSS equivalent:** adjacent to [Change Request Closure Ratio](https://chaoss.community/kb/metric-change-request-closure-ratio/)
  and general responsiveness guidance.

---

## 5. Organizational Diversity

### `elephant_factor`
- **Definition:** The minimum number of **organizations** whose employees' combined commits reach 50% of total
  commits in the window — same algorithm as `contributor_absence_factor`, applied to organizational affiliation
  instead of individual identity.
- **Direction of good:** higher (more organizations required to reach the 50% threshold = less concentrated =
  healthier — the mirror image of `organizational_hhi`/`single_org_share`, which is exactly why every metric's
  direction is assigned individually rather than inferred from its dimension). **Role:** **key** (organizational
  diversity) — one of this dimension's 1–3 key metrics (SCORING.md §5.3), the direct CNCF/CHAOSS-style
  corporate-capture-risk read the research brief calls out first.
- **Formula:** identical structure to `contributor_absence_factor`, substituting organization for contributor:
  sort organizations by commit share descending, cumulatively sum to the 50% threshold, count organizations used.
  `unknown` affiliation is treated as its own "organization" bucket for this purpose so it cannot silently vanish
  from the denominator, but the resulting count is annotated to show whether `unknown` was one of the entities
  needed to reach the threshold (which would make the number misleading either direction).
- **Population & exclusions:** §0.5, §0.6 (floor 5 distinct known organizations before a status is computed; if
  `unknown` dominates the population, the metric renders `insufficient data` for status purposes even if a raw
  number can be shown, per D6 — unknown stays unknown, never guessed into a bucket).
- **Window:** trailing-12m.
- **Required data / source:** git log author identity, joined to `affiliations.yaml` (D6) — curated file seeded
  from email domains and GitHub profile `company` fields, with dated ranges.
- **Strengths:** Direct CNCF/CHAOSS-style measure of corporate-capture risk; intuitive.
- **Weaknesses / gaming risk:** Time-window sensitivity — CHAOSS's own documentation flags that elephant factor
  "over the life of a product may misrepresent the current level of organizational diversity"
  (https://github.com/chaoss/wg-risk/blob/main/focus-areas/business-risk/elephant-factor.md), which is exactly
  why this project uses a rolling trailing-12m window rather than all-time. Marked `experimental` (rather than
  `established`) specifically because Cassandra's affiliation data is not yet built and validated (D6 — a
  reviewed `affiliations.yaml` that does not yet exist); the algorithm itself is simple and well specified, but
  the *input data quality* for this project is unproven, so the tier reflects that composite uncertainty, not the
  algorithm.
- **CHAOSS equivalent:** [Elephant Factor](https://www.chaoss.community/kb/metric-elephant-factor/),
  [wg-risk elephant-factor.md](https://github.com/chaoss/wg-risk/blob/main/focus-areas/business-risk/elephant-factor.md).

### `organizational_hhi` / `effective_organizational_population`
- **Definition:** HHI (and reciprocal) applied to organizational commit share, same math as `contributor_hhi`.
- **Direction of good:** `organizational_hhi` is **lower**; `effective_organizational_population` is **higher**
  (its reciprocal). **Role:** `organizational_hhi` is **key** (organizational diversity) — this dimension's
  second and last key metric (kept to 2, not 3, per SCORING.md §5.3, since `elephant_factor` and
  `organizational_hhi` together already cover both the discrete and continuous readings of the same underlying
  concentration); `effective_organizational_population` is supporting.
- **Formula:** `HHI_org = Σ s_org²`; `EffectivePopulation_org = 1 / HHI_org`.
- **Population & exclusions:** §0.5, §0.6; `unknown` treated as its own bucket, never redistributed (D6).
- **Window:** trailing-12m.
- **Required data / source:** git log + `affiliations.yaml`.
- **Strengths:** Continuous complement to the discrete `elephant_factor`; standard concentration math.
- **Weaknesses / gaming risk:** Same affiliation-data-quality caveat as `elephant_factor`.
- **CHAOSS equivalent:** [Organizational Diversity](https://www.chaoss.community/kb/metric-organizational-diversity/)
  (CHAOSS's own metric is closer to a single-company-share ratio; this project reports both the simple ratio —
  see `single_org_share` — and the fuller HHI, since HHI captures concentration among several large orgs that a
  single-company ratio can miss).

### `single_org_share`
- **Definition:** Share of total commits (in the window) contributed by the single largest organization — the
  CHAOSS "Organizational Diversity" metric as published (ratio of contributors/commits from one company over the
  total).
- **Direction of good:** lower. **Role:** supporting (organizational diversity) — the exact published CHAOSS
  formula and a useful sanity-check number, but deliberately not `key`: it is largely implied by
  `organizational_hhi` (already `key`), and a single-largest-org share can miss multi-organization concentration
  that HHI catches (see Weaknesses below).
- **Formula:** `max_i(s_org_i)`.
- **Population & exclusions:** §0.5, §0.6; `unknown` bucket shown separately, never merged into the "largest
  known" figure.
- **Window:** trailing-12m.
- **Required data / source:** git log + `affiliations.yaml`.
- **Strengths:** Matches the published CHAOSS definition exactly, easiest of the organizational metrics for the
  community to sanity-check by hand.
- **Weaknesses / gaming risk:** A single ratio hides multi-organization concentration (three orgs at 30% each is
  invisible to this metric but visible to `organizational_hhi`) — reported together, never as a substitute for HHI.
- **CHAOSS equivalent:** [Organizational Diversity](https://www.chaoss.community/kb/metric-organizational-diversity/).

### `unknown_affiliation_rate`
- **Definition:** Share of commits (or, separately, of distinct contributors) in the window whose author
  affiliation is `unknown` per `affiliations.yaml` and the heuristic seed rules (D6).
- **Direction of good:** **none** — deliberately not assigned: this metric measures the completeness of
  affiliation *resolution*, not project health directly; per D6 an `unknown` value must never be guessed away, so
  neither a rising nor a falling rate has a clean "the project got healthier/less healthy" reading on its own —
  it is shown as a confidence modifier on `elephant_factor`/`organizational_hhi`/`single_org_share`, not
  classified independently. **Role:** supporting (organizational diversity).
- **Formula:** `UnknownRate(m) = commits_unknown(m) / commits_total(m)`, reported alongside the equivalent
  contributor-count version.
- **Population & exclusions:** none — this metric's entire purpose is to quantify the size of the `unknown`
  bucket the other organizational metrics exclude from their denominators, per D6's "unknown stays unknown,
  never guessed" rule; it is `established` **as a measurement of the unknown bucket itself**, even though the
  metrics it caveats are experimental/established with caveats.
- **Window:** trailing-12m.
- **Required data / source:** git log + `affiliations.yaml`.
- **Strengths:** Makes the coverage gap in organizational metrics visible and auditable instead of silently
  excluded, directly serving D2's "uncertainty is shown" principle.
- **Weaknesses / gaming risk:** None — this metric exists specifically to prevent the other organizational
  metrics from looking more confident than the underlying data supports; a high `unknown_affiliation_rate` should
  visibly suppress confidence in `elephant_factor`/`organizational_hhi`/`single_org_share` on the dashboard.
- **CHAOSS equivalent:** none directly; a transparency companion metric this project adds on top of the CHAOSS
  organizational metrics.

---

## 6. Release Cadence

### `release_frequency`
- **Definition:** Count of GA releases published in a trailing-24-month window.
- **Direction of good:** target-range — deliberately not `higher` or `lower`: too few releases signals stagnation,
  but unboundedly more releases is not unambiguously healthier either (it can strain downstream packagers,
  operators, and test capacity), so this metric is judged against the project's own historical range (§4.1's
  trailing-24m baseline), not a monotonic direction. **Role:** **key** (release cadence) — this dimension's only
  key metric (1 of the allowed 1–3, per SCORING.md §5.3): the sole reasonably continuous cadence signal in this
  dimension, since `release_regularity` and `time_since_last_release` are both `none`-direction and cannot carry
  a veto.
- **Formula:** `Freq = |{ releases : release_date ∈ trailing24m }|`.
- **Population & exclusions:** RCs, alphas, and betas excluded by default (reported separately as a secondary
  series, since RC cadence is itself informative about release-process health, just a different question).
- **Window:** trailing-24m, updated monthly.
- **Required data / source:** `dist.apache.org` release archive metadata; git tags; GitHub Releases (as a
  cross-check).
- **Strengths:** Fully deterministic, low ambiguity.
- **Weaknesses / gaming risk:** A count alone does not show regularity — a burst of four releases in one quarter
  and none for the next eighteen months reads the same total as evenly spaced releases; see `release_regularity`.
- **CHAOSS equivalent:** none named identically; adjacent to general CHAOSS "Releases" data collection guidance.

### `release_regularity`
- **Definition:** Coefficient of variation (CoV = standard deviation / mean) of inter-release intervals over the
  trailing window, as a proxy for how predictable the release cadence is.
- **Direction of good:** **none** — deliberately not assigned: an intentional long freeze (e.g., Cassandra's 4.0
  stabilization period, `SCORING.md` §6) spikes this metric without the project being any less healthy, so it is
  shown as context and only interpretable structural-break-aware, never as a standalone improving/declining
  signal. **Role:** supporting (release cadence).
- **Formula:** `CoV = σ(Δt) / μ(Δt)` over the set of inter-release gaps `Δt` in the window.
- **Population & exclusions:** floor of 3 releases (2 intervals) in the window; below that, `insufficient data`.
- **Window:** trailing-24m.
- **Required data / source:** same as `release_frequency`.
- **Strengths:** Distinguishes "steady cadence" from "bursty" releases with the same count.
- **Weaknesses / gaming risk:** CoV is undefined/unstable for very few intervals (§0.6 floor mitigates); a single
  intentional long freeze (e.g., Cassandra's 4.0 stabilization period) will spike this metric — treated explicitly
  as a candidate structural break, documented in `SCORING.md` §6, not silently smoothed over.
- **CHAOSS equivalent:** none named; a `proxy` composition on top of raw release timestamp data.

### `time_since_last_release`
- **Definition:** Days between the most recent GA release and the current run date.
- **Direction of good:** **none** — deliberately not assigned: a long gap can precede a major release (healthy
  build-up, as with the 4.0 freeze) or reflect genuine stagnation (unhealthy), and the two are indistinguishable
  from this metric alone — only `release_frequency`/`release_regularity` in context can tell them apart. **Role:**
  supporting (release cadence).
- **Formula:** `t_now − t_last_release`.
- **Population & exclusions:** none.
- **Window:** snapshot (not a windowed rate).
- **Required data / source:** same as `release_frequency`.
- **Strengths:** Trivial, unambiguous, immediately legible.
- **Weaknesses / gaming risk:** None as a fact; needs baseline context (is this normal for Cassandra?) to
  interpret, which is exactly what `SCORING.md`'s historical-baseline comparison supplies rather than an absolute
  threshold.
- **CHAOSS equivalent:** none named; basic derived fact.

---

## 7. Interaction Health (Phase 2 — brief pointer only)

The metrics below require classifying message content and are gated behind the human-labeled benchmark corpus
and agreement/precision thresholds described in D1 (Phase 2a) and D7. Their full taxonomy, thread-level model,
disagreement-vs-toxicity distinction, and validation methodology are specified in `COMMUNITY-HEALTH.md`, not here.
They are listed here only so the dimension has a named home in the metric catalog and summary table.

- `dismissive_interaction_rate` — dismissive interactions per 1,000 classified messages (aggregate only, never
  per-person, per D2.4). Direction of good: lower. Role: supporting.
- `escalation_rate` — share of threads whose classified trajectory includes an escalation event. Direction of
  good: lower. Role: **key**.
- `constructive_resolution_rate` — share of threads containing disagreement that reach a classified
  "resolution"/"compromise" state rather than ending in abandonment. Direction of good: higher. Role: **key**.
- `newcomer_interaction_quality` — aggregate measure of how first-time posters are treated in their initial
  thread(s), reported only in aggregate (e.g., correlated with `first_to_second_conversion_rate` as a hypothesis,
  never as an individual-level causal claim — see `SCORING.md` on correlation vs. causation and D2.7's ban on
  causal claims). Direction of good: higher. Role: supporting.

This dimension's 1–3 key metrics (per SCORING.md §5.3, once Phase 2a ships) are `escalation_rate` and
`constructive_resolution_rate` — the two signals that most directly capture whether disagreement on this
project trends toward resolution or toward harm, without relying on a single subjective judgment. This
assignment is provisional pending `COMMUNITY-HEALTH.md`'s own taxonomy and validation work, and may change before
Phase 2a ships (any change is itself a scoring-rule version bump per `SCORING.md` §8).

All four are tier `classified` and cannot ship until `COMMUNITY-HEALTH.md`'s validation gates (inter-rater
agreement, precision/recall against a frozen benchmark corpus, classifier-version provenance) are met, per D1.

---

## 8. Sources referenced

- CHAOSS Knowledge Base: https://chaoss.community/kb/ (individual metric pages linked inline above).
- CHAOSS Responsiveness Practitioner Guide: https://www.chaoss.community/practitioner-guide-responsiveness/
- CHAOSS wg-risk Elephant Factor: https://github.com/chaoss/wg-risk/blob/main/focus-areas/business-risk/elephant-factor.md
- Avelino, G., Passos, L., Hora, A., & Valente, M. T. (2016). *A Novel Approach for Estimating Truck Factors.* ICPC 2016. https://arxiv.org/abs/1604.06766
- Ferreira, F., Avelino, G., Valente, M. T., Ferreira, I., & Bigonha, M. (2019). *Algorithms for estimating truck factors: a comparative study.* Software Quality Journal. https://doi.org/10.1007/s11219-019-09457-2
- Lin, B., Robles, G., & Serebrenik, A. (2017). *Developer Turnover in Global, Industrial Open Source Projects: Insights from Applying Survival Analysis.* ICGSE 2017. https://doi.org/10.1109/ICGSE.2017.11
- Foucault, M., Palyart, M., Blanc, X., Murphy, G. C., & Falleri, J.-R. (2015). *Impact of Developer Turnover on Quality in Open-Source Software.* ESEC/FSE 2015. https://doi.org/10.1145/2786805.2786870
- Rhoades, S. A. (1993). *The Herfindahl-Hirschman Index.* Federal Reserve Bulletin. https://www.federalreserve.gov/pubs/bulletin/1993/pdf/index2.pdf
- US DOJ/FTC Herfindahl-Hirschman Index guidance: https://www.justice.gov/atr/herfindahl-hirschman-index
- ASF Whimsy public roster (committer/PMC data): https://whimsy.apache.org/public/, https://whimsy.apache.org/docs/api/ASF/Committee.html
- Apache Pony Mail JSON API (mailing-list metadata): https://lists.apache.org/ (list archive with API access, per DECISIONS.md)
- `docs/spec/data-probe.md` (this repository) — verified, empirical probe of `apache/cassandra` git history, ASF
  JIRA custom fields, GitHub PR counts, Pony Mail coverage, and the ASF roster, run 2026-09-25. This is the
  primary evidence base for §0.3's merge-commit exclusion rule and §0.4's reviewer-proxy coverage numbers and
  2017+ baseline recommendation.

**Unverified / to confirm during implementation** (flagged rather than silently assumed):
- Exact current Pony Mail JSON API endpoint shape and rate limits for `dev@cassandra.apache.org` — verify against
  `DATA-SOURCES.md` (separate deliverable) before building the collector.
- `docs/spec/data-probe.md`'s reviewer-trailer and JIRA-reviewer-field coverage numbers (§0.4) were produced by a
  single probe run against current repository/API state; the per-year breakdown should be re-verified once the
  actual collector is built, and the two sources' *agreement rate* (how often trailer and JIRA reviewer field
  name the same people for the same issue) has not yet been measured — only that both are populated. That
  agreement-rate check should happen before `unique_reviewers_monthly` and its dependents are promoted to
  `established`.
- Whether pre-2016 git history has any usable reviewer signal at all (the data probe shows 43–57% trailer
  coverage in 2009–2014, not zero) — this project treats that period as context-only, not baseline-eligible, but
  has not attempted to mine an alternative signal (e.g., mailing-list patch-review threads) for those years.
