# Roadmap

Deliverable #7. Binding inputs: `docs/research.md` (brief) and `docs/spec/DECISIONS.md` (D1–D9), consistent
with `docs/spec/ARCHITECTURE.md`. Facts about Cassandra's actual data volume come from the live probe in
`docs/spec/data-probe.md` (2026-09-25, including its "Corrections" section) — this roadmap treats those
numbers as ground truth rather than the brief's rough estimates.

---

## 0. Smallest useful prototype

**Proposal: git + JIRA only, 6 metrics, one static page.**

### Why git + JIRA, not GitHub PRs or mailing lists

- Both sources confirm as the *primary* record for Cassandra specifically: DECISIONS.md already flags that
  "issues and most review discussion live in ASF JIRA, not GitHub Issues," and the data probe confirms it
  harder than expected — `apache/cassandra` has **GitHub Issues and Discussions disabled entirely**
  (`data-probe.md` §3), so JIRA isn't just the richer source, it's the *only* issue-tracking source that
  exists for this repo. Starting anywhere else would mean building against a secondary source first.
- Both are usable with **zero authentication** for public read access at prototype scale: a local git clone
  and unauthenticated JIRA REST calls. This keeps the very first CI pipeline free of secrets management,
  GitHub API rate-limit budgeting (GitHub's PR/GraphQL API is rate-limited at 5,000 req/hr per the probe;
  JIRA and a local clone are not meaningfully rate-limited at this scale), or Pony Mail's thread-reconstruction
  complexity.
- Together they already prove the two hardest architectural bets in `ARCHITECTURE.md` before investing in a
  fuller site: (a) the collector → normalize → Parquet → DuckDB → metric → chart pipeline end to end, and
  (b) reviewer-attribution extraction, which is the single most Cassandra-specific piece of logic in the
  system (the `patch by X; reviewed by Y` commit trailer, cross-checked against JIRA's
  `customfield_12313420`/`customfield_10022` reviewer fields — see `ARCHITECTURE.md` §3.1). If this works for
  two sources, the adapter pattern is validated before six more sources are added.
- GitHub PRs add real complexity (pagination, GraphQL, only 5,207 PRs vs. 21,483 JIRA issues — a fraction of
  review activity) for comparatively little incremental proof value at prototype stage. Mailing lists add
  thread-reconstruction logic that's better proven once the core pipeline shape is settled.

### Six metrics (all `established` or `proxy` tier per D2 rule 1 — none require classification)

1. **Active contributors** (trailing 90-day, git commit authorship, identity-resolved, bots excluded, merge
   commits excluded — merge exclusion matters concretely here: the probe found merge commits distort
   trailer-match rates by more than 2x, and the same distortion would inflate or skew contributor-activity
   counts if merges were counted as "activity").
2. **New contributors per month** (git; first-commit-ever per resolved identity).
3. **Commit frequency / volume trend** (git, non-merge commits/month, compared to trailing baseline per D4).
4. **Reviewer concentration (HHI)** (commit-trailer + JIRA-field reviewer attributions combined, per
   `ARCHITECTURE.md` §3.1; baseline window starts 2017-01-01 per `reliable_from`, since pre-2017 trailer
   coverage is 45–71% vs. 77–87% from 2017 on).
5. **Issue resolution latency** (JIRA: `resolved − created`, median + p90, trailing baseline).
6. **Open/stale issue backlog** (JIRA: count of open issues with no update in >N days, N configurable).

### One static page

A single scrollable home page rendering whichever of D4's five dimension cards have supporting data at this
stage (contributor sustainability, reviewer capacity, and a partial responsiveness card) — each card links
straight to a Vega-Lite chart + its downloadable JSON/CSV (D8), with no per-metric subpages, no monthly
report archive, no methodology/changelog/corrections pages beyond stubs. This is deliberately minimal: it
proves collectors → Parquet → DuckDB → chart → Pages deploy end to end (including a real `nightly.yml` run
and a real Pages deployment) before investing in the full site IA, identity-resolution tooling, roster/
affiliation ingestion, or additional adapters that `ARCHITECTURE.md` describes but that add no proof value
to "does the pipeline work."

