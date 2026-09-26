# SCORING.md — Scoring and Aggregation Design

Status: draft for review. Companion to `docs/spec/DECISIONS.md` (binding, especially D2 and D4) and
`docs/spec/METRICS.md` (deliverable #2, defines every metric referenced below). Worked-example numbers throughout
this document are **illustrative only** — clearly labeled, invented for exposition, not derived from real
Cassandra data.

## 1. The question this document answers

Given a catalog of individually well-defined metrics (`METRICS.md`), how does the system turn "here are forty
numbers" into something a visitor to the dashboard can act on, without manufacturing false precision or hiding
deterioration behind an average? D4 already settled the headline answer — dimensions plus historical baseline, no
composite score — but D4 is two sentences and this document is the concrete mechanism: the exact baseline
definition, the exact status rule, how dimension status is derived without averaging away a bad metric, how
structural breaks are handled, how uncertainty is shown, and how the rules themselves are versioned.

## 2. Candidate scoring approaches — comparison

The research brief (`docs/research.md`, "Scoring") asked for a comparison before committing. Below is that
comparison; §3 explains why D4 picked what it picked.

| Approach | What it does | Strength | Why it's wrong for this project (or where it survives, scoped) |
|---|---|---|---|
| **No aggregate at all** | Publish only raw metrics, no synthesis whatsoever. | Maximum transparency; zero risk of hiding anything; simplest to build and reason about. | Too little synthesis to be usable — a visitor facing forty ungrouped numbers has no entry point. This project keeps raw metrics as the ground truth one click away (D2.3) but does not stop there. |
| **Dimension scores (no composite)** | Group metrics into named dimensions (contributor sustainability, reviewer capacity, etc.); report a status per dimension; never combine dimensions into one number. | Enough synthesis to be navigable, small enough grouping that a reader can still see which metrics drove a dimension's status; matches how the research brief's own "Historical Analysis" questions are phrased (per-dimension: "is reviewer concentration increasing?" not "is the project healthy?"). | This is the chosen approach (D4). Its own failure mode — averaging away a bad metric inside a dimension — is handled explicitly in §5, not by falling back to raw numbers. |
| **Weighted composite score** | Assign a weight to every metric or dimension, sum to one number (0–10, 0–100, letter grade, etc.). This is the approach used by tools like [OpenSSF Scorecard](https://github.com/ossf/scorecard), which aggregates per-check scores (Maintained, Code-Review, Branch-Protection, Vulnerabilities, …) into a single weighted 0–10 result. | Maximally legible to a non-expert; easy to rank projects against each other; easy to put in a badge. | Explicitly a non-goal (D2.7 — "no single headline health number"). The weights are a value judgment dressed as math: OpenSSF Scorecard's own weights are a defensible but debatable editorial choice, and a single number by construction *must* average away a severe problem in one area if enough other areas look fine — exactly what D2.3/D4 forbid. Composite scores also invite gaming toward whatever the formula rewards rather than toward actual health. |
| **Percentile / benchmark scores** | Compare a project's metric to a distribution across many projects (e.g., "70th percentile for reviewer diversity among ASF projects"). | Gives external context a pure self-baseline can't; useful once multiple projects are onboarded (D1 Phase 3). | Requires a peer set collected with the same definitions — this project has exactly one project (Cassandra) instrumented in Phase 1–2, so there is no peer set yet. It also violates D2.2 ("status compares the project to its own history, never to universal thresholds") if used for *status*; it can still be shown later, once Phase 3 adds a second project, as *context*, explicitly separate from status. Deferred, not rejected. |
| **Historical-baseline scores** | Compare current metric values to the project's own trailing history using robust statistics; classify direction of change. | Matches D2.2 exactly; requires no cross-project data; naturally accommodates a project's own idiosyncratic normal (Cassandra's release cadence, JIRA-centric workflow, etc.). | **This is the chosen mechanism** (D4), detailed in §4. Its risks — structural breaks, small-n noise — are handled in §6–7, not ignored. |
| **Maturity-adjusted scores** | Normalize expectations by project age/size/lifecycle stage (a 2-year-old project isn't held to a 15-year-old project's release cadence). | Prevents unfair comparison across very differently-staged projects. | Same problem as percentile scoring: needs a reference set of projects at known maturity stages, which does not exist yet in a single-project system. A trailing self-baseline (§4) already captures *this* project's own maturity trajectory implicitly, which is enough for Phase 1–3's single-project (then two-project) scope. Revisit if/when Phase 3 expansion produces enough projects to define maturity cohorts honestly. |
| **Statistical anomaly detection (SPC-style)** | Treat each metric as a monitored process; flag values outside control limits (e.g., Shewhart control charts, Western Electric rules) as signals rather than computing a graded score at all. | Well-established methodology (statistical process control, in use since the 1920s; see Shewhart/Western Electric Rules, 1958) for separating routine variation from a real change, without needing a peer set. Doesn't produce a false sense of graded precision — it just says "in control" or "out of control." | Adopted as the *mechanism inside* the historical-baseline approach (§4), not as a separate competing approach — SPC's "flag deviations from a monitored process's own history" framing is exactly D2.2's own baseline principle, formalized. Pure SPC terminology ("out of control") is avoided in the UI in favor of plain-language improving/stable/declining/insufficient data (§5), because "out of control" reads as an alarm bell for what may be routine variation once a robust baseline is used. |

**Decision (already made in D4, restated for clarity):** dimension-level status only, computed against the
project's own historical baseline, using SPC-style deviation detection with robust statistics (§4). No
project-wide composite number, ever. Percentile/benchmark and maturity-adjusted approaches are explicitly
deferred — not rejected in principle — to Phase 3, when a second project (D1) gives this system an honest peer
set to compare against; until then, showing a percentile of one project's history against itself would just be a
confusing rename of the baseline comparison this document already specifies.

## 3. What CHAOSS's own metrics models do (context, not a rule this project must follow)

CHAOSS itself publishes "Metrics Models" that group multiple metrics under a named concern (e.g., *OSS Project
Viability*, *Community Welcomingness* — https://chaoss.community/kb/metrics-model-oss-project-viability-strategy/,
https://chaoss.community/kb/metrics-model-community-welcomingness/) without collapsing them into a single score;
this is structurally the same "dimension, not composite" choice D4 makes, and is one more point of external
support for that decision, though this project's specific dimension boundaries (contributor sustainability,
reviewer capacity, responsiveness, organizational diversity, release cadence, later interaction health) are its
own, chosen to match the research brief's own framing rather than copied from any single CHAOSS model.

## 4. Baseline definition

### 4.1 Window

The baseline for a metric is computed over its own **trailing 24 completed months**, evaluated monthly on the 1st
(D5 — monthly editions use completed months only; nightly runs never let a partial month contribute to a
baseline or a status call). Twenty-four months, not twelve, by default, for two reasons: (a) it is long enough to
span at least one full release cycle for a project with Cassandra's historical cadence (`release_frequency`,
`METRICS.md` §6), and (b) it gives robust statistics (§4.2) enough data points to be stable against a single
unusual month. Where a metric's own data-availability window is shorter than 24 months for a documented reason —
e.g., reviewer-proxy metrics, whose evidence base starts in 2017 per `METRICS.md` §0.4's verified coverage data —
the baseline uses whatever completed-month history exists back to that documented start, explicitly labeled
"partial baseline history" rather than silently padded or silently shortened without explanation.

### 4.2 Robust statistics, not mean/standard-deviation

The baseline center and spread are computed with **median and MAD (median absolute deviation)**, not mean and
standard deviation, because OSS activity metrics are routinely non-normal and outlier-heavy (a single record
month, a single dead month), and mean/stdev are known to be distorted by exactly that kind of outlier while
median/MAD tolerate up to 50% contamination before breaking down. This project follows the standard robust
"modified z-score" formulation:

```
median_baseline  = median(x_1, …, x_24)                      # trailing 24 completed months
MAD              = median(| x_i − median_baseline |)
modified_z       = 0.6745 × (x_current − median_baseline) / MAD
```

The constant 0.6745 rescales MAD so it estimates a standard deviation under a normal distribution, which is the
standard convention (Φ⁻¹(3/4) ≈ 1.4826; the modified z-score literature uses `0.6745 = 1/1.4826`). When `MAD = 0`
(a metric that has been perfectly flat for 24 months — plausible for something like `release_frequency` in a
quiet stretch), the modified z-score is undefined by division-by-zero; the system falls back to the raw magnitude
of `x_current − median_baseline` compared against a metric-specific minimum-meaningful-change floor (documented
per metric in `METRICS.md` where relevant) rather than reporting a spurious infinite z-score.

**Seasonality.** Some metrics plausibly have seasonal structure (e.g., mailing-list volume dipping in
northern-hemisphere August, JIRA activity dipping around end-of-year holidays). Where a metric is later shown
empirically to have a real seasonal component (a decision made explicitly, per metric, and logged — not assumed
by default for every metric), the baseline is computed **within the same calendar month across prior years**
(e.g., comparing this August to the trailing 3–5 prior Augusts) rather than the trailing-24-month rolling window,
using the same median/MAD mechanics. This is deferred as an opt-in refinement per metric rather than a Phase 1
default, because claiming a seasonal pattern without evidence would itself be a form of false precision this
project is trying to avoid; `METRICS.md` does not currently mark any metric seasonal, and none should be marked
seasonal without a documented empirical check.

### 4.3 Direction of good

A metric's raw movement is not self-interpreting — "up" is good for some metrics and bad for others. Every metric
in `METRICS.md` §1's summary table (and each metric's own detail entry) carries one of four explicit
**`direction_of_good`** values, used by the status rule in §5. This document is the source of truth for what the
four values *mean* and how they feed the status rule; `METRICS.md` is the source of truth for which value is
*assigned* to each metric — the two are kept in sync by construction (a change to either is a scoring-rule or
metric-definition version bump, §8).

| Direction | Meaning | Representative examples (full, authoritative list: `METRICS.md` §1) |
|---|---|---|
| `higher` | An increase relative to baseline is `improving`. | `sustained_contributor_count`, `truck_factor`, `unique_reviewers_monthly`, `elephant_factor` (a *higher* count of organizations needed to reach the 50% threshold means less concentration — the healthy direction — the mirror image of `organizational_hhi`/`single_org_share`, which is exactly why direction is assigned per metric, never inferred from a dimension or from another metric's direction). |
| `lower` | An increase relative to baseline is `declining`. | `reviewer_hhi`, `contributor_hhi`, `organizational_hhi`, `time_to_first_response_jira`, `stale_jira_rate`, `time_to_first_reply_devlist`. |
| `target-range` | Neither extreme is healthy; judged against the project's own historical range (§4.1–4.2), not a monotonic direction. | `release_frequency` — too rare signals stagnation, too frequent strains downstream packagers/operators/test capacity, so there is no simple more-is-better reading. |
| `none` | Movement is informative but not classifiable as good/bad on its own; never drives a status classification and never carries `key` veto power (§5.3). | `time_since_last_release` (a long gap can precede a major release or reflect stagnation — indistinguishable alone), `release_regularity` (a spike during an intentional stabilization freeze, e.g. Cassandra's 4.0 freeze, is not "bad" — see §6), `review_load_per_reviewer` (rising load can mean healthy throughput growth or overload, depending on the concurrent `reviewer_hhi` reading), `merge_authority_concentration` (concentration among earned ASF committers is expected in a meritocratic project, unlike `reviewer_hhi` concentration, so no direction is assigned), `unknown_affiliation_rate` (a data-completeness signal, not a health signal — D6). |

Every `none`-direction metric's one-line reason is recorded on the metric itself in `METRICS.md`, not just
summarized here, per D2.3's auditability requirement.

## 5. Status rule: improving / stable / declining / insufficient data

### 5.1 Per-metric status

For a metric with a valid baseline (§4) and a current value `x_current` for the most recently completed month
(or trailing window, per the metric's own window definition in `METRICS.md`):

1. **`insufficient data`** if any of: the metric's population in the current window is below its documented
   minimum sample size (`METRICS.md` §0.6), fewer than 12 completed months of baseline history exist yet (even a
   partial 24-month baseline needs a floor), or the metric is `none` direction (§4.3 — those are shown as
   raw trend lines, never classified improving/stable/declining).
2. Otherwise, compute the modified z-score (§4.2, or the fallback for `MAD = 0`).
3. Classify using thresholds modeled on the Western Electric Rules' single-point 2-sigma/3-sigma convention
   (Western Electric Company, *Statistical Quality Control Handbook*, 1956; commonly cited as the 1958 "Western
   Electric Rules"), adapted to the modified z-score:
   - `|modified_z| < 1.5` → **`stable`** (routine variation, not a signal).
   - `1.5 ≤ |modified_z| < 3.0`, sign consistent with the metric's direction-of-good (§4.3) → **`improving`**
     (favorable direction) or **`declining`** (unfavorable direction).
   - `|modified_z| ≥ 3.0` → same `improving`/`declining` label as above, but rendered with a visually stronger
     "large deviation" flag — this project does not introduce a fifth status label for this case (D2 favors a
     small, legible vocabulary), but the magnitude is shown, not thrown away.
4. **Sustained-trend override:** a single month crossing the 1.5-sigma line is not, by itself, enough to move a
   metric out of `stable` for the *dashboard's* status if it is a one-month blip; this project requires the
   deviation to hold for **2 of the last 3 completed months** (a direct adaptation of the Western Electric "2 out
   of 3 beyond 2-sigma" rule) before the per-metric status flips from `stable` to `improving`/`declining`. A
   single extreme month (`|modified_z| ≥ 3.0`) is flagged immediately as a "notable single-month event" alongside
   the (still `stable`, pending confirmation) status, so a real shock is never hidden for two months waiting for
   confirmation — it is shown, just not yet promoted to a sustained-trend classification.

### 5.2 Worked example (illustrative — invented numbers)

Metric: `time_to_first_response_pr` (median days, `lower`). Trailing 24 completed months' medians
(illustrative): baseline `median = 3.0` days, `MAD = 0.8` days. Current month's value: `5.1` days.

```
modified_z = 0.6745 × (5.1 − 3.0) / 0.8 = 0.6745 × 2.625 = 1.77
```

`1.5 ≤ 1.77 < 3.0` and the direction is unfavorable (higher latency is worse for a `lower` metric) →
this month reads as a `declining` signal. Per §5.1 step 4, the dashboard checks whether 2 of the last 3 completed
months clear the 1.5-sigma line before flipping the *displayed* status from `stable`; if only this one month
does, the dashboard shows `stable` with a "notable single-month event" flag rather than `declining`, and revisits
next month.

### 5.3 Dimension status: derived from metrics, deterioration never averaged away

A dimension's status is **not** a numeric average or vote count of its member metrics' statuses. Averaging would
recreate exactly the composite-score failure mode D4 rejects, one level down. Instead:

**Rule:** A dimension's displayed status is the **worst** status among its non-`insufficient-data` member metrics,
using the ordering `declining` worse than `stable` worse than `improving`, with one refinement: metrics are
tagged, in `METRICS.md`, with a **`role`** of either **key** (load-bearing for that dimension's core concern —
capped at 1–3 per dimension, e.g. `truck_factor`, `contributor_hhi`, and `sustained_contributor_count` are the
three key metrics for contributor sustainability; `time_since_last_release`, being `none`-direction, cannot be
key) or **supporting**. A dimension shows `declining` if **any key metric** is `declining`, regardless of how many
supporting or other key metrics are `improving` or `stable`. A dimension shows `improving` only if no key metric
is `declining` and at least one key metric is `improving`. Otherwise `stable`. If every key metric in a dimension
is `insufficient data`, the dimension itself shows `insufficient data`.

This "any key metric declining wins" rule is the direct implementation of D2's own text: *"deterioration in any
key metric must stay visible — no averaging away."* Which metrics are tagged `key` per dimension is itself
versioned (§8) — adding or removing a `key` tag is a scoring-rule change, not a silent editorial tweak, because it
changes which single metric can veto a dimension's status. The full, authoritative `role` assignment for every
metric lives in `METRICS.md` §1 (summary table) and each metric's own entry; the complete key-metric roster per
dimension is:

- Contributor sustainability: `truck_factor`, `contributor_hhi`, `sustained_contributor_count`
- Reviewer capacity: `reviewer_hhi`, `review_latency`, `unique_reviewers_monthly`
- Responsiveness: `time_to_first_response_jira`, `stale_jira_rate`, `time_to_first_reply_devlist`
- Organizational diversity: `elephant_factor`, `organizational_hhi`
- Release cadence: `release_frequency`
- Interaction health (Phase 2, provisional pending `COMMUNITY-HEALTH.md`): `escalation_rate`, `constructive_resolution_rate`

**Worked example (illustrative).** Contributor sustainability dimension, hypothetical month:

| Metric | Role | Status |
|---|---|---|
| `active_contributors_monthly` | supporting | improving |
| `new_contributors_monthly` | supporting | stable |
| `sustained_contributor_count` | key | stable |
| `truck_factor` | key | **declining** |
| `contributor_hhi` | key | stable |
| `contributor_tenure_survival` | supporting | insufficient data (cohort below floor) |

Dimension status: **declining** — driven entirely by `truck_factor`, even though the other two key metrics read
`stable` and one supporting metric reads `improving`. `contributor_tenure_survival` is `supporting`, not `key`
(§1's roster and `METRICS.md`'s own note on that metric explain why: it is `experimental` and frequently gated to
`insufficient data` by its cohort-size floor, which would make the dimension's status depend on a metric that is
often unavailable), so its `insufficient data` reading here has no bearing on the dimension's status. The
dashboard shows this explicitly ("declining, driven by: truck_factor"), not as a bare word.

### 5.4 What "insufficient data" means at the dimension level

If a dimension has at least one key metric with a real (non-insufficient) status, the dimension is not
`insufficient data` even if some other key metric is — each metric's data-sufficiency is independent and shown
per metric. The dimension only reads `insufficient data` when *no* key metric has enough data to classify,
consistent with D5's "partial months never drive status."

## 6. Structural breaks

A structural break is a real, identifiable event that changes the *process being measured*, such that comparing
before and after against the same rolling baseline produces a misleading signal — not noise to be smoothed over,
but a case where the baseline itself needs to be segmented.

**Known and anticipated structural breaks for Cassandra**, to be encoded in a maintained, versioned
`structural-breaks.yaml` (or equivalent) rather than handled ad hoc per metric:

- **The 4.0 stabilization freeze.** Cassandra's long freeze before the 4.0 release (an extended period of
  feature-freeze focused on stabilization) plausibly depresses `release_frequency`/inflates `release_regularity`'s
  CoV and shifts contribution *type* mix (more bugfix/testing activity, less new-feature commits) without those
  changes meaning anything bad about project health. **Handling:** `release_regularity` and
  `time_since_last_release` are `none` direction already (§4.3), so they never drive a `declining` status
  on their own; additionally, once the exact freeze date range is confirmed against the real release history
  (`METRICS.md` §8 flags this as needing empirical verification), that date range is excluded from the trailing
  baseline window for `release_regularity`, so a future *unplanned* long gap is still compared against a baseline
  that has not been artificially widened by the 4.0 freeze itself.
- **The 2017-era reviewer-metadata reliability shift.** Per `METRICS.md` §0.4's verified data-probe findings,
  the "reviewed by" commit-trailer convention's coverage of non-merge commits rose from roughly 43–57%
  (2009–2014) to 71–77% (2016–2017) and has held at 79–87% from 2018 onward. This is not a change in reviewer
  *behavior* — it is a change in how *completely that behavior gets recorded* by the collectors this project can
  build. Treating pre-2017 data as part of the same baseline as post-2017 data would read as "reviewer activity
  improved," when the honest reading is "our visibility into reviewer activity improved." **Handling:** every
  reviewer-proxy metric's baseline (§4.1) starts no earlier than 2017, by construction, not by a break correction
  applied after the fact — this is a hard floor on the baseline window, documented once here and in `METRICS.md`
  §0.4 rather than re-derived per metric.
- **COVID-era disruption (2020–2021).** Broad, cross-project shifts in OSS contribution patterns during this
  period are documented in the general OSS research literature; this project has not yet independently verified
  Cassandra-specific COVID-era anomalies against its own data (an open item, not an assumed fact — flagged rather
  than silently applied). If verified, the same segmented-baseline treatment as the 4.0 freeze would apply to the
  affected window.
- **Any future JIRA field migration, GitHub repository change, or ASF infrastructure migration** that alters
  what a collector can see (e.g., if Cassandra's review workflow moves further onto GitHub PRs over time) is
  itself a candidate structural break and should be logged in `structural-breaks.yaml` at the time it is first
  noticed, not retroactively reconstructed months later.

**General rule:** a structural break is never silently patched into a metric's historical baseline by smoothing
or interpolating. It is logged, dated, and, where it affects a specific metric's baseline window, that window
exclusion is itself part of the metric's versioned definition (§8) — a reviewer of the system can see *why* a
given month is excluded from a given metric's baseline, not just that it is.

## 7. Uncertainty representation

- **Small-n flags.** Every metric subject to `METRICS.md` §0.6's minimum-sample-size rule renders
  `insufficient data` rather than a number below its floor (already specified in §0.6/§5.1). Above the floor but
  still small (rule of thumb: population under roughly 25, following the general small-sample-CI guidance that
  bootstrap coverage becomes unreliable well below that size), the dashboard additionally shows a "small sample"
  badge next to the value, distinct from `insufficient data` — the number is shown, but visually marked as less
  stable than a large-n value.
- **Confidence intervals via bootstrap.** For summary statistics computed over a population of individual
  events in a window (e.g., median `time_to_first_response_pr`, `review_latency`), the dashboard shows a
  nonparametric bootstrap confidence interval (resample-with-replacement from the window's events, recompute the
  median, repeat ≥ 1,000 times, report the 5th/95th percentile of the resampled medians as a 90% CI) alongside
  the point estimate. A 90%, not 95%, confidence level is used by default for windows with fewer than ~25 events,
  consistent with the general guidance that bootstrap intervals are optimistic (too narrow) at very small n and a
  slightly less ambitious stated confidence level is more honest at that scale; this project treats "the CI is
  wide" as a feature to display, not a reason to suppress the estimate.
- **Cohort/survival uncertainty.** `contributor_tenure_survival`'s Kaplan–Meier curve is shown with its
  Greenwood-formula confidence band (the standard variance estimator for KM curves) where the cohort clears its
  minimum size (`METRICS.md` §0.6, floor 30); below that, only the raw "N still active at M months" counts are
  shown, with no smoothed curve or CI at all, to avoid implying a precision the data doesn't support.
- **Concentration-metric uncertainty (HHI, bus/truck/elephant factor).** These are computed from the full
  observed population in the window, not a sample of it, so they have no sampling uncertainty in the usual sense
  — but they do have **definitional** uncertainty (e.g., `unknown_affiliation_rate` bounding how much
  `elephant_factor`/`organizational_hhi` can be trusted). That uncertainty is shown as the `unknown_affiliation_rate`
  value itself, displayed alongside the organizational metrics it caveats (`METRICS.md` §5), rather than folded
  into a numeric confidence interval that would overstate how quantifiable that particular uncertainty is.
- **Tier is itself an uncertainty signal.** A metric's tier (`established`/`proxy`/`experimental`/`classified`,
  `METRICS.md` §0.1) is shown next to its status everywhere the metric appears, per D2.1 — this is the
  project's primary, always-visible uncertainty disclosure, upstream of any statistical CI.

## 8. Versioning of the scoring rules

Per D2.6 and D3, a change to any of the following triggers a full-history recompute under a new scoring-rules
version number, with a changelog entry, exactly like a metric-definition version bump — never a silent change to
how existing history reads:

- The baseline window length (currently 24 completed months) or its robust-statistic method (currently
  median/MAD).
- The status thresholds (currently 1.5-sigma / 3.0-sigma modified z-score, 2-of-3-months confirmation).
- A metric's `direction_of_good` assignment.
- A metric's `key` vs. `supporting` tag within a dimension (§5.3) — since this changes which metric can veto a
  dimension's status.
- The dimension-status derivation rule itself (§5.3's "worst key metric wins").
- Any entry added to or removed from `structural-breaks.yaml` (§6), including the date ranges it excludes from a
  metric's baseline.
- The confidence-level convention or bootstrap iteration count used for uncertainty display (§7).

The scoring-rules version is a separate version number from the per-metric definition version (D3), since a
scoring-rule change (e.g., moving from 1.5/3.0-sigma to different thresholds) can affect every dimension's status
without changing any metric's underlying formula, and the two kinds of change should be independently auditable
and independently citable from a frozen monthly report (D5).

## 9. What must never be combined

Restating and extending D2.4/D2.7/D4/D20 as concrete rules for implementers, not just principles. D20
(2026-09-25) amends D4's original "no composite score, ever" — **this is the one, narrowly-scoped exception** to
the rule below, not a general license to combine things; §12 is the exact, versioned mechanism, and every other
combination this section lists is still forbidden without exception.

- **No project-wide composite score outside the exact mechanism §12/D20 defines.** The one composite this project
  publishes is a *fully reproducible*, versioned computation (`scoring.yaml`, `scoring_version`) that (a) never
  appears without its full per-dimension breakdown and a declining-key-metric flag alongside it (D20: "the
  composite never appears alone"), (b) is built only from each dimension's own `key` metrics (the same metrics
  that can veto that dimension's *status*, §5.3 — never a plain average of every metric in a dimension), and
  (c) is disclosed, never silently smoothed, whenever a dimension is excluded and its weight re-normalized (§12).
  Any *other* single number presented as "the" health score, grade, or index — an ad hoc weighting, a
  differently-normalized recomputation, an unversioned one-off — is still exactly what D4/D2.7 forbid.
- **No averaging of metric statuses within a dimension** — the worst-key-metric rule (§5.3) replaces any
  averaging or voting scheme. This is unchanged by D20: the composite's *score* (§12) is a numeric aggregate of
  numeric key-metric scores, but a dimension's *status* (`improving`/`stable`/`declining`/`insufficient_data`,
  shown right next to its score on the composite breakdown) is still never an average, always the worst-key-metric
  rule — the two are computed independently and never substitute for each other.
- **No combining dimensions across each other outside §12's published composite** (e.g., no ad hoc "sustainability
  + responsiveness ÷ 2" computed anywhere but the one versioned composite) — each dimension's status still stands
  alone on the dashboard, and the composite breakdown shows every dimension's own score and status individually,
  never collapsed into an intermediate sub-total.
- **No blending Phase 2 (classified) metrics into the composite** until `COMMUNITY-HEALTH.md`'s validation gates
  clear (D20) — Interaction Health has no key metrics in `scoring.yaml`'s composite-eligible registry today, and
  adding one is itself a `scoring_version` bump, never a silent inclusion once classification ships.
- **Governance compliance does not enter the composite.** Governance (D14/D15, per-commit minimums judged against
  a dated policy version) is not one of D4's five composite dimensions, and its pass/fail/unknown compliance
  model was never defined against a trailing self-baseline the way §4–5 requires. Whether governance should ever
  feed a project "health" composite is genuinely ambiguous in this spec as written; this project resolves that
  ambiguity by keeping governance out and disclosing the reason in `scoring.yaml` (`composite.excluded_dimensions`)
  rather than guessing either way.
- **No per-person scores of any kind**, classified or deterministic — D2.4. Reviewer/committer concentration
  metrics (`reviewer_top_k_share`, `merge_authority_concentration`, etc.) report aggregate shares, never a named
  individual's personal "score."
- **No merging identity-uncertain contributors** into a combined count for the sake of a cleaner-looking metric
  — D2.5, `METRICS.md` §0.5.
- **No redistributing `unknown` affiliation into known organizational buckets** to make an organizational metric
  look more complete — D6, `METRICS.md` §5.
- **No blending classified (Phase 2) interaction-health signals into any Phase 1 deterministic dimension's
  status** — interaction health is its own dimension (D4), gated behind `COMMUNITY-HEALTH.md`'s validation gates,
  and even once live, is never used to adjust, offset, or explain away a deterministic dimension's status (e.g.,
  "reviewer capacity is declining, but interaction health is great, so it nets out fine" is exactly the kind of
  statement this system is built to make impossible).
- **No causal claims** from any correlation this system surfaces (e.g., a correlation between newcomer
  interaction quality and `first_to_second_conversion_rate`) — D2.7. Correlations may be *shown*, explicitly
  labeled as correlational, never as "X causes Y."
- **No cross-project comparison used for status** until Phase 3 provides an honest peer set (§2's
  percentile/maturity-adjusted row) — and even then, cross-project context is additive, never a replacement for
  the self-baseline status computed in §4–5.

## 10. Worked dimension example, end to end (illustrative — invented numbers)

To make §4–5 concrete in one place: suppose, hypothetically, the **reviewer capacity** dimension for a completed
month reads as follows (all numbers invented for illustration, not real Cassandra data):

| Metric | Role | Baseline median (trailing 24 completed months) | MAD | Current value | Modified z | Direction | 2-of-3-months confirmed? | Status |
|---|---|---|---|---|---|---|---|---|
| `unique_reviewers_monthly` | key | 14 | 2 | 15 | 0.34 | higher | — | stable |
| `reviewer_hhi` | key | 0.18 | 0.03 | 0.27 | 2.02 | lower | yes (2 of last 3 months) | **declining** |
| `review_latency` (median days) | key | 4.0 | 1.0 | 4.3 | 0.20 | lower | — | stable |
| `effective_reviewer_population` | supporting | 5.6 | 0.9 | 3.7 | −1.43 | higher | no (1 of last 3 months) | stable (flagged: notable single-month event) |
| `contributor_reviewer_ratio` | supporting | 9.0 | 1.5 | 11.0 | 0.90 | lower | — | stable |
| `review_load_per_reviewer` | supporting | — | — | — | — | none | — | (not classified) |

Dimension status: **declining**, because `reviewer_hhi` — a key metric — cleared the 1.5-sigma threshold for 2 of
the last 3 months in the unfavorable direction. This holds even though the other two key metrics
(`unique_reviewers_monthly`, `review_latency`) both look stable. `effective_reviewer_population` is `supporting`
(it is `reviewer_hhi`'s own reciprocal, so giving it independent key status would let the same concentration
signal veto twice, §5.3) — its single weak month is shown as context, not as a second driver, even though it
happens to point the same direction as `reviewer_hhi` this month. The dashboard surfaces exactly this: **"Reviewer
capacity: declining — driven by reviewer_hhi (rising concentration, confirmed 2 of last 3 months);
effective_reviewer_population also weakened this month (supporting metric, shown for context, not a status
driver)."** A visitor who wants the raw numbers behind that sentence gets them by clicking through to each
metric's own page (D2.3), where the full baseline, current value, and calculation shown above are what they see.

## 11. Sources referenced

- CHAOSS Metrics Models (dimension-grouping precedent): https://chaoss.community/kb/metrics-model-oss-project-viability-strategy/, https://chaoss.community/kb/metrics-model-community-welcomingness/
- OpenSSF Scorecard (weighted-composite comparison point): https://github.com/ossf/scorecard — general knowledge of its public weighted-aggregation approach; not independently re-verified against its current scoring source during this research pass, flagged here rather than cited as a confirmed methodology detail.
- Western Electric Company (1956). *Statistical Quality Control Handbook.* The "Western Electric Rules" (commonly dated to the 1958 edition) for control-chart signal detection (points beyond 3-sigma; 2-of-3 beyond 2-sigma; 4-of-5 beyond 1-sigma; 8 consecutive on one side of center) — general statistical-process-control literature, adapted here to a modified z-score rather than a raw Shewhart chart.
- Median absolute deviation / modified z-score convention (0.6745 scaling factor): standard robust-statistics literature; see e.g. Iglewicz, B., & Hoaglin, D. C. (1993). *How to Detect and Handle Outliers.* ASQC Quality Press — cited from general statistical knowledge; not independently re-fetched during this research pass.
- Bootstrap confidence intervals at small sample sizes (coverage degradation below ~n=25): general nonparametric-bootstrap literature (Efron & Tibshirani, *An Introduction to the Bootstrap*, 1993) plus applied guidance gathered during this research pass — see `METRICS.md`-adjacent web research; specific coverage percentages cited in casual online sources during research were treated as indicative, not as a peer-reviewed figure this project should quote as precise.
- `docs/spec/data-probe.md` (this repository) — empirical basis for the 2017+ reviewer-baseline structural-break decision in §6.
- `docs/spec/DECISIONS.md` D2, D4, D5, D6 — binding constraints this entire document implements.

**Unverified / to confirm during implementation:**
- The exact 4.0 freeze date range (referenced in §6) has not been pulled from the real Cassandra release history
  in this research pass — needs confirmation against actual release/JIRA dates before `structural-breaks.yaml` is
  populated.
- Whether Cassandra shows a genuine, measurable COVID-era (2020–2021) anomaly in its own contribution data is an
  open empirical question, not assumed here.
- OpenSSF Scorecard's and CNCF LFX Insights's current, exact scoring formulas were described from general
  knowledge in §2's comparison table, not freshly re-verified against their primary documentation during this
  research pass (web search budget was exhausted before this could be completed) — the comparison's *shape*
  (weighted composite vs. this project's dimension-only approach) is a safe characterization, but exact weight
  values or check lists should not be quoted from this document without checking https://github.com/ossf/scorecard
  and https://insights.lfx.linuxfoundation.org/ directly first.

## 12. Composite Health Score (D20)

D20 (2026-09-25) amends D4: alongside the per-dimension statuses this document already specifies, the home page
also shows a single 0–100 composite, "comparable in spirit to LFX Insights' Health Score." Everything in §2–§10
above is unchanged by this — the composite is an *additional*, separately-computed number, never a replacement
for dimension status, and §9 (revised above) still forbids every other way of combining metrics or dimensions.

### 12.1 What makes this composite different from a generic weighted score

Unlike a typical weighted-composite tool (§2's OpenSSF Scorecard row), this project's composite is fully
reproducible and disclosed at every step, per D20:

- **Versioned inputs.** Every weight, every per-metric normalization function, and the roster of which metrics
  feed which dimension live in `scoring.yaml` at the repo root, tagged with a `scoring_version`. A change to any
  of them is a new version plus a `CHANGELOG.md` entry and a full-history recompute (D2 rule 6, mirroring §8's
  scoring-rules versioning) — never a silent reweighting.
- **Built only from `key` metrics.** A dimension's composite score is the mean of its own `key` metrics' 0–100
  normalized scores (§12.3) — the same metrics that can veto that dimension's *status* under §5.3's worst-key-
  metric rule. Supporting metrics contribute evidence to a metric's own page but never move the composite, for
  the same reason §5.3 restricts dimension status to key metrics: capping which metrics can move the number is
  what keeps a handful of healthy supporting metrics from diluting a real problem in a key one.
- **Never shown alone.** The home page always renders the composite with its full per-dimension breakdown (each
  dimension's own 0–100 score, status, and weight) immediately beside it, plus a visible flag whenever any
  dimension's status is `declining` (i.e., a key metric within it is declining, §5.3) — D20's own text: "a
  weighted average can hide a deteriorating dimension, and this prevents that." A reader never sees the bare
  number without also seeing exactly which dimension(s), if any, are pulling it down.
- **Classified metrics excluded until validated.** Phase 2 (`classified`-tier) metrics — Interaction Health's
  `escalation_rate`/`constructive_resolution_rate` — do not appear in `scoring.yaml`'s composite-eligible registry
  at all yet, and won't until `COMMUNITY-HEALTH.md`'s validation gates clear (D20's own text, §9's revised list).

### 12.2 Insufficient-data handling: disclosed re-normalization, at two levels

Real Cassandra data at M0 does not yet have a shipped collector for every metric METRICS.md defines — most
notably, `release_frequency` (release cadence's only key metric, §1) has no release collector yet, so that
dimension's status and score are `insufficient_data` on every run until it ships. D20 requires this to be shown,
not hidden behind a reweighted number that looks the same as if every dimension had data:

- **Within a dimension:** the dimension's score is the mean of only its `key` metrics that currently have a
  classifiable (non-`insufficient_data`) status this month (§5.1) — a key metric with no data yet, or too little
  baseline history, simply doesn't contribute to that mean. If *every* key metric in a dimension is
  `insufficient_data` (release cadence, today), the dimension has no score at all this month, matching §5.4's
  "the dimension only reads insufficient data when no key metric has enough data to classify."
- **Across dimensions:** a dimension with no score this month is dropped entirely from the top-level weighted
  average, and the remaining composite-eligible dimensions' `scoring.yaml` weights are re-normalized to sum to
  1.0 for that month's composite. Both re-normalizations are disclosed on the home page's breakdown every time
  they apply (which dimensions were included, how many of a dimension's key metrics scored) — never silently
  absorbed into a number that looks the same as a run with full data.

### 12.3 Per-metric normalization to 0–100

Every composite-eligible (`key`) metric's 0–100 score reuses the exact modified z-score already computed for its
own baseline status (§4.2) — never a second, independently-tuned statistic — so the composite stays a function of
the metric's own trailing self-baseline (D2 rule 2), exactly like status. The mapping is monotonic in the modified
z-score and direction-aware (§4.3):

- **`higher`/`lower` direction:** `score = clip(50 + z_scale × signed_z, 0, 100)`, where `signed_z` is the
  modified z-score, sign-flipped for a `lower`-direction metric so a positive `signed_z` always means "moving
  toward improving." A metric sitting exactly on its own trailing median scores 50; `z_scale` is chosen so
  `|signed_z| = stable_threshold` (1.5, §5.1) lands on 25/75 and `|signed_z| = large_deviation_threshold` (3.0)
  saturates at 0/100.
- **`target-range` direction** (`release_frequency` today, §4.3): `score = clip(100 - target_range_scale ×
  |modified_z|, 0, 100)` — sitting on the metric's own historical center scores 100, and a large deviation in
  *either* direction (too rare or too frequent) pulls the score down toward 0, matching §5.1's "declining" status
  reading for a large target-range deviation in either direction.
- **`none` direction:** excluded — a `none`-direction metric is never classified improving/stable/declining
  (§5.1 rule 1) and is never `key` for exactly that reason (§4.3: "never carries key power"), so it never enters
  a composite-eligible dimension's key-metric roster in the first place.

Both scale constants, and the exact 0–100 formula above, are published in `scoring.yaml`'s
`composite.normalization` block — a reader can recompute any dimension's score by hand from a metric's own
published `baseline_median`/`baseline_mad`/`modified_z` (D2.3's auditability requirement, extended to the
composite the same way it already applies to every raw metric).

### 12.4 What is (and isn't) in scope

Per §9 (revised above): governance compliance (D14/D15) and Phase 2 classified metrics never enter this
composite. `scoring.yaml`'s `composite.excluded_dimensions` names both exclusions and states the reason for each,
so a reader sees the boundary drawn explicitly rather than wondering why the Governance page's pass rates aren't
reflected in "the" number. The composite is Phase 1 (deterministic) only, over exactly the five dimensions D4
names minus Interaction Health (still Phase 2): contributor sustainability, reviewer capacity, responsiveness,
organizational diversity, release cadence.