**Exit criteria for the prototype**: `nightly.yml` runs unattended on schedule, writes to a real `data`
orphan branch, all 6 metrics have golden fixture tests passing, the site deploys to
`pmcfadin.github.io/cassandra-project-health/` via `actions/deploy-pages`, and re-running metric computation
against a pinned snapshot reproduces identical output (the reproducibility check from `ARCHITECTURE.md` §9).

---

## 1. Milestones through Phase 1 complete

| Milestone | Deliverables | Exit criteria | Key risks |
|---|---|---|---|
| **M0 — Prototype** (above) | git + JIRA adapters, 6 metrics, 1 static page, `nightly.yml`, golden tests | See exit criteria above | Identity resolution done naively (email-exact-match only) — acceptable here, must be revisited at M2 before it's load-bearing for contributor-count metrics |
| **M1 — Broaden Phase 1 sources** | GitHub PR adapter (5,207 PRs), Releases adapter, ASF roster adapter (`whimsy.apache.org/public/committee-info.json`, 49 PMC / 101 committers), `affiliations.yaml` seeded from email domains + GitHub `company` field (D6) | Org-concentration, bus-factor, elephant-factor, release-cadence metrics added and golden-tested; roster/affiliation data flows into `roster_entry`/`affiliation_period`; unresolved identities render as `unknown`, never guessed (D6) | Affiliation seeding is heuristic and will misclassify some contributors — must ship with the "PR your own entry" correction path (D6), not silently accepted as ground truth; GitHub PR pagination/rate-limit handling under real load |
| **M2 — Mailing-list metadata + real identity resolution** | Pony Mail metadata-only adapter (dev@/user@/commits@, sender/timestamp/thread structure — no body, per D1); full `identity_link`/unresolved-identity implementation (`ARCHITECTURE.md` §3, confidence tiers, `identity_overrides.yaml` review flow) | Thread/participation-shape metrics (not content — Phase 1 has no classifier) added; a documented, PR-reviewable process exists for merging two provisional identities; dev@ volume (1,200–3,000 msgs/yr per probe) collects without error across the full 2009–2026 archive | Pony Mail's `stats.lua` monthly-granularity API may require care to reconstruct thread relationships accurately from metadata alone; identity resolution is the highest-risk "quietly wrong forever" surface in the whole system — needs deliberate manual QA before M3, not just automated tests |
| **M3 — Full site IA + provenance + monthly freeze = Phase 1 complete** | Full site IA (`ARCHITECTURE.md` §8: per-dimension, per-metric pages with version history, `/reports/`, `/methodology/`, `/changelog/`, `/corrections/`); `monthly-edition.yml` freezing `/reports/YYYY-MM/`; run manifest + full `metric_value` provenance wired end to end; `backfill.yml` and `pr-checks.yml` workflows live | All 5 D4 dimensions render with real data and a baseline-vs-status computation; every metric value resolves to its provenance chain (source snapshot, code SHA, population, exclusions); a metric-definition-version bump has been exercised at least once end-to-end (full-history recompute + changelog entry, per D2 rule 6); reproducibility and schema-validation CI gates are green on every PR | Site-generation complexity (Jinja2 + Vega-Lite across ~30+ pages) is a real engineering lift, not just config; "insufficient data" rendering for small-N cases must be implemented before this counts as done, not deferred; GitHub auto-disables scheduled workflows on a public repo after 60 days of no repository activity, and does not define "activity" — mitigated per `ARCHITECTURE.md` §7.1 by the monthly-edition workflow's `main`-branch commit (a second heartbeat independent of the `data` branch), a data-freshness/staleness banner computed from the run manifest, and a documented manual `workflow_dispatch` re-enable step; this must ship as part of M3, not assumed away |

**Phase 1 complete** = M3's exit criteria met. This is the point at which `docs/research.md`'s "nightly
dashboard plus a frozen monthly report" (D1) is real and live, using only deterministic metrics — no
classification anywhere yet.

---

## 2. Phase 2a gates — classification

Phase 2a (dev@/user@, JIRA comments, GitHub PR comments — D7) does not start on a calendar date; it starts
when these gates clear, per D1's explicit gating language ("begins only after a human-labeled benchmark
corpus exists and the classifier clears agreement/precision gates against it"):

- **Benchmark corpus exists**: a frozen, human-labeled set of threads spanning dev@, user@, JIRA comments,
  and GitHub PR comments, large enough and diverse enough (across time periods, thread lengths, and — as far
  as can be judged — contentiousness) to support the taxonomy `COMMUNITY-HEALTH.md` defines. Corpus
  construction and labeling guidelines are that document's job; this gate only asserts the corpus must exist
  and be frozen (versioned, immutable) before any classifier training/prompting work is evaluated against it.
- **Multiple independent human raters, with measured inter-rater agreement** on the benchmark corpus, above
  a threshold — `COMMUNITY-HEALTH.md` §6.3 now specifies this: Krippendorff's alpha, computed per label, with
  α ≥ 0.667 as the minimum to use a label's human annotations as ground truth at all and α ≥ 0.80 as the
  target before that label's metric publishes unqualified. Not an open question any longer; see §5 for what
  remains open in this area (who rates, §5 item 3).
- **Classifier precision/recall/F1 against the benchmark corpus**, per taxonomy category, above a threshold
  — `COMMUNITY-HEALTH.md` §6.4 now specifies this per label group (precision/recall/F1 floors, e.g. ≥0.85
  precision for `personal_attack`/`hostility`/`gatekeeping`). Not an open question any longer.
- **Calibration and false-positive analysis** on the benchmark corpus, specifically for the categories D2
  rule 4 treats as most sensitive to weaponization (`personal_attack`, `hostility`, `gatekeeping`) — false
  positives on these categories carry more real-world cost than on `technical_disagreement`, so a uniform
  precision bar across all categories may be wrong; `COMMUNITY-HEALTH.md` §6.4 already answers this with
  asymmetric, stricter gates for the highest-stakes label group. Not an open question any longer.
- **A drift-monitoring plan exists** before the classifier runs against live data — not just a one-time
  validation, since research.md explicitly calls out "classifier drift" and "model-version comparisons" as
  things to watch, and D2 rule 6 requires that a classifier-version bump triggers a visible recompute, which
  presupposes the tooling to detect when a bump has happened (prompt change, model upgrade) and treat it as
  one.

**Deliverables for this milestone**: the frozen benchmark corpus (data + labeling methodology doc), an
inter-rater-agreement report, a classifier-vs-corpus precision/recall/F1/confusion-matrix report, the
Anthropic Message Batches API integration (`ARCHITECTURE.md` §6) wired to real (not just benchmark) data but
gated off by a feature flag until the gate report is reviewed and accepted.

**Risks**: benchmark corpus construction is genuinely hard research work, not engineering — it can easily
become the longest single milestone in the roadmap; there's a real risk of the classifier clearing agreement/
precision bars on the frozen corpus but drifting or degrading on live traffic in ways the corpus didn't
anticipate (topic shift, new slang, a contentious release cycle unlike anything in the corpus) — the
drift-monitoring plan exists specifically to catch this, but "exists" isn't the same as "proven," so early
Phase 2a output should probably ship with a visible "recently enabled, under observation" flag on the site
rather than presented with the same confidence as Phase 1's deterministic metrics.

---

## 3. Phase 2b gates — ASF Slack

Per D1, Phase 2b is gated independently of Phase 2a and requires **both**:

- **PMC consensus on dev@**: a proposal sent to `dev@cassandra.apache.org` describing exactly what the Slack
  integration will and won't do (read-only bot, aggregate-only output, no raw text ever persisted — D1),
  with the thread showing PMC consensus to proceed. This is a community-engagement step, not an engineering
  one — see §5.
- **ASF Infra approval** of a read-only bot on the specific public channels named in D1 (`#cassandra`,
  `#cassandra-dev`), via whatever process ASF Infra requires for bot approval (an INFRA JIRA ticket is the
  likely mechanism, but this should be confirmed with Infra directly rather than assumed here — flagged as
  an open question, §5).

**Exit criteria before enabling**: both approvals obtained and recorded (linked from `/methodology/`); the
"never persists raw text" architectural enforcement (`ARCHITECTURE.md` §6 — no `slack_message` table, no
per-message classification row, aggregator-only write path) has been code-reviewed specifically for this
property, not just implemented; minimum-sample-size suppression is verified working before any real Slack
aggregate is published (a small, low-traffic channel/window must render "insufficient data," not a
low-confidence real number).

**Risks**: this is the phase most likely to stall on factors outside the project's control (PMC and Infra
timelines); the "never persist raw text" guarantee is a hard requirement with real reputational and trust
consequences if violated even once, so this phase should have the most conservative rollout of any milestone
(a private-owner-only staging run before any public-facing aggregate ships, even after both approvals land).

---

## 4. Phase 3 — Kafka (expansion)

**Deliverables**: `projects/kafka.yaml` (per the schema in `ARCHITECTURE.md` §2.1 — JIRA project key `KAFKA`,
mailing-list domain `kafka.apache.org`, a reviewer-extraction strategy suited to Kafka's actual commit/review
convention, which is very likely `github_pr_review`-based rather than Cassandra's commit-trailer convention
and needs its own short data probe before this milestone starts, not an assumption carried over from
Cassandra); any new adapter code only if Kafka's source shape differs structurally from what Cassandra
needed (config-only is the target, per D9).

**Exit criteria**: Kafka's dashboard computes the same D4 dimensions Cassandra's does, using only
`projects/kafka.yaml` plus (at most) new adapter *classes* registered by config — and a concrete check that
`core/` required zero Cassandra-specific edits to support it (e.g., a CI grep asserting no literal
`"cassandra"` or `"CASSANDRA"` string appears outside `projects/cassandra.yaml` and test fixtures). If
supporting Kafka requires editing shared metric/identity/provenance code, that's a finding that the
"project-agnostic core" claim in D9/`ARCHITECTURE.md` §2 wasn't actually true, and the roadmap treats fixing
that as this milestone's real deliverable, not a footnote.

**Risks**: Kafka's commit/review conventions are almost certainly not Cassandra's trailer format, which is
the one piece of `ARCHITECTURE.md` most explicitly designed around a Cassandra-specific string pattern — this
is the milestone most likely to surface an accidental core/adapter boundary violation, which is exactly why
it exists as the Phase 3 proof rather than being skipped.

---

## 5. Open questions for the project owner

1. **Resolved** — inter-rater agreement threshold for the Phase 2a benchmark corpus: `COMMUNITY-HEALTH.md`
   §6.3 sets Krippendorff's alpha, α ≥ 0.667 minimum / α ≥ 0.80 target. No longer open; see §2 above.
2. **Resolved** — precision/recall/F1 thresholds per taxonomy category for Phase 2a: `COMMUNITY-HEALTH.md`
   §6.4 sets per-label-group floors, stricter for `personal_attack`/`hostility`/`gatekeeping` than for
   lower-stakes labels. No longer open; see §2 above.
3. **Who rates the benchmark corpus?** Community volunteers (e.g., PMC members) bring domain expertise but
   also potential bias (they may be participants in the very threads being labeled); paid/independent raters
   avoid that but lack context. Not resolved by this roadmap.
4. **Identity-merge confidence threshold specifics**: `ARCHITECTURE.md` §3 defines `exact`/`high` as
   auto-mergeable and `medium`/`low` as requiring manual review, but doesn't enumerate exactly which
   heuristics map to which tier (e.g., is a GitHub-noreply-email → login mapping "exact" or "high"?) — needs
   the owner's sign-off before M2.
5. **When commit-trailer and JIRA-field reviewer attributions disagree** (`ARCHITECTURE.md` §3.1 notes they
   aren't guaranteed to agree), does reviewer-concentration use the union, the intersection, or prefer one
   source over the other? Affects the HHI metric's actual formula, which belongs in `METRICS.md` but needs
   the owner's steer.
6. **Bot-pattern edge cases**: automation accounts that commit on behalf of a human (e.g., a release-manager
   script run by a person, vs. a fully automated dependency bot) — where's the line, and who maintains the
   `bot_patterns` list as new edge cases surface?
7. **ASF Infra's actual process** for approving a read-only Slack bot on public channels — needs to be
   confirmed directly with Infra rather than assumed (flagged in §3); is it an INFRA JIRA ticket, a direct
   request, something else, and what's the typical timeline?
8. **Anthropic API cost ownership**: who pays for Phase 2a's Batches API usage, is there a monthly cap, and
   what happens to the pipeline if a cap is hit mid-run (partial classification with a visible gap, or a hard
   stop)?
9. **Retention of superseded metric-definition versions**: kept forever (append-only, per the architecture's
   default) or pruned after some period? Affects long-term `data` branch/site growth, though not urgently at
   current projected sizes (`ARCHITECTURE.md` §4.2).
10. **Corrections/appeals SLA**: is there a committed response time for the `/corrections/` page (research.md
    calls for an appeals process; D2/D4 don't set an SLA), and who — just the project owner, or a wider
    reviewer group — adjudicates disputes?
11. **Minimum sample size** for "insufficient data" suppression (D2 rule 5) — what's the actual N threshold,
    and does it vary by metric (e.g., a small PMC of 49 people means some org-concentration slices will be
    small by nature, not just early-data-collection small)?

---

## 6. Community-engagement steps

These are sequenced against the milestones above, not bundled into a single "launch" event — each is a
distinct point where the project should talk to the Cassandra community before proceeding, not after:

- **After the M0 prototype exists, before Phase 1 publish (end of M3)** (D11): announce intent and scope on
  `dev@cassandra.apache.org` and point to the working prototype. The repo is public but not promoted before this. Cover what the
  site measures, what it explicitly doesn't (no composite score, no individual attribution, per D2 rule 4/7),
  where the code and data live, and how to file a correction. This is the first time Cassandra's community
  sees the project; it should not be a surprise announcement after the fact.
- **At Phase 1 publish**: invite corrections via the `/corrections/` page and a visible link from every
  metric page (`ARCHITECTURE.md` §8) — the appeals path should exist from day one of publication, not be
  added reactively after the first complaint.
- **Before Phase 2a benchmark-corpus labeling begins**: a second dev@ note specifically about the
  classification effort — what's being labeled, by whom (pending resolution of open question #3), and an
  invitation for community members to review the taxonomy in `COMMUNITY-HEALTH.md` before it's frozen as the
  basis for a benchmark corpus.
- **Before Phase 2a goes live on the public site**: a third dev@ note once the gates in §2 clear, showing the
  agreement/precision report, before classified metrics appear on the live dashboard — giving the community a
  chance to react to the validation evidence, not just the resulting numbers.
- **Before Phase 2b (Slack) is enabled**: the PMC-consensus dev@ thread required by the gate itself (§3) *is*
  this engagement step — it shouldn't be treated as a formality vote, but as the actual venue where channel
  scope, retention, and the "never persist raw text" guarantee are discussed and can be pushed back on.
- **At each metric-definition-version bump post-launch**: the changelog entry (`ARCHITECTURE.md` §4.4) is
  the standing mechanism, but a definition change large enough to visibly move a dimension's status
  (`improving`/`declining`) should also get a dev@ heads-up before it goes live, not just a changelog line
  discovered after the fact.
- **Before Phase 3 (Kafka)**: a lighter-touch outreach to Kafka's own community (not Cassandra's) is worth
  considering once Kafka is in scope, though this roadmap treats it as a discretionary courtesy rather than a
  hard gate the way Phase 2a/2b are gated — flagged for the owner's judgment, not decided here.
