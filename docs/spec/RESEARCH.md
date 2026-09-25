# RESEARCH.md — Literature and Existing-System Review

Research deliverable #1 for the cassandra-project-health project (see `docs/research.md` for the
original brief and `docs/spec/DECISIONS.md` for binding decisions this document must stay
consistent with). This document surveys existing project-health systems, academic literature on
OSS sustainability, and academic/tooling literature on toxicity and disagreement in software
communities, and evaluates each against eight fixed questions:

1. What does it measure?
2. What data does it require?
3. How is the metric calculated?
4. Is the calculation deterministic?
5. Is there empirical validation?
6. What are its limitations?
7. Can it be reproduced from publicly available data?
8. What ideas should this project adopt or avoid?

Every substantive claim is labeled **established** (directly verified against a primary source),
**reasonable proxy** (a defensible stand-in for something harder to measure directly, with some
supporting evidence), **experimental** (plausible but unvalidated, or validated only in a narrow
context), or **subjective** (a judgment call, consensus opinion, or unfalsifiable framing). Claims
that could not be verified during this research pass are flagged explicitly rather than presented
as fact — several are, because primary sources were paywalled, PDFs failed to extract, or a claim
in a secondary source could not be traced back further.

**Research method:** This document was compiled from four parallel research passes — CHAOSS/CNCF/
LFX Insights; ASF community-health practice, GitHub, and OpenSSF Scorecard; academic sustainability
and contributor-dynamics literature; and academic/tooling literature on toxicity, sentiment, and
disagreement — each conducted via live web search and direct fetch of primary sources (official
documentation, GitHub repositories and source code, paper PDFs/abstracts, arXiv preprints). Several
of the resulting claims were independently re-verified by fetching the primary source a second
time (noted inline where done). Today's date for currency purposes is **2026-09-25**; several
findings below describe changes that happened in 2026 and would not be reflected in older summaries
of these systems.

---

## 1. CHAOSS (Community Health Analytics for Open Source Software)

CHAOSS is a Linux Foundation project producing an open metrics catalog plus (historically) two
associated software implementations, Augur and GrimoireLab. It operates through GitHub-hosted
working-group repos (`chaoss/wg-*`) and a knowledge base at chaoss.community/kb.

### 1.1 Metrics catalog

Verified individual metric definitions, sourced from chaoss.community/kb:

- **Contributor Absence Factor** (renamed from "Bus Factor"): smallest number of contributors
  responsible for 50% of total contributions. [source](https://chaoss.community/kb/metric-bus-factor/)
- **Elephant Factor**: minimum number of companies whose employees together make a specified share
  (commonly 50%) of commits — the organizational analogue of bus factor.
  [source](https://www.chaoss.community/kb/metric-elephant-factor/)
- **Organizational Diversity** (metrics model): bundles Number of Contributing Organizations,
  Elephant Factor, and Affiliation Diversity. [source](https://www.chaoss.community/kb/metric-organizational-diversity/)
- **Time to First Response**: time between an activity (issue/change request) opening and its
  first human response. [source](https://www.chaoss.community/kb/metric-time-to-first-response/)
- **Change Request Closure Ratio**: ratio of change requests opened vs. closed in a period.
  [source](https://chaoss.community/kb/metric-change-request-closure-ratio/)
- **Committers**: `Number_of_committers = distinct_contributor_ids` over a date range.
  [source](https://chaoss.community/kb/metric-committers/)
- **OSS Project Viability** (metrics model): aggregates Compliance+Security, Governance, Community
  Engagement, and Strategy. [source](https://chaoss.community/kb/metrics-model-oss-project-viability-community/)

**Established.** These formulas are published and plain-language. **Could not verify:** standalone
KB pages titled exactly "Types of Contributions" or "Contributor Retention" — these concepts appear
folded into other metrics/models rather than existing as independently numbered CHAOSS metrics.

### 1.2 CHAOSS working groups

Per the [CHAOSS Community Handbook](https://handbook.chaoss.community/community-handbook/community-initiatives/working-groups):
Diversity, Equity & Inclusion (DEI — the first WG, founded 2017, covering seven diversity areas);
Evolution (code/project evolution metrics — the handbook's own language indicates future work is
folding into the newer Common Metrics WG); Risk (compliance/risk metrics, including elephant and
bus factor); Value (metrics on the value of OSS engagement — the handbook states this discussion
has moved to the OSPO working group); Common Metrics (cross-cutting consolidation point).
**Established** that these WGs exist and are described this way in the handbook; **could not
verify** current (2026) meeting/commit cadence for Risk or Value specifically — the handbook's own
wording suggests contraction, not five uniformly active parallel groups.

### 1.3 Augur — archived as of July 2026 (established, directly verified)

This is a materially important, recent finding. On **2026-03-29**, Augur co-founder Sean Goggins
unilaterally removed the `chaoss/augur` repository from the CHAOSS GitHub organization, moved it to
a separate org ("AugurLabs"), and resigned from the CHAOSS Board — without consulting other
maintainers or the Board beforehand, communicating the move via Slack and a personal Mailchimp
account presented as official CHAOSS communication. The CHAOSS Board's own statement is more
measured than this summary suggests: it says only that they are "hopeful a path forward can be
found, whether that means Augur stays with CHAOSS or continues development independently," and that
Goggins "remains an active member and contributor to the CHAOSS project"
([Public Update on Recent Events at CHAOSS](https://chaoss.community/public-update-on-recent-events-at-chaoss/),
dated 2026-04-13). The `chaoss/augur` GitHub repository was subsequently **archived (read-only) on
2026-07-23**; its README now reads *"The Augur project is no longer part of CHAOSS. Use CollectOSS
instead!"* — directly re-verified by fetching the live repo page. CHAOSS has stood up
**`chaoss/CollectOSS`**, a fork-based successor aiming for "drop-in" migration.
[chaoss/augur (archived)](https://github.com/chaoss/augur)

**Implication for this project:** Augur is a dead/archived dependency as of September 2026.
CollectOSS is young and unproven. This project should not plan to depend on Augur, and should treat
CollectOSS as experimental rather than a stable foundation.

### 1.4 GrimoireLab — appears actively maintained (established, with caveats)

GrimoireLab (`chaoss/grimoirelab`), Bitergia's toolkit donated to CHAOSS in 2017, comprises
**Perceval** (data-collection connectors for git, GitHub, GitLab, Gerrit, Jira, mailing lists, etc.),
**SortingHat** (identity/affiliation management), and a GrimoireELK/Kibiter/Grafana-based
visualization layer. Evidence of recency: `chaoss/grimoirelab-sirmordred` updated 2026-07-24;
`chaoss/grimoirelab-sortinghat` shows release candidates as recent as September 2026. This is
**reasonable proxy** evidence of maintenance (dependency bumps and RC releases are lower-effort
signals than substantive feature work, but the repo is not abandoned). GrimoireLab now underpins
LFX Insights (see §3), which gives it institutional maintenance incentive Augur lacked.
**Could not verify** GrimoireLab's current governance ownership — whether it remains
community-governed under CHAOSS or is now effectively an LF/Bitergia-internal component.

### 1.5 SortingHat — identity resolution (established, directly relevant to this project)

SortingHat distinguishes raw **identities** (name/email/username tuples from a data source) from
consolidated **individuals** (UUID-keyed people), with time-bounded organizational **enrollments**.
Its documented merge mechanism is a **human-driven UI action** — drag-and-drop, multi-select, or a
"workspace" gather-and-merge flow, each with a confirmation dialog
([tutorial](https://chaoss.github.io/grimoirelab-tutorial/docs/sortinghat/profiles/merge/)). The
tutorial material does not describe a silent/automatic merge path. **Could not verify**: the
similarity/matching heuristic behind merge-candidate suggestions, or whether any large-scale
deployment (e.g., inside LFX Insights, resolving identities across 13,000+ projects) auto-applies
merges without a human click — that would be a materially different, riskier mode than the
single-project UI implies. Treat that gap as unresolved, not settled either way.

**Comparison to this project's stance:** DECISIONS.md D2.5 ("identities below a confidence
threshold are not merged") is compatible with, and arguably stricter than, what's documented of
SortingHat's UI, which itself requires an explicit human action to merge. Where this project should
be more cautious than SortingHat's *documented* behavior is on the unverified bulk/automated
pathway a large-scale deployment might use.

### 1.6 CHAOSS — 8-question answers

1. **Measures:** contributor activity, retention, and diversity; response/turnaround times;
   organizational concentration of contribution.
2. **Data required:** VCS history, issue/PR/change-request metadata; organizational metrics
   additionally require an identity-to-organization mapping.
3. **Calculation:** published, mostly-arithmetic formulas per metric (established).
4. **Deterministic:** formulas are deterministic given fixed inputs and a fixed identity mapping;
   the mapping is human-curated and can change over time, so re-running later can shift
   organizational breakdowns for the same historical period (reasonable proxy inference, not an
   explicit CHAOSS claim).
5. **Empirical validation:** none found for metric *usefulness* (e.g., no study showing Elephant
   Factor thresholds correlate with project outcomes). Treat as consensus-based, not empirically
   validated, absent a specific cited study.
6. **Limitations:** catalog breadth outpaces maintained implementations now that Augur is gone;
   identity/affiliation quality is a manual bottleneck; working-group activity is uneven.
7. **Reproducible from public data:** yes in principle — formulas are public and primary data
   (git logs, forge APIs) is public for public repos; the blocking dependency is a maintained
   collection+identity pipeline, currently in flux.
8. **Adopt/avoid:** adopt the published metric vocabulary/formulas (Elephant Factor, Contributor
   Absence Factor, Change Request Closure Ratio, Time to First Response) as a reference; adopt
   SortingHat's human-confirmed-merge principle as validation that "never silently merge" is
   industry-credible, not overcautious. Avoid depending on Augur or assuming CHAOSS's reference
   tooling is stable — it fractured in 2026.

---

## 2. CNCF (Cloud Native Computing Foundation) project-health practice

### 2.1 DevStats — appears operational, not confirmed deprecated

`cncf/devstats`: Go + PostgreSQL + Grafana, ingesting GH Archive events, the GitHub API, and git
itself, running on Kubernetes with Postgres+Patroni for HA. Computes per-project and cross-project
dashboards sliceable by company (via gitdm — §2.3) and country.
[DASHBOARDS.md](https://github.com/cncf/devstats/blob/master/DASHBOARDS.md). No formal deprecation
announcement was found; a CNCF blog post dated 2026-03-12
(["Japan's CNCF DevStats 2025"](https://www.cncf.io/blog/2026/03/12/japans-cncf-devstats-2025/))
actively uses fresh DevStats data. **Reasonable proxy** for "still operational as of Q1 2026" — exact
current commit velocity was not independently confirmed. CNCF's October 2025 relaunch announcement
for LFX Insights does not mention DevStats being replaced or sunset; the two currently appear to
coexist, though this coexistence is not guaranteed to be permanent.

### 2.2 CNCF TOC graduation criteria (established)

The canonical criteria live in
[`template-graduation-application.md`](https://github.com/cncf/toc/blob/main/.github/ISSUE_TEMPLATE/template-graduation-application.md)
(the older standalone `graduation_criteria.md` now redirects there). Verified requirements:
project maintainers from **at least 2 organizations** demonstrating survivability; a documented
maintainer lifecycle (onboarding/offboarding/emeritus) with maintainers listed by name, contact,
domain of responsibility, and **affiliation**; a contributor ladder shown to have produced
maintainers from more than one organization; an OpenSSF Best Practices **passing** badge (an
earlier search-summarized source suggested "silver/gold" — that is **not confirmed** by the primary
template text and should be treated as unverified/possibly outdated) plus a completed third-party
security review; adoption evidence from at least 3 independent adopters, typically 5–7 submitted via
questionnaire. There is no numeric *contributor*-count or organizational-diversity threshold beyond
the "≥2 organizations" maintainer rule. Real applications are visible as worked examples, e.g.
[Crossplane #1788](https://github.com/cncf/toc/issues/1788).

### 2.3 cncf/gitdm — organizational affiliation mapping (established, directly relevant)

`cncf/gitdm` (forked from Greg Kroah-Hartman's Linux-kernel `gitdm`) is the closest existing
precedent to this project's `affiliations.yaml` design (DECISIONS.md D6). Its data format,
in priority order:

1. **`email-map`** — per-individual email-to-employer override (highest priority).
2. **`domain-map`** — domain-based fallback (e.g. `@redhat.com` → Red Hat); generic public-email
   domains get a trailing `*` (e.g. `gmail.com` → "Gmail *"), signaling "domain-matched, not
   individually confirmed."
3. **`aliases`** — consolidates multiple emails belonging to one person (gitdm's own simpler
   identity-merge mechanism).
4. Special-case/group files for rarer overrides.

Unmapped developers are listed under an explicit **"Developers with unknown affiliation"** section
in output, with uncertain matches tracked separately in `uncertain.csv` — an explicit, visible
unknown bucket rather than a forced guess. Corrections happen via **pull request** against the repo
(e.g. [PR #1264](https://github.com/cncf/gitdm/pull/1264)), so the mapping is versioned and
auditable through git history. Affiliation data is imported into DevStats roughly **every 4 weeks**.
A known limitation: affiliations are a point-in-time snapshot tied to whichever emails appear in
historical commits, so historical commits can show a stale employer unless retroactively corrected —
the tool's design assumes affiliation history must be time-bounded, not just "current employer."

**Direct confirmation for this project:** gitdm's individual-override → domain-fallback → explicit
unknown bucket → PR-based correction pattern is structurally identical to this project's planned
`affiliations.yaml` approach. DECISIONS.md D6 is a precedented pattern, not a novel invention.

### 2.4 CNCF/TOC — 8-question answers

1. **Measures:** DevStats — raw contribution activity by project/company/country. TOC review —
   governance maturity, maintainer/org diversity, security-practice adoption, real adoption.
2. **Data:** GH Archive/GitHub API/git (DevStats); gitdm mapping files; self-reported maintainer
   lists, adopter interviews, OpenSSF badge status (TOC).
3. **Calculation:** DevStats — SQL aggregation over ingested events, rendered in Grafana (arithmetic).
   TOC — checklist/threshold review, not a computed score.
4. **Deterministic:** DevStats deterministic per gitdm-mapping-version (mapping revised ~monthly, so
   re-running later can change historical org breakdowns). TOC criteria are a committee judgment
   against documented thresholds, not a formula.
5. **Empirical validation:** not found for DevStats' specific metric choices. TOC criteria are
   consensus-derived through CNCF governance process, not empirically validated against outcomes —
   explicitly subjective/consensus by CNCF's own process design.
6. **Limitations:** DevStats depends on GH Archive completeness and gitdm currency; gitdm has a
   manual-curation bottleneck and can carry a large "unknown" bucket for less corporate-backed
   projects; TOC thresholds are coarse (binary "≥2 orgs") and don't capture degree of concentration.
7. **Reproducible from public data:** yes — one of the strongest stories among systems reviewed;
   DevStats source, GH Archive, and gitdm mapping files are all public and versioned.
8. **Adopt/avoid:** adopt gitdm's exact file-format pattern for `affiliations.yaml`; adopt TOC's
   practice of pairing quantitative thresholds with qualitative adopter review. Avoid treating
   DevStats' org-diversity breakdowns as point-in-time-stable — document/snapshot the mapping
   version used. Avoid hard-depending on DevStats' long-term roadmap.

---

## 3. LFX Insights

LFX Insights (insights.linuxfoundation.org) evaluates "the health and trustworthiness" of projects
across a large catalog (13,000+ to 15,000+ depending on which page is cited), spanning both
LF-hosted and external "critical" projects selected partly via the OpenSSF Criticality Score.

**Health Score formula — directly fetched, with an important discrepancy found and resolved.** A
direct fetch of the current methodology page gives:

> "Health Score (0–100) = Maintainer Health (0–40 pts) + Security and Supply Chain (0–35 pts) +
> Development Activity (0–25 pts)"

with a documented recent change: "The 5 points formerly reserved for Supply Chain Integrity have
been permanently reallocated to the other sub-signals; the category total remains 35 pts" (Supply
Chain Integrity assessment is still under development and currently blocked for all projects). This
**directly supersedes** an older methodology also findable in search results —
"Contributors (25) + Popularity (25) + Development (25) + Security & Best Practices (25)," an
equal-weight four-category mean. **This is itself a finding worth stating plainly: LFX Insights has
materially changed its scoring formula, and its public documentation does not carry a version number
or changelog that would let someone reproduce a historical score.** Re-verified by direct fetch of
[insights.linuxfoundation.org/docs/metrics/health-score](https://insights.linuxfoundation.org/docs/metrics/health-score).

Organizational diversity is covered ("Insights resolves contributor identities and affiliations...
to show which organizations are truly backing a project"), but the identity-resolution algorithm is
not documented in any page reached during this research. **Open source / reproducible:** LFX
Insights is a **hosted, proprietary platform** — no public source repository for the platform
itself. One input it uses, the **OpenSSF Criticality Score**, is independently open and documented
([ossf/criticality_score](https://github.com/ossf/criticality_score)), so part of its methodology
(project selection) is reproducible even though the full scoring pipeline is not.

**Relationship to CHAOSS:** established architectural lineage — "LFX Insights is the largest
platform to have ever been built on top of the GrimoireLab tool"
([PR Newswire](https://www.prnewswire.com/news-releases/grimoirelab-grows-up-to-power-the-linux-foundations-lfx-insights-platform-301173642.html)).
**Could not verify** the specific claim that "LFX Insights implements CHAOSS metrics" — no page
fetched during this research (health-score docs, LF OSS Index docs, the CNCF October 2025 relaunch
announcement) mentions CHAOSS at all. The substantiated connection is shared GrimoireLab ancestry,
not a one-to-one metric-definition mapping.

### 3.1 LFX Insights — 8-question answers

1. **Measures:** a composite 0–100 Health Score (maintainer health, security/supply-chain,
   development activity per the current formula), plus organizational-affiliation/contributor
   breakdowns and adoption/popularity signals.
2. **Data:** GitHub and other forge data, OpenSSF Criticality Score inputs, LF's own contributor/
   affiliation resolution across 15+ integrated sources.
3. **Calculation:** weighted point-scale composite per documented category.
4. **Deterministic:** unverified/unstable — the methodology changed at least once without a visible
   version history reachable from the docs; do not assume a historical score is reproducible.
5. **Empirical validation:** none found.
6. **Limitations:** closed/hosted; methodology description inconsistent across LF's own pages;
   identity-resolution algorithm undocumented; no confirmed CHAOSS-metric equivalence.
7. **Reproducible from public data:** partially — raw GitHub activity is public and the Criticality
   Score input is fully open, but the full scoring pipeline, weights, and identity-resolution logic
   are not published as code; the overall Health Score is not independently reproducible.
8. **Adopt/avoid:** adopt the idea of a composite, category-weighted score as a *presentation layer*
   over transparent, versioned sub-metrics — the opposite of what LFX Insights currently does.
   Avoid LFX Insights' apparent lack of a stable, versioned public methodology; this project should
   publish its scoring formula with an explicit version and changelog (this is exactly what
   DECISIONS.md D2.6 already requires). Avoid presenting a composite score as objective/authoritative
   while the hardest part — identity/affiliation resolution — stays opaque, as LFX Insights currently
   does.

---

## 4. Apache Software Foundation (ASF) community-health practice

### 4.1 ASF board reporting (established, directly verified)

Per [apache.org/foundation/board/reporting](https://www.apache.org/foundation/board/reporting):
PMCs **must** report quarterly to the Board (or after 3 monthly reports if newly graduated/
recovering from an "at risk" flag). The PMC chair is ultimately responsible; reports are due at
least a week before the relevant board meeting. Required content: project description; project
state (New / Ongoing-with-activity-level / Dormant / At Risk / considering Attic); board issues
needing attention; release history with dates. Recommended-but-not-templated content: community
health (activity levels, contributor engagement, new-member elections), future plans, and diversity
concerns if organizational-representation issues exist. The guidance explicitly warns these bullets
"are **not** a template" to be copy-pasted — reports are meant to be written prose, judged by
readers (board members), not computed.

### 4.2 reporter.apache.org (established, partially verified — auth-gated)

The **Apache Committee Reporter** tool generates a report-drafting scaffold for PMC chairs
("Post Report" workflow), explicitly described as something that "can simplify gathering data for
your report, but you must edit and add information from the generated template" — i.e., it
auto-populates a starting structure but does not itself compute or assert a health status; a human
still writes the substantive content. **Could not independently verify** the exact underlying data
sources it pulls into that template (commits, mailing-list volume, JIRA activity) — the tool's live
interface returned HTTP 401 (requires ASF committer authentication) during this research, so this
detail rests on the reporting-guidance page's description rather than direct inspection of the tool.

### 4.3 Whimsy (whimsy.apache.org) — governance bookkeeping, not health metrics (established)

Whimsy provides board minutes (collated, with anchors), an org chart, the Incubator podlings
directory, JSON data exports, and **website health checks for top-level projects and podlings**
(link/build validity, not community-health scoring) as public tools; committer-access tools for
roster/mailing-list management; officer/member tools (members' meeting info, CLA tracking);
secretary tools (ICLA validation, LDAP verification, name-consistency checks). Whimsy is explicitly
described as "a set of unnecessary yet highly useful applications" for "organizational information
about the ASF" and automating "corporate processes." **Established:** no evidence found that Whimsy
computes project-health metrics of any kind — it is governance/administrative bookkeeping. The
publicly-available `committee-info.json` and podling-status JSON exports it produces are, however, a
useful machine-readable source of PMC roster and podling-status data for this project's own
collectors.

### 4.4 The Apache Way / Incubator Maturity Model (established — and a genuine, important gap)

The canonical incubation policy document
([incubator.apache.org/policy/incubation.html](https://incubator.apache.org/policy/incubation.html))
is a **procedural checklist**, not a maturity-scoring model: monthly reports for the first three
months tapering to quarterly, disclaimer requirements, branding/publicity restrictions, and release
voting procedures (PPMC + IPMC approval). It does **not** operationalize meritocracy,
consensus-decision-making, or diversity as measured dimensions — those are Apache Way *values*,
invoked in board/mentor discretion and community norms, not formulas. Where podlings deviate, the
mechanism is qualitative Incubator PMC judgment ("MAY, at their discretion, ask a Podling to report
more frequently"), not a scored gate. **This is directly relevant to this project's core premise:**
ASF itself does not have a computed "health model" to borrow — its "maturity model" is a compliance
checklist plus human judgment, reinforcing that this project's proposed evidence-based metrics are
filling a real gap in ASF's own practice, not duplicating an existing ASF system. (Separately, the
academic literature — Yin et al., §5.1 — has attempted to *predict* incubator graduation from
activity+communication data, which is the closest thing to a quantitative maturity model in this
space, and it is academic, not ASF-official.)

### 4.5 Apache Cassandra's own board reports (established, directly verified — high practical value)

Fetched directly from Whimsy's collated board minutes for Cassandra
([whimsy.apache.org/board/minutes/Cassandra.html](https://whimsy.apache.org/board/minutes/Cassandra.html)).
Recent entries (2026) show real board-report content and cadence:

- **2026-08-19:** 24 PMC members responding to roll call; releases across the Cassandra server plus
  Java/NodeJS/Python drivers and analytics tools; a Community track secured at Community over Code EU
  2026.
- **2026-05-20:** +1 PMC member, +3 committers; releases across server and multiple driver
  implementations; first alpha of Cassandra 6.0; strong CoC EU submission participation.
- **2026-02-18:** +1 PMC member; releases across server, Java drivers, and analytics; approaching
  6.0 alpha; received software grants from DataStax for Python/C++/Node.js/C# drivers.

Recurring themes across quarters: PMC roster size and "more than the requisite three members
available for rapid [security] response" as an explicit self-reported bus-factor-adjacent statement;
release cadence across the multi-repo driver ecosystem, not just the server; conference-track
presence as a community-engagement signal; explicit language about "deliberate diversification and
expansion of our contributor base" as an ongoing, self-acknowledged concern. This confirms two
things for this project directly: (a) Cassandra's PMC already self-reports informal versions of
several metrics this project plans to formalize (contributor growth, release cadence, bus-factor-
like security-response capacity, organizational diversification); (b) these reports are unstructured
prose, not structured data, so this project's dashboard would be a genuine upgrade in rigor and
auditability over the status quo, not a duplicate of it. The board reports are a legitimate
**citation target** once this project has data worth citing back to the PMC.

### 4.6 ASF — 8-question answers (and comparison-table row)

1. **Measures:** project vitality via structured-but-prose quarterly self-report (state, releases,
   PMC/committer roster changes, self-identified risks); Incubator policy separately measures
   procedural compliance during incubation, not ongoing health.
2. **Data required:** entirely self-reported/manually authored by the PMC chair; reporter.apache.org
   provides an auto-populated *starting* template, but the content that matters is human-written.
3. **Calculation:** none — this is qualitative reporting reviewed by the Board, not a computed
   metric.
4. **Deterministic:** no — the closest thing to determinism is the required-field checklist
   (state, releases, board issues); everything substantive is prose.
5. **Empirical validation:** none — this is governance process, not a measurement instrument, and
   was never intended as one.
6. **Limitations:** self-report bias (a project can characterize itself favorably); no
   cross-project comparability by design; no historical trend visualization; not machine-readable
   beyond the required-field skeleton.
7. **Reproducible from public data:** the reports themselves are public (Whimsy board minutes), so
   this project can treat historical board reports as a *qualitative* cross-check/citation source,
   but the reports cannot be used as a source of *quantitative* metrics — they don't contain
   consistently structured numbers.
8. **Adopt/avoid:** adopt the practice of citing this project's own dated monthly reports back to
   dev@ and eventually to ASF board reports, closing the loop between this project's structured
   metrics and the PMC's existing qualitative narrative (this is already anticipated in
   DECISIONS.md D5). Adopt Whimsy's public JSON exports (committee-info, podling status) as a
   low-effort source of authoritative PMC/committer roster data. Avoid assuming ASF has an existing
   quantitative health model to align with or borrow from — it does not; this project is filling a
   real gap.

**Comparison-table row — ASF:**

| Primary purpose | Data sources | Deterministic? | Open source? | Identity resolution | Org-affiliation tracking? |
|---|---|---|---|---|---|
| Governance oversight via quarterly self-report (Board) and a procedural incubation checklist (Incubator), not a health-scoring system | Entirely self-authored prose by PMC chairs; reporter.apache.org provides an auto-populated template scaffold (exact underlying data sources not independently verified — tool is auth-gated); Whimsy JSON exports for roster/podling data | No — qualitative narrative reviewed by the Board, not a formula | Reports and Whimsy tooling are public; the *process* is public ASF governance, not a reusable software artifact | No formal mechanism — PMC rosters are authoritative membership records (LDAP-backed), not inferred/merged identities | Only informally, via prose mentions of "diversification of contributor base" — no structured per-org data published |

---

## 5. GitHub's own community/project metrics

### 5.1 Community Profile / Community Standards (established)

The checklist ([docs.github.com](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories))
checks for presence of README, CODE_OF_CONDUCT, LICENSE, CONTRIBUTING, issue templates (in
`.github/ISSUE_TEMPLATE`), and a security policy, producing a `health_percentage`. It is available
via the REST API: `GET /repos/{owner}/{repo}/community/profile` — **established**, directly
confirmed — returning `health_percentage`, per-file presence booleans, and an `updated_at`
timestamp (the repo cannot be a fork). This is a cheap, deterministic, fully reproducible presence
check.

### 5.2 GitHub Insights / traffic / statistics (established, with real quirks that matter for design)

- **Traffic API** (`/traffic/views`, `/traffic/clones`, `/traffic/popular/referrers`,
  `/traffic/popular/paths`): confirmed **14-day retention window** — "Get the total number of
  clones and breakdown per day or week for the last 14 days," directly re-verified from
  [docs.github.com/en/rest/metrics/traffic](https://docs.github.com/en/rest/metrics/traffic).
  Requires push access to the repository, so this data is not fetchable for arbitrary third-party
  repos including `apache/cassandra` unless the collector has write access — **this project cannot
  rely on GitHub traffic data for Cassandra at all**, since it has no push access to the ASF repo.
- **Statistics endpoints** (`/stats/commit_activity`, `/stats/code_frequency`,
  `/stats/contributors`, `/stats/participation`, `/stats/punch_card`): commit_activity gives one
  year of weekly commit counts; participation gives 52-week owner-vs-all commit counts; punch_card
  gives hour-of-day/day-of-week commit density. **Important limitation directly relevant to
  Cassandra**: `code_frequency` and `contributors` stats are explicitly documented as degraded for
  large repositories — `code_frequency` "can only be used for repositories with fewer than 10,000
  commits," and `contributors` "will return 0 values for all addition and deletion counts in
  repositories with 10,000 or more commits." Apache Cassandra's git history has run since roughly
  2009 and is well past 10,000 commits, so **these two specific endpoints should be assumed
  unreliable or empty for Cassandra** — a concrete, verified reason to prefer computing additions/
  deletions directly from `git log`/local clone rather than these two GitHub statistics endpoints.
  All statistics endpoints exclude merge commits (and `contributors` additionally excludes empty
  commits); results are cached by default-branch SHA and a push to the default branch invalidates
  the cache, and if data isn't cached yet the API returns **202** while a background job computes
  it — collectors must poll/retry, not treat a single request as authoritative.

### 5.3 GitHub — 8-question answers

1. **Measures:** presence of recommended community-health files (Community Profile); raw commit/
   contributor activity and (only where the collector has push access) traffic/popularity
   (Insights/statistics).
2. **Data required:** repository metadata and git history via REST/GraphQL API; traffic data
   additionally requires push-level repo access this project will not have for Cassandra.
3. **Calculation:** Community Profile — simple presence/percentage. Statistics — arithmetic
   aggregation over commit history, computed server-side and cached.
4. **Deterministic:** yes for a fixed default-branch SHA and fixed cache state; the 202-then-retry
   background-compute pattern means a *single* request is not guaranteed complete/authoritative.
5. **Empirical validation:** N/A — these are raw counts and presence checks, not derived health
   claims requiring validation.
6. **Limitations:** traffic data is 14-day, push-access-only, and thus unusable for this project's
   actual target repo; two of five statistics endpoints degrade/zero-out above 10,000 commits,
   directly affecting Cassandra; Community Profile only checks presence of files, not their quality
   or currency.
7. **Reproducible from public data:** Community Profile and the still-functional statistics
   endpoints (commit_activity, participation, punch_card) yes, for public repos, via unauthenticated
   or standard-token API access. Traffic: no, not for this project's target repo.
8. **Adopt/avoid:** adopt Community Profile's `health_percentage` fields as one cheap, deterministic
   input signal (file-presence hygiene), clearly labeled as a proxy for documentation hygiene, not
   community health. Adopt commit_activity/participation/punch_card where useful, but avoid
   code_frequency/contributors stats for Cassandra given the documented 10,000-commit degradation —
   compute additions/deletions and per-author breakdowns from a local git clone instead, which this
   project already plans to maintain (DECISIONS.md D3). Avoid planning around GitHub traffic data
   entirely for a repo this project doesn't own.

---

## 6. OpenSSF Scorecard — security posture, not community health

`github.com/ossf/scorecard` computes an automated security-posture score for a repository. This
section verifies precisely what it measures so this project can explain, with citations, why it is
**not** used as a community-health signal.

### 6.1 What it checks (established, directly verified from `docs/checks.md`)

19 checks, each scored 0–10, tagged with a risk level (Critical/High/Medium/Low) that determines its
weight in the aggregate:

| Check | Risk | Category |
|---|---|---|
| Binary-Artifacts | High | Security |
| Branch-Protection | High | Security |
| CI-Tests | Low | Process |
| CII-Best-Practices (OpenSSF Best Practices Badge) | Low | Process |
| Code-Review | High | Security/process hybrid |
| Contributors | Low | Community-adjacent |
| Dangerous-Workflow | Critical | Security |
| Dependency-Update-Tool | High | Security |
| Fuzzing | Medium | Security |
| License | Low | Process |
| Maintained | High | Community-adjacent |
| Packaging | Medium | Process |
| Pinned-Dependencies | Medium | Security |
| SAST | Medium | Security |
| SBOM | Medium | Security |
| Security-Policy | Medium | Security |
| Signed-Releases | High | Security |
| Token-Permissions | High | Security |
| Vulnerabilities | High | Security |
| Webhooks | Critical | Security |

13 of 19 checks are unambiguously security-focused (branch protection, dangerous workflows, token
permissions, known vulnerabilities, webhook config, pinned/verifiable dependencies, static analysis,
fuzzing, signed releases, security policy presence, SBOM, binary artifacts). Only **two** checks —
`Maintained` (has there been recent commit activity / is the project not abandoned) and
`Contributors` (does contribution come from more than one organization/individual, a coarse
proxy) — touch anything resembling community health, and both are still framed as *security-risk*
signals (an abandoned or single-maintainer project is a supply-chain risk, which is Scorecard's
actual concern) rather than as community-wellbeing measures.

### 6.2 Scoring methodology (established, directly verified)

"Each individual check returns a score of 0 to 10, with 10 representing the best possible score.
Scorecard also produces an aggregate score, which is a weight-based average of the individual
checks weighted by risk." Weight mapping: Critical = 10, High = 7.5, Medium = 5, Low = 2.5. The
project's own documentation explicitly warns: "aggregate scores in particular tell you nothing about
what individual behaviors a repository is or is not doing," since different combinations of checks
can produce the same overall number — Scorecard's own maintainers caution against treating the
composite as self-explanatory, which is directly relevant to this project's own scoring-approach
research (see `docs/spec/SCORING.md`).

**Verified directly against Cassandra itself (established, live-queried 2026-09-25).** A direct fetch
of `https://api.securityscorecards.dev/projects/github.com/apache/cassandra` (Scorecard version
`v5.5.1`, result dated 2026-09-21) returns an **aggregate score of 4.6/10**, with `Code-Review`
scoring **0**, reason string: *"Found 0/30 approved changesets."* Other checks: `Maintained` 10
("30 commit(s) and 0 issue activity found in the last 90 days" — the 0 issue activity reflects
GitHub Issues being disabled on `apache/cassandra`, not inactivity); `Branch-Protection` 1;
`Token-Permissions` 0; `Pinned-Dependencies` 0; `SAST` 0; `Fuzzing` 0; `CII-Best-Practices` 0;
`Security-Policy` 10; `License` 10; `Binary-Artifacts` 10; `Dangerous-Workflow` 10; `Packaging` and
`Signed-Releases` both -1 (not applicable/not detected). **This is direct, concrete confirmation of
the point made in §4 and §8.5: Cassandra's actual review process (commit-trailer `reviewed by`
records plus JIRA) is invisible to Scorecard's `Code-Review` check, which only counts GitHub PR
approvals.** A naive reading of Cassandra's Scorecard result would conclude the project has *no*
code review at all, which is false — it is strong, concrete evidence that this project must compute
review coverage/participation from commit trailers and JIRA, not infer it from GitHub-native signals,
for ASF-style projects generally.

### 6.3 OpenSSF Scorecard — 8-question answers

1. **Measures:** supply-chain security posture and secure-development-practice hygiene (branch
   protections, dependency pinning, vulnerability response, CI/build security, release signing).
   Not: contributor wellbeing, reviewer load, newcomer experience, communication quality, or
   organizational sustainability — none of the 19 checks address these.
2. **Data required:** GitHub API/repo metadata (branch protection settings, workflow files,
   dependency manifests), OSV vulnerability database queries, and (for CII-Best-Practices) an
   external OpenSSF Best Practices badge lookup.
3. **Calculation:** per-check deterministic rule evaluation against repo configuration/metadata,
   scored 0–10, combined via risk-weighted average.
4. **Deterministic:** yes, given a fixed repo state and a fixed Scorecard version — this is one of
   the more cleanly deterministic systems reviewed in this document.
5. **Empirical validation:** not found as a claim that Scorecard's aggregate predicts actual
   security incidents; individual checks are directly observable configuration facts (e.g., "is
   branch protection on") rather than statistical predictions, so "validation" in the academic sense
   mostly doesn't apply — they are definitional, not inferential.
6. **Limitations:** explicitly, per Scorecard's own docs, the aggregate obscures which specific
   practices are/aren't in place; checks reward configuration and process artifacts (a badge, a
   file, a setting), not outcomes; says nothing about the human community around the code.
7. **Reproducible from public data:** yes, fully — Scorecard is open source, runs against public
   repo metadata, and is designed to be run by anyone via CLI or GitHub Action.
8. **Adopt/avoid:** **avoid** treating Scorecard (or any of its 19 checks) as a community-health
   input — the evidence above supports the project's implicit assumption that this is out of scope
   for METRICS.md/COMMUNITY-HEALTH.md. If this project wants a security-posture panel at all
   (arguably out of scope per the original brief, which is about community and engineering-activity
   health, not supply-chain security), Scorecard is a reasonable, well-documented, fully
   reproducible tool to reuse rather than reinvent — but it answers a different question than this
   project is asking, and should not be blended into contributor-sustainability or interaction-
   health dimensions.

---

## 7. Comparison table: CNCF/LFX vs. CHAOSS vs. ASF

| | **CHAOSS** | **CNCF / LFX Insights** | **ASF** |
|---|---|---|---|
| **Primary purpose** | Open, implementation-agnostic metrics catalog + reference tooling (Augur — archived/defunct as of Jul 2026; GrimoireLab — active) | CNCF: contribution dashboards (DevStats) + governance-driven graduation review (TOC). LFX Insights: hosted cross-project health scoring for 13k+ projects | Governance oversight via quarterly self-report (Board) + procedural incubation checklist (Incubator); not a health-scoring system |
| **Data sources** | git logs, issue/PR/change-request metadata via Perceval connectors | DevStats: GH Archive + GitHub API + git. TOC: self-reported data + OpenSSF badge status. LFX Insights: GitHub + 15+ integrated sources + OpenSSF Criticality Score | Entirely self-authored prose by PMC chairs; reporter.apache.org template scaffold; Whimsy JSON exports for roster data |
| **Deterministic?** | Formulas deterministic given fixed input + identity mapping; mapping itself is human-curated and can drift over time | DevStats: deterministic per gitdm-mapping-version (~monthly revisions). TOC: checklist/threshold judgment, not computed. LFX Insights: methodology changed at least once with no visible version history — not confirmed deterministic/reproducible | No — qualitative narrative reviewed by the Board |
| **Open source?** | Yes — metrics catalog and GrimoireLab (incl. SortingHat) are open; Augur is archived/no longer CHAOSS | DevStats: yes (`cncf/devstats`). gitdm: yes, PR-correctable. TOC criteria: yes, public. LFX Insights: **no** — hosted/proprietary; only the OpenSSF Criticality Score input is independently open | Reports and Whimsy tooling are public; not a reusable software artifact |
| **Identity resolution** | SortingHat: identities → individuals via human-confirmed UI merge; automated bulk-merge internals at scale are unverified | gitdm: individual override → domain fallback → explicit "unknown" bucket → community PR correction, versioned and auditable. LFX Insights: undocumented algorithm | No formal mechanism — PMC rosters are authoritative LDAP-backed membership records, not inferred identities |
| **Org-affiliation tracking?** | Yes — Organizational Diversity model (Elephant Factor, Affiliation Diversity, # orgs), built on SortingHat data | Yes, prominently — DevStats slices by company via gitdm; TOC requires "maintainers from ≥2 orgs"; LFX Insights markets org-level "Contribution Affiliation" | Only informally, via prose mentions — no structured per-org data published |

---

## 8. Academic research: sustainability, bus factor, retention, review concentration

### 8.1 Apache Incubator graduation prediction — Yin, Chen, Xuan, Filkov (2021)

**Citation (verified):** Likang Yin, Zhuangzhi Chen, Qi Xuan, Vladimir Filkov, "Sustainability
Forecasting for Apache Incubator Projects," *ESEC/FSE '21*, Athens, Greece, 2021.
DOI: [10.1145/3468264.3468563](https://doi.org/10.1145/3468264.3468563).
arXiv: [2105.14252](https://arxiv.org/abs/2105.14252) (abstract re-fetched and confirmed directly).
A companion dataset paper exists at MSR 2021 ([IEEE](https://ieeexplore.ieee.org/document/9463075)),
and a follow-up tool paper "Exploring Apache Incubator Project Trajectories with APEX," MSR 2022
(DOI: 10.1145/3524842.3528506). A further paper reportedly extending this to code/process metrics
("Code, Quality, and Process Metrics in Graduated and Retired ASFI Projects") was found only via a
single secondary summary (an ASF blog post), with no independently confirmed DOI or venue page —
**do not cite that specific paper without further verification.**

1. **Measures:** whether an ASF Incubator podling will graduate vs. be retired, forecast within
   roughly 8 months of incubation start.
2. **Data required:** mailing-list/email communication (NLP-analyzed), commit history, Incubator
   status reports, and semi-structured interviews with 16 Incubator mentors — a hybrid quantitative
   + qualitative dataset, not git-log-only.
3. **Calculation:** socio-technical network modeling combining "internal" (productivity/commit)
   metrics and "external" (mailing-list engagement/popularity) metrics, feeding an interpretable
   ML model. Reported predictive signals include frequency of governance discussions (releases,
   documentation, testing), moderate code complexity, presence of both major and minor
   contributors (an early, direct signal of contributor-funnel health), mentor engagement, and
   timeliness of board reporting.
4. **Deterministic:** the underlying features are deterministic and reproducible from (mostly)
   public data, but the prediction itself is a trained statistical model, not a fixed formula.
5. **Empirical validation: established, and the strongest result in this literature.** The arXiv
   abstract itself (the more authoritative of two secondary figures found) states the model
   forecasts sustainability "with more than 93 percent accuracy, within 8 months of incubation
   start," against ground-truth graduate/retired outcomes across the ASF Incubator's historical
   project population. **Could not independently verify** the exact ML algorithm, cross-validation
   protocol, or confidence intervals — full-text PDF extraction failed during this research; the
   headline accuracy figure is sourced from the paper's own abstract, not a secondary summary.
6. **Limitations:** single-foundation study (ASF-specific governance/mentoring process; may not
   generalize to non-incubated or non-ASF projects); relies partly on non-public data (mentor
   interviews, internal status reports) this project's collectors cannot reproduce; ASF Incubator
   has an acknowledged high base failure rate ("more than 80 percent of OSS projects fail," per the
   paper's framing), raising a class-imbalance question the extracted text does not resolve.
7. **Reproducible from git history alone:** **no.** The paper's strongest predictors are
   governance-discussion content and mentor engagement — signals a git/PR/JIRA-only pipeline will
   not capture. This is a direct, important caveat: this project's Phase 1 deterministic-only MVP
   (DECISIONS.md D1) will, per this paper's own findings, miss the leading predictors of
   sustainability that peer-reviewed research identified as most informative. That is an argument
   *for* Phase 2's mailing-list/communication signals, not a criticism of the phasing itself.
8. **Adopt/avoid:** adopt the framing that governance-discussion frequency and contributor-role
   diversity (not raw commit volume alone) predict sustainability, as a citable justification for
   folding mailing-list/PR/JIRA-adjacent signals into this project's metric set once Phase 2 begins.
   Avoid citing "93%+ accuracy" as evidence for this project's own simpler deterministic heuristics
   (bus factor, HHI) — that figure is specific to Yin et al.'s ML model on ASF Incubator data, not
   transferable to unrelated formulas.

### 8.2 Bus factor / truck factor — Avelino, Passos, Hora, Valente (2016), with two follow-ups

**Citation (verified, primary-source PDF read directly, including formula and validation numbers):**
Guilherme Avelino, Leonardo Passos, Andre Hora, Marco Tulio Valente, "A Novel Approach for
Estimating Truck Factors," *ICPC 2016*, IEEE. DOI: [10.1109/ICPC.2016.7503718](https://doi.org/10.1109/ICPC.2016.7503718).
arXiv: [1604.06766](https://arxiv.org/abs/1604.06766).

1. **Measures:** the minimum number of developers who, if they left simultaneously, would leave a
   project unable to maintain itself — knowledge-concentration risk.
2. **Data required:** full git commit history per file — which authors touched which files, commit
   counts and timing per author per file. Git-log-only; no issue tracker or mailing-list data
   needed, directly matching this project's planned data sources.
3. **Calculation — verified directly from the PDF (primary source, not a secondary summary):**
   a **Degree of Authorship (DOA)** score per developer per file:

   ```
   DOA(dev, file) = 3.293 + 1.098 × FA(dev, file) + 0.164 × DL(dev, file) − 0.321 × ln(1 + AC(dev, file))
   ```

   where **FA** = 1 if the developer authored the file's first version (else 0), **DL** relates to
   how recently/how much the developer has changed the file relative to others, and **AC** is the
   number of other developers' changes to the file since (more competing changes by others *lowers*
   DOA). Note that the paper's own text shows **DL's coefficient as positive (+0.164)**, not
   negative — a detail worth getting right if this project ever implements the formula, since an
   incorrect sign would invert the term's effect. Thresholds, also verified directly from the text:
   a developer counts as a file's "author" if their normalized DOA (range 0–1, where 1 = highest
   absolute DOA among all developers on that file) exceeds **k = 0.75** and absolute DOA is at least
   **m = 3.293** (the model's own constant term) — thresholds the authors tuned by manually
   inspecting a sample of 120 files, not derived from first principles. The **greedy truck-factor
   algorithm**: repeatedly remove the developer who is top-author of the most files; stop when the
   remaining authors cover **less than 50%** of the project's files (i.e., more than half the files
   become "orphaned").
4. **Deterministic:** yes, given fixed git history and fixed DOA parameters — the algorithm is
   greedy with no randomness. The parameters (k, m) are a researcher judgment call, so different
   threshold choices produce different truck-factor values from the same repository; this is a
   real, verified nuance, not just a caveat.
5. **Empirical validation — verified directly from the paper's own abstract and results text.**
   The authors surveyed developers from **67 of 133** studied GitHub projects. Of valid responses:
   **84%** agreed or partially agreed that the algorithm's identified top authors were in fact the
   system's actual main authors; separately, **53%** gave a positive or partially positive
   assessment of the *estimated truck-factor number itself* — these are two different questions
   ("are these the right people" vs. "is this the right count") and should not be conflated. This
   is developer self-report validation on a modest, but real, sample — **reasonable proxy**, not
   fully established, and it validates DOA's people-identification better than it validates the
   truck-factor number as a risk predictor.
6. **Limitations (author-stated and later-literature-identified):** the authors themselves write
   "there is no consensus about how to calculate truck factor, and no supporting evidence backing
   estimates for systems in the wild" — i.e., even they flag that low truck factor has not been
   shown to predict actual abandonment. 65% of their 133 studied projects had TF ≤ 2, meaning the
   metric is heavily right-skewed in practice. A follow-up comparative study — Ferreira, Mombach,
   Valente, Bigonha, "Algorithms for estimating truck factors: a comparative study," *Software
   Quality Journal* 27, 1583–1617 (2019), DOI: [10.1007/s11219-019-09457-2](https://doi.org/10.1007/s11219-019-09457-2)
   (citation verified via multiple independent listings; full-text content not independently
   re-extracted in this research pass) — exists specifically because different truck-factor
   algorithms disagree with each other on the same repositories. Most importantly, a 2024 critical
   replication — Nourry, Kondo, Saito, Iimura, Ubayashi, Kamei, "Myth: The loss of core developers
   is a critical issue for OSS communities," arXiv:[2412.00313](https://arxiv.org/abs/2412.00313)
   (fetched and confirmed directly) — analyzed **36,464** projects (vs. Avelino's 133) and found
   **89%** of projects experienced a "Truck Factor Developer Detachment" event (all core devs
   leaving), but **27%** of those subsequently attracted new core developers and revived. Their
   core finding, stated plainly: **losing truck-factor-defining developers is not reliably fatal**,
   and prior truck-factor literature over-generalized from small samples of atypical, highly
   popular projects.
7. **Reproducible from git history alone:** yes — this is the paper's central selling point, and it
   directly validates this project's plan (DECISIONS.md core deterministic-metrics list) to compute
   bus factor from git data alone.
8. **Adopt/avoid:** **adopt** DOA-weighted authorship (first-authorship + recency + relative-share
   weighting) over naive "top-N-by-raw-commit-count" truck factor — this is a direct, citable reason
   to prefer a DOA-style or at least file-coverage/recency-weighted heuristic, since raw commit
   count is known to misidentify true ownership (large mechanical refactors, bot commits, or
   squash-merges inflate counts without reflecting real expertise). Adopt the "orphaned files > 50%"
   stopping criterion verbatim as a clean deterministic definition. **Avoid** presenting bus factor
   as a validated predictor of project failure — both the original authors and the 2024 replication
   caution against exactly this. Frame it in this project's docs as "knowledge-concentration
   exposure," and consider surfacing recovery/onboarding-rate context alongside it, per Nourry et
   al.'s finding that many projects recover from core-developer loss. **Avoid** treating the DOA
   threshold constants (k=0.75, m=3.293) as universal — they were tuned on a specific corpus of 133
   popular GitHub projects, not JIRA-based ASF-governance projects, so sensitivity-testing against
   Cassandra's own history is warranted before trusting absolute values.

### 8.3 Elephant factor — no academic source exists (subjective/experimental, established as a gap)

Despite structural similarity to bus factor, **no peer-reviewed paper defining or validating
"elephant factor" was found.** CHAOSS's own knowledge-base entry (directly re-fetched to check this
claim) hedges that "the origin of the term 'elephant factor' is not clearly delineated in the
literature, though it may arise out of the general identification of software sustainability as a
critical non-functional software requirement by Venters et al (2014)" — itself a citation this
document could not independently confirm as a primary source (§8.6/§11). **Correction to an earlier
draft of this document:** a prior version of this section attributed the term's origin specifically
to Bitergia presenting it at OSCON 2015, in the same lineage as a "Pony Factor" coined by Daniel
Gruno in 2015. The CHAOSS KB page does not say this, and no independent primary source for it was
found on re-verification — that attribution is **removed as unverifiable** rather than repeated
here. **This must be
labeled subjective/industry-defined, not established, wherever it appears in this project's other
documents.** The arithmetic itself (sort organizations by commit count descending, cumulatively sum
until a threshold percentage is crossed) is simple and deterministic; the entire risk is in the
underlying author→organization mapping, not the summation. No study validates that elephant factor
predicts any real-world outcome (resilience to a sponsor's departure, project continuity, etc.).
**Adopt** the arithmetic; **avoid** describing it as academically validated anywhere in this
project's documentation, and flag clearly that affiliation-mapping data quality (not the formula) is
its actual bottleneck.

### 8.4 Contributor retention and newcomer barriers — Steinmacher et al.

Three papers verified from the same research program (Igor Steinmacher and collaborators):

- Steinmacher, Conte, Gerosa, Redmiles, "Social Barriers Faced by Newcomers Placing Their First
  Contribution in Open Source Software Projects," *CSCW '15*, pp. 1379–1392.
- Steinmacher, Conte, Treude, Gerosa, "Overcoming Open Source Project Entry Barriers with a Portal
  for Newcomers," *ICSE '16*, pp. 273–284. DOI: [10.1145/2884781.2884806](https://doi.org/10.1145/2884781.2884806).
- Steinmacher, Pinto, Wiese, Gerosa, "Almost There: A Study on Quasi-Contributors in Open-Source
  Software Projects," *ICSE '18*.

1. **Measures:** (CSCW'15) taxonomizes *social* barriers to a first contribution (as distinct from
   technical barriers); (ICSE'16) evaluates whether a purpose-built onboarding portal (FLOSScoach)
   reduces those barriers; (ICSE'18) measures the scale and causes of "quasi-contribution" — PRs
   submitted but never merged — as a lens on retention failure at the acceptance stage.
2. **Data required:** (CSCW'15) qualitative interviews/surveys plus artifact analysis. (ICSE'16) a
   controlled study of 65 students with diaries, self-efficacy questionnaires, and TAM instruments —
   not reproducible from repository data. (ICSE'18) GitHub PR data from 21 popular projects
   (10,099 quasi-contributors vs. 14,623 accepted contributors identified) plus two surveys.
3. **Calculation:** not formula-based for the barrier taxonomy (qualitative categories: Reception,
   Communication, Orientation issues, alongside technical/process barriers). The quasi-vs-actual
   contributor split *is* directly operational and computable: a "quasi-contributor" is a submitter
   whose PR was never merged, contrasted with a merged-PR contributor — directly computable from PR
   merge-status data this project already plans to collect.
4. **Deterministic:** the quasi-contributor/actual-contributor split is deterministic given PR
   merge-status data; the barrier taxonomy is qualitative/interpretive, meant to inform what to
   measure or investigate, not itself a number.
5. **Empirical validation:** strong for this program. ICSE'16's portal study is genuinely
   experimental (control group), finding FLOSScoach improved orientation/process barriers but did
   **not** meaningfully reduce technical barriers — a specific, falsifiable, non-trivial finding.
   ICSE'18 is large-scale (21 projects, ~25,000 total contribution attempts, two surveys) with clear
   operational definitions. Rate: **established** for the qualitative barrier taxonomy and the
   quasi-contributor phenomenon's existence/scale; **reasonable proxy** for reducing it to a single
   retention metric, since root causes aren't git-log-observable.
6. **Limitations:** CSCW'15 data is from 2015, predating now-common onboarding conventions
   ("good first issue" labels, bot-driven welcome messages, CONTRIBUTING.md norms) — flag that OSS
   onboarding UX has materially evolved since. The portal study used students, not real newcomers in
   the wild, limiting external validity. ICSE'18 found PR-rejection reasons dominated by
   "superseded/duplicate" and "vision mismatch" — reasons invisible in raw merge/close metadata (a
   closed-not-merged PR looks the same whether it was rejected for genuine quality reasons or for
   reasons that reflect badly on the project), which limits what any deterministic pipeline can
   infer about *why* retention fails, only *that* it fails and at what rate.
7. **Reproducible from git history alone:** partially. First-PR-to-merge conversion rate and
   repeat-contribution rate (does a first-time contributor return for a second contribution within
   N months) are fully reproducible from PR/issue-tracker API data — directly supporting this
   project's planned contributor-funnel metrics. The causal barrier taxonomy is not reproducible
   from structured data alone.
8. **Adopt/avoid:** adopt first-PR-to-merge conversion rate and repeat-contribution rate as core
   funnel/retention metrics — this is exactly the quasi-contributor framing, data-native to this
   project's sources, and empirically shown to matter. Adopt the qualitative barrier taxonomy as a
   documentation/interpretation aid (when a dashboard shows a retention drop, cite Steinmacher's
   categories as candidate root causes to investigate manually) rather than something to
   auto-score. Avoid conflating "quasi-contributor rate" with "barrier severity" — the metric shows
   *that* a problem may exist, not *which* barrier, so avoid overclaiming diagnostic specificity.

### 8.5 Code-review health and reviewer concentration

No single canonical "reviewer concentration index" paper exists comparable to Avelino's truck-factor
work, but a substantial, verified body of literature covers directly adjacent constructs:

- Rigby, Storey, "Understanding Broadcast Based Peer Review on Open Source Software Projects,"
  *ICSE 2011*, pp. 541–550; extended in Rigby, Germán, Cowen, Storey, "Peer Review on Open-Source
  Software Projects: Parameters, Statistical Models, and Theory," *ACM TOSEM* 23(4), 2014,
  DOI: [10.1145/2594458](https://doi.org/10.1145/2594458) (author list corrected in this pass —
  the 2014 paper has four authors, not two) — foundational work on OSS's broadcast-style (not
  assigned) review model.
- Baysal, Kononenko, Holmes, Godfrey, "Investigating Technical and Non-Technical Factors Influencing
  Modern Code Review," *Empirical Software Engineering*, 2015 — studied WebKit; found median review
  time scaled directly with reviewer queue depth (reported 63 / 90 / 158 minutes median review time
  at queue depths of 0 / 2 / 5 waiting patches) — a quantified demonstration that reviewer-load
  concentration causes latency bottlenecks.
- Kononenko, Baysal, Godfrey, "Code Review Quality: How Developers See It," *ICSE 2016* — studied
  28,127 Mozilla reviews; found reviewer workload was among the factors associated with 54% of
  reviewed changes still introducing bugs post-review.
- McIntosh, Kamei, Adams, Hassan, "The Impact of Code Review Coverage and Code Review Participation
  on Software Quality," *MSR 2014*, extended as "An Empirical Study of the Impact of Modern Code
  Review Practices on Software Quality," *Empirical Software Engineering*, 2016,
  DOI: [10.1007/s10664-015-9381-9](https://doi.org/10.1007/s10664-015-9381-9) — studied Qt, VTK,
  ITK; regression models show **low review coverage and low review participation both
  independently predict higher post-release defect rates** — the strongest outcome-validated
  evidence in this literature that review-process metrics matter for real quality, not just process
  aesthetics.
- Hajari, Malmir, Mirsaeedi, Rigby, "Factoring Expertise, Workload, and Turnover into Code Review
  Recommendation," *IEEE TSE*, 2024 (DOI resolvable via IEEE Xplore, document 10444097) — across six
  major OSS projects, found the **median reviewer participates in only 2–3 reviews/month while the
  95th-percentile reviewer participates in 13–36 reviews/month**, and applies a **Gini
  coefficient/Lorenz-curve** formalism to quantify review-load concentration — the closest existing
  academic analogue to a formal "reviewer concentration index."

1. **Measures:** how unevenly review workload distributes across a reviewer pool; how that
   imbalance affects review latency; how coverage/participation causally relate to defect rates.
2. **Data required:** reviewer identity, request/completion timestamps, which changes each reviewer
   touched — for Cassandra this maps to JIRA reviewer/comment data or GitHub PR review data, both
   already in this project's planned scope.
3. **Calculation:** two directly reusable constructs converge from this literature — a **Gini
   coefficient/Lorenz curve over per-reviewer review counts** (a defensible, precedented alternative
   or complement to HHI specifically for review load, per Hajari et al.), and separately, **review
   coverage** (% of merged changes reviewed at all) and **review participation** (reviewers per
   change) as independently outcome-validated quality predictors.
4. **Deterministic:** yes — coverage, participation, and Gini/HHI-style concentration are all
   deterministic aggregate statistics over structured review-event logs.
5. **Empirical validation:** established for the coverage/participation → defect-rate link
   (McIntosh et al., regression-validated against real post-release defects on three projects) and
   for the load-imbalance → latency link (Baysal et al., directly measured). The Gini-based
   concentration formalism (Hajari et al.) is empirically *described* (shown to exist and be large)
   but not itself validated as an outcome predictor — reviewer-concentration-as-health-metric is a
   **reasonable proxy**, extrapolated from adjacent validated findings, not directly validated.
6. **Limitations:** most of this literature studies single, large, formally-instrumented projects
   (Mozilla, WebKit, Qt/VTK/ITK) using Gerrit/Phabricator-style structured review — Cassandra's
   JIRA-plus-mailing-list-plus-GitHub-PR workflow is messier and less uniformly instrumented,
   especially in older history, so coverage/participation figures will likely need more careful
   data-cleaning than in these source projects. None of this literature specifically studies
   JIRA-based ASF-governance projects.
7. **Reproducible from git history alone:** no — review data is not in git commits; it requires
   PR/JIRA/reviewboard metadata, consistent with this project's already-planned data sources.
8. **Adopt/avoid:** adopt review coverage and participation as first-class metrics alongside
   reviewer concentration — the evidence for these is stronger and more outcome-validated than for
   concentration alone. Adopt Gini/Lorenz as a complementary lens to HHI specifically for reviewer
   concentration (more directly precedented in this sub-literature than HHI is), while keeping HHI
   for general contributor concentration where it's more standard. Avoid presenting "reviewer
   concentration is high" alone as a validated risk signal without coverage/participation context.

### 8.6 General OSS sustainability metrics research — a direct caution for this project's design

- Yehudi, Goble, Jay, "Individual context-free online community health indicators fail to identify
  open source software sustainability," arXiv:[2309.12120](https://arxiv.org/abs/2309.12120)
  (submitted 2023, revised 2024). Studying 38 OSS projects over one year with both survey and
  repository data, the authors found that **the same quantitative indicator often means
  structurally different things across different projects depending on context**, and conclude
  that "context-free metrics... might even become detrimental if used to support high-stakes
  decision making" when compared cross-project without qualitative context. Their recommendation:
  pair quantitative metrics with qualitative, single-project contextual analysis rather than rely on
  cross-project comparison alone. **Established** as a real empirical finding (real data, real
  methodology), though it is a single study (38 projects, one year) and its "detrimental" claim is
  a normative caution more than a strict statistical result.
- Adejumo, Johnson, "An Empirical Validation of Open Source Repository Stability Metrics,"
  arXiv:[2508.01358](https://arxiv.org/abs/2508.01358), located and citation-verified but its
  specific tested metrics and correlation results were not independently extracted in this research
  pass (PDF extraction limits) — **flagged as located but not deeply verified**; read directly
  before citing its specific findings elsewhere in this project's documents.
- CHAOSS itself is a Linux Foundation working group, not a peer-reviewed academic body — its metrics
  catalog is community-consensus-built, not independently peer-reviewed, unless a specific metric
  has independent academic validation elsewhere (as bus factor does, via §8.2, and review coverage/
  participation does, via §8.5).
- A background citation repeatedly invoked by others ("Venters et al. 2014," on software
  sustainability as a non-functional requirement) **could not be independently located or confirmed**
  as a primary source in this research pass — it surfaced only inside other papers' reference lists,
  never as a directly verifiable hit. Do not cite it without further verification.

**This is arguably the single most actionable piece of academic pushback for this project's overall
design**, not just for one metric: it directly reinforces DECISIONS.md D2.2 ("trends over
snapshots... never [compared] to universal thresholds") and D2.7 ("no single headline health
number"). The Yehudi et al. finding is independent, citable, academic support for exactly the design
posture this project's binding decisions already committed to before this research phase began —
worth stating plainly in METRICS.md and SCORING.md as a citation, not just an internal design choice.

### 8.7 Cross-cutting assessment against this project's planned deterministic metrics

| Planned metric | Academic grounding | Verdict |
|---|---|---|
| Bus factor | Avelino et al. 2016 + Ferreira et al. 2019 (comparative) + Nourry et al. 2024 (replication) | Reasonable proxy; git-native and computable with modest survey validation of "who counts as an author." Not validated as a failure predictor — refine toward DOA-weighting rather than naive top-N-committer counting. |
| Elephant factor | None (industry/CNCF/Bitergia origin only) | Subjective/experimental. Structurally sound by analogy; zero empirical validation. Document honestly. |
| Contributor concentration (HHI) | No paper applies HHI specifically to OSS contributor concentration; Gini/Lorenz literature (Hajari et al.) is the closest analogue, applied to reviewers | Reasonable proxy by economic-literature analogy; OSS-specific validation is thin. |
| Reviewer concentration | Hajari et al. 2024 (Gini/Lorenz); Baysal et al. 2015 (queue depth → latency); Kononenko et al. 2016 (workload → quality) | Established that load imbalance is real and consequential; the concentration-index-as-formal-metric itself is a reasonable proxy built on adjacent established findings — among the better-supported choices in the whole metric set. |
| Contributor funnel/retention | Steinmacher et al. 2015/2016/2018 | Established that first-PR-to-merge conversion and repeat-contribution are real, measurable, and linked to identifiable barrier categories. Among the most directly research-backed metrics planned, provided it's framed as "what," not "why." |
| Cross-project "health score" framing | Yehudi, Goble, Jay 2023/2024 | Established caution: avoid implying a metric means the same thing across every project regardless of governance model, size, or maturity stage — directly supports this project's own no-composite-score, own-history-baseline decision (D4). |

---

## 9. Toxicity, sentiment, and disagreement in software-engineering communities

### 9.1 Raman, Cao, Tsvetkov, Kästner, Vasilescu, "Stress and Burnout in Open Source" (2020)

**Citation (verified):** *ICSE-NIER '20*. DOI: [10.1145/3377816.3381732](https://doi.org/10.1145/3377816.3381732).
Replication package: github.com/CMUSTRUDEL/toxicity-detector.

**Correction to a common misconception:** this paper does **not** build a multi-category taxonomy of
unhealthy-interaction types. It builds a single **binary toxic/non-toxic classifier** ("STRUDEL")
and runs exploratory longitudinal studies with it. This distinction matters for how this project
cites it — later papers (Miller et al., §9.2) explicitly cite this binary-collapse as the paper's
own limitation.

1. **Measures:** binary toxicity of a GitHub issue comment.
2. **Data required:** a hand-labeled corpus of 386 GitHub issue threads (167 containing ≥1 toxic
   comment: locked-as-"too heated" issues plus maintainer-"attitude"-reaction issues), plus 300
   additional randomly sampled threads, split 50/50 train/test.
3. **Calculation:** SVM over engineered features — comment length, TF-IDF, Politeness
   (Danescu-Niculescu-Mizil detector), Toxicity (Google Perspective API), Subjectivity/Polarity
   (TextBlob), Sentiment (NLTK/VADER), Anger (LIWC). A domain-adaptation step neutralizes SE-jargon
   words with false-negative connotations (e.g., "kill," "abort") via log-odds comparison against a
   general-English corpus.
4. **Deterministic:** the SVM is deterministic given fixed weights/inputs; the pipeline depends on
   the external, versioned Google Perspective API, so the full pipeline is not fully deterministic
   over time.
5. **Empirical validation:** reported at three operating points that diverge sharply — this
   divergence *is* the key lesson. Best cross-validated configuration: precision 0.91 / recall 0.42.
   Held-out test set: 75% precision / 35% recall. On 100 randomly sampled issues the model flagged
   toxic, out of a 100K-issue sample: only **50% precision**. Precision collapses from 91% to 50%
   moving from curated benchmark to "in the wild."
6. **Limitations:** overfitting risk from a small training set; GitHub-issues-only (no mailing
   lists, PRs, or chat); no subpopulation fairness analysis.
7. **Reproducible/open:** yes — code, model, and partial labeled data are public.
8. **Adopt/avoid:** **avoid** treating this (or any similarly-scoped classifier) as a drop-in
   toxicity oracle for Cassandra — its own authors show precision collapsing by 41 points moving
   from curated to wild data, the single most concrete argument in this literature for why a
   classifier must be validated against Cassandra's own labeled data before being trusted, not
   assumed to transfer from a paper's benchmark. **Adopt** the domain-adaptation technique
   (correcting SE-jargon false positives) and the practice of reporting precision/recall at multiple
   distribution shifts, which this project's own classifier-validation gate should replicate.

### 9.2 Miller, Cohen, Klug, Vasilescu, Kästner, "Did You Miss My Comment or What?" (2022)

**Citation (verified):** *ICSE '22*, pp. 710–722. DOI: [10.1145/3510003.3510111](https://doi.org/10.1145/3510003.3510111).
ACM SIGSOFT Distinguished Paper Award winner.

1. **Measures:** not automated — a qualitative characterization study producing a **9-dimension
   coding scheme**: **Nature** (Insulting, Arrogant, Entitled, Trolling, Unprofessional),
   **Severity** (profanity yes/no), **Target** (At code, At people, Other), **Author** (New account,
   Repeat issuer, Experienced dev, Project member), **Triggers** (Errors, Technical Disagreement,
   Politics/Ideology, Past Interactions), plus project-level covariates.
2. **Data required:** 100 manually labeled toxic GitHub issue discussions, sampled via four
   overlapping detection strategies (Raman et al.'s classifier applied to issues and comments
   separately; code-of-conduct keyword search; heated-lock signal; deleted-issue signal),
   20 per strategy.
3. **Calculation:** manual thematic/grounded qualitative coding (Lincoln & Guba trustworthiness
   criteria), not automated.
4. **Deterministic:** N/A — human coding.
5. **Empirical validation:** inter-rater reliability explicitly measured — Cohen's unweighted
   kappa = **0.82** (149 comments, 2 raters), Fleiss' kappa = **0.72** (43 comments, 3 raters), both
   "substantial to near-perfect agreement." Separately, and critically: **the four automated/
   heuristic strategies used just to *find* candidate toxic issues had false-positive rates of
   0.69–0.98** — even combining a purpose-built classifier, keyword search, lock signal, and
   deletion signal, 69–98% of what they flagged was not actually toxic on manual review.
6. **Limitations (author-stated):** diverse but not statistically representative sample (no
   prevalence claims possible); GitHub-issues-only, English-only; toxicity construct validity
   contested ("very much in the eye of the beholder"); deliberately no contact with original
   participants (Belmont Report beneficence), so no ground-truth on actual harm experienced.
7. **Reproducible/open:** partially — sample-selection strategy documented, but raw issue links
   intentionally withheld for participant privacy; replication package available on request.
8. **Adopt/avoid:** **adopt directly** — the strongest match in this literature to this project's
   proposed schema. Their "entitlement, insults, arrogance" (not hate speech/harassment, which
   dominate elsewhere) as the dominant OSS toxicity types maps onto `dismissiveness`/
   `personal_attack`; their **Trigger: Technical Disagreement** category is direct precedent for
   treating `technical_disagreement` as analytically separate from `personal_attack`. Their explicit
   statement that "classifiers of toxicity, sentiment, emotion... rarely generalize well beyond the
   specific contexts in which they have been developed, even within the same general software
   engineering domain" is a direct, citable warning against assuming any off-the-shelf model works
   on Cassandra text. Their 69–98% false-positive rate for every tested detection heuristic is the
   strongest available evidence for gating any classifier behind a frozen human-labeled validation
   set before operational use. **Avoid** assuming toxicity maps to outsiders/newcomers — they found
   project members/maintainers frequently author toxic comments too, so classification should not
   assume "community vs. maintainer" as a health proxy.

### 9.3 ToxiCR — Sarker, Turzo, Dong, Bosu (2023)

**Citation (verified):** "Automated Identification of Toxic Code Reviews Using ToxiCR," *ACM TOSEM*
32(1), Article 1, 2023. DOI: [10.1145/3583562](https://doi.org/10.1145/3583562). arXiv:
[2202.13056](https://arxiv.org/abs/2202.13056). Code/data: github.com/WSU-SEAL/ToxiCR.

1. **Measures:** binary toxic/non-toxic classification of individual code-review comments (not
   multi-label, not thread-level).
2. **Data required:** 19,651 manually labeled code-review comments from four FOSS communities
   (Android, Chromium OS, OpenStack, LibreOffice).
3. **Calculation:** a pipeline offering a choice of 10 algorithms (5 classical/ensemble, 4 DNN
   architectures, 1 BERT-based), 8 preprocessing steps (2 SE-domain-specific, e.g. stripping
   embedded code/stack traces), and 5 vectorization options; best configuration chosen empirically.
4. **Deterministic:** the final selected model is deterministic at inference given fixed weights;
   the model-selection process (grid search over configurations) is not a single fixed procedure.
5. **Empirical validation:** 10-fold cross-validation; best configuration achieved **95.8% accuracy,
   88.9% F1**. The paper also reports that Raman et al.'s STRUDEL and four other general-purpose
   toxicity detectors "performed poorly on new samples," improving only after retraining on
   SE-specific data (accuracy 83%→92%, F-score 40%→87%) — direct evidence of the cross-dataset
   generalization problem.
6. **Limitations:** binary label only — no distinction between personal attack, sarcasm,
   dismissiveness, or gatekeeping; trained on 4 specific FOSS communities' code-review comments,
   generalization to mailing lists, JIRA, or chat not established.
7. **Reproducible/open:** yes — dataset, pretrained models, evaluation results, source code all
   public.
8. **Adopt/avoid:** adopt the domain-specific preprocessing insight (strip code/stack traces before
   classifying prose) and the practice of benchmarking multiple algorithm families before
   committing. **Avoid** treating ToxiCR's binary output as sufficient — it collapses everything
   this project wants distinguished (personal_attack, dismissiveness, gatekeeping, sarcasm) into one
   "toxic" bucket, with no positive-pole labels at all (constructive_counterargument, deescalation,
   resolution). It is evidence of feasibility on code-review comments *specifically* (a narrower,
   more homogeneous register than Cassandra's JIRA/mailing-list/Slack mix), not evidence the same
   numbers hold on Cassandra's own corpus.

### 9.4 Incivility research — Ferreira, Cheng, Adams / Ferreira, Rafiq, Cheng

**Foundational taxonomy paper (verified, including DOI, directly re-confirmed):** Isabella Ferreira,
Jinghui Cheng, Bram Adams, "The 'Shut the f**k up' Phenomenon: Characterizing Incivility in Open
Source Code Review Discussions," *Proc. ACM Hum.-Comput. Interact.* 5(CSCW2), Article 353, 2021.
DOI: [10.1145/3479497](https://doi.org/10.1145/3479497). arXiv: [2108.09905](https://arxiv.org/abs/2108.09905).

This paper defines **incivility** as "features of discussion that convey an unnecessarily
disrespectful tone toward the discussion forum, its participants, or its topics" — explicitly
broader than toxicity (impact-centered) and narrower than hate speech (slur/emotion-centered). It
introduces the **Tone Bearing Discussion Feature (TBDF) taxonomy**, from Linux Kernel Mailing List
rejected-patch discussions: **civil positive** (humility, excitement); **civil neutral**
(apologies, friendly joke); **civil negative** (sadness, oppression); **uncivil** (bitter
frustration, impatience, mocking, irony, vulgarity, threat, entitlement, insulting, identity
attacks/name-calling). The paper reports **66.66%** of non-technical LKML emails in their sample
included uncivil features, across a 1,545-email corpus from rejected-patch threads.

**Detection paper (verified, primary source read):** Isabella Ferreira, Ahlaam Rafiq, Jinghui Cheng,
"Incivility Detection in Open Source Code Review and Issue Discussions," preprint submitted to
*Journal of Systems and Software*, arXiv:[2206.13429](https://arxiv.org/abs/2206.13429) (v2 dated
2023-12-20). A matching ScienceDirect record exists ([S0164121223003308](https://www.sciencedirect.com/science/article/abs/pii/S0164121223003308));
the final journal DOI was not independently confirmed.

1. **Measures:** two-stage: **CT1** = tone-bearing vs. non-tone-bearing text; **CT2** (only for
   tone-bearing text) = civil vs. uncivil, using the TBDF categories.
2. **Data required:** two prior manually-labeled datasets — code review (LKML rejected-patch
   emails: 1,365 non-tone-bearing + 168 tone-bearing; within tone-bearing, 117 civil vs. 276
   uncivil sentences) and issues (GitHub issues locked as "too heated": 4,793 non-tone-bearing +
   718 tone-bearing; 353 civil vs. 896 uncivil). Incivility is rare overall: **7.25%** of code
   review comments and **8.82%** of issue comments in their earlier studies.
3. **Calculation:** 6 classical ML models (CART, KNN, Logistic Regression, Naive Bayes, Random
   Forest, SVM) compared against BERT, with augmentation (synonym replacement, random insertion/
   swap/deletion) and class-balancing (oversampling, undersampling, SMOTE) to address class rarity.
4. **Deterministic:** BERT fine-tuning is stochastic (random init, batching); classical models are
   largely deterministic given fixed hyperparameters/splits. Neither is deterministic across
   retraining runs without seed control.
5. **Empirical validation:** 5-fold cross-validation with hyperparameter search. **BERT
   outperformed all classical models, best F1 = 0.95** on uncivil-detection for both datasets.
   Classical models underperform specifically on the majority (civil/non-tone-bearing) class — an
   asymmetric error risk. Adding prior-comment context did **not** improve BERT's performance.
   **Critically: no classifier tested performed well cross-platform** — a model trained on code-
   review incivility did not transfer reliably to issue-discussion incivility, or vice versa, even
   though both are GitHub/OSS text.
6. **Limitations:** rare-class data-scarcity problem persists even with augmentation; explicit
   cross-platform transfer failure; incivility remains contested as a construct ("very much in the
   eye of the beholder," echoing Miller et al.); the paper reports conflicting conclusions with
   related work on whether conversational context helps.
7. **Reproducible/open:** yes — dataset, code, and results published via a replication package
   (figshare DOI 10.6084/m9.figshare.24603237, per the paper's own text).
8. **Adopt/avoid:** adopt the TBDF taxonomy's granularity as a partial model for decomposing
   `sarcasm` and `dismissiveness` (irony/mocking vs. bitter frustration/impatience vs. entitlement
   are analytically distinct with different remediation implications). Adopt the two-stage design
   pattern (tone-bearing? then which tone?) as directly reusable for a thread-level pipeline that
   first flags threads worth deeper labeling. **The cross-platform transfer failure is the single
   most important negative finding here**: even within the same general OSS/GitHub domain, a
   classifier trained on one register (code review) did not reliably transfer to a sibling register
   (issue discussion). Cassandra's corpus spans JIRA, dev mailing list, and Slack — registers *more*
   different from each other than code-review-vs-issues — so a classifier trained anywhere else must
   be re-validated, not assumed, separately on each of Cassandra's own text sources.

### 9.5 Sentiment-analysis tool limitations on SE text

**Senti4SD** — Calefato, Lanubile, Maiorano, Novielli, "Sentiment Polarity Detection for Software
Development," *Empirical Software Engineering* 23(3), 1352–1382, 2018.
DOI: [10.1007/s10664-017-9546-9](https://doi.org/10.1007/s10664-017-9546-9). Trained on a manually
double-annotated Stack Overflow gold standard, combining lexicon-based, keyword-based, and
word-embedding semantic features (the exact underlying learning algorithm was not independently
re-confirmed in this research pass — commonly described elsewhere as SVM-based, flagged as
unverified in detail). Reports **95.27% overall accuracy** on its Stack Overflow test set (per-class
F1 0.941–0.974), compared against SentiStrength as baseline, which misclassified **28%** of neutral
posts as negative (neutral-class recall only 0.64, negative-class precision only 0.67) — a
demonstration that general-purpose tools systematically over-flag neutral technical language
("kill," "error," "abort," "bug") as negative. **Adopt** the core lesson (general-purpose sentiment
tools misfire specifically on SE jargon, directly reinforcing this project's "sentiment ≠ health"
stance). **Avoid** assuming Senti4SD's Stack-Overflow-trained accuracy transfers to Cassandra's
mailing-list/JIRA/Slack text without validation, and note that sentiment polarity itself is not what
this project wants to measure — a scathing-but-correct technical critique is legitimately negative
in sentiment and should not be conflated with unhealthy.

**Jongeling, Sarkar, Datta, Serebrenik, "On negative results when using sentiment analysis tools for
software engineering research," *Empirical Software Engineering* 22(5), 2543–2584, 2017.
DOI: [10.1007/s10664-016-9493-x](https://doi.org/10.1007/s10664-016-9493-x).** A benchmarking/
meta-study across seven Stack-Overflow and issue-tracker datasets with human-annotated ground truth,
comparing multiple off-the-shelf sentiment tools against each other and against human labels. Key
result: the tools **disagree with human annotation and with each other**; when the authors
replicated earlier published SE studies substituting a different sentiment tool, **the original
conclusions could not be confirmed** — published empirical claims in this literature have flipped
depending solely on which off-the-shelf tool was used, traced to most tools being trained on
product/movie reviews rather than SE text. **This is the single most important citation for
justifying this project's validation-gate design (DECISIONS.md D1's Phase 2a gate and D2.6's
versioning requirement).** It directly supports: never deploying any classifier — general-purpose or
SE-specific — against Cassandra text without first validating on a frozen, human-labeled Cassandra
benchmark; reporting inter-rater agreement and tool-vs-tool disagreement explicitly, not just
accuracy against one's own labels; and treating full provenance (classifier version, model version,
input hash) as a methodological necessity, since Jongeling's core finding is that swapping the tool
silently changes the answer — without provenance, no one downstream could tell whether a "health"
trend changed because the community changed or because the classifier was swapped/updated.

### 9.6 Constructive vs. destructive disagreement

No canonical "constructive-disagreement detector" exists analogous to ToxiCR; this is a less mature
area, and the closest work converges on multi-component models rather than a single score.

**Gonçalves, Çalıklı, Bacchelli, "Interpersonal Conflicts During Code Review: Developers'
Experience and Practices," *Proc. ACM Hum.-Comput. Interact.* 6(CSCW1), Article 33, 2022.
DOI: [10.1145/3512945](https://doi.org/10.1145/3512945).** Adapts Hartwick & Barki's interpersonal-
conflict model (from Information Systems research) into code review, defining conflict as a
**three-component construct**: (1) **negative emotions**, (2) **cognitive disagreement**, (3)
**interference of behavior** — disagreement alone (component 2) is explicitly *not* sufficient to
constitute harmful conflict; it requires accompanying negative affect and behavioral interference
(e.g., blocking, withholding). Based on semi-structured interviews with 22 developers; qualitative
thematic analysis, not a classifier. Findings: conflicts are "commonplace, anticipated, and seen as
normal," and conflicts **resolved constructively** "can also create value and bring improvement,"
while unresolved/mishandled conflict damages the review and the relationship. Limitations: small
interview sample (N=22), self-reported perceptions, no automated instrument produced. A related
paper on automated conflict detection — Qiu, Vasilescu, Kästner, Egelman, Jaspan, Murphy-Hill,
"Detecting Interpersonal Conflict in Issues and Code Review: Cross Pollinating Open- and
Closed-Source Approaches," *ICSE-SEIS '22*, DOI: [10.1145/3510458.3513019](https://doi.org/10.1145/3510458.3513019) —
extends detection to issues and code review across open- and closed-source (Google-internal) data.
**Correction to an earlier draft of this document:** this paper was previously misattributed here to
Gonçalves, Çalıklı, and Bacchelli as a "follow-up" to their CSCW '22 paper above. Re-querying the DOI
directly (Semantic Scholar) shows it is by a different author team entirely — the title and DOI were
correct, the prior author attribution was not. Its full abstract/findings were not independently
verified in this research pass (ACM access blocked); **flagged as unverified beyond title/authors/DOI.**

**Adopt directly:** the 3-component conflict model gives independent academic grounding for exactly
the distinction this project's own brief already assumes — `technical_disagreement` corresponds to
"cognitive disagreement" alone, while `personal_attack`/`dismissiveness` correspond to "negative
emotions" + "interference of behavior" layered on top. This is strong, independent support for
treating disagreement-intensity and harm as orthogonal axes, not one scale — precisely the framing
in `docs/research.md`'s "technical disagreement can be intense and still be productive."

**Gunawardena, Devine, Beaumont, Garden, Murphy-Hill, Blincoe, "Destructive Criticism in Software
Code Review Impacts Inclusion," *Proc. ACM Hum.-Comput. Interact.* 6(CSCW2), Article 292, 2022.
DOI: [10.1145/3555183](https://doi.org/10.1145/3555183).** Adapts Baron's applied-psychology
definitions: **destructive** feedback is negative feedback that is both **nonspecific and
inconsiderate** (harsh/sarcastic tone, attacks the individual rather than the work); **constructive**
feedback stays considerate and includes specific improvement suggestions. A vignette-based survey of
93 practitioners (not text classification — measures perception/self-report, not automated
detection). Found destructive criticism is common, and that **women are significantly less likely**
to view inconsiderate feedback as appropriate and **less motivated to continue working** with a
developer after receiving destructive criticism, regardless of whether code quality improved as a
result. Exact prevalence percentages were confirmed present in the paper but not independently
extracted in this research pass — **treat specific percentage figures as unconfirmed pending direct
extraction.** Small non-binary subsample (n=3) explicitly flagged by the authors as non-
representative. **Adopt** the finding that the *same* nonspecific/inconsiderate feedback has
**heterogeneous costs across recipients** — a caution against any health metric treating "harsh but
technically correct" feedback as uniformly fine or uniformly harmful; impact interacts with who
receives it, not just what was said. This argues for keeping this project's schema descriptive/
structural (label what happened) rather than computing a single universal "harm score."

### 9.7 Synthesis: what this literature implies for this project's classification schema

**Structured multi-label schema over a single score — strongly supported, not just a design
preference.** Every empirical taxonomy paper reviewed (Miller's Nature×Target×Author×Trigger scheme;
Ferreira's TBDF categories; Gonçalves's three-component conflict construct) independently converges
on decomposing "unhealthy interaction" into multiple orthogonal dimensions rather than collapsing it
to one score. The two papers that *do* output a single score (Raman/STRUDEL, ToxiCR) are also the
two whose authors and downstream critics most explicitly flag that single-score binary toxicity
collapses meaningfully different phenomena this project's schema keeps separate. This is
**established** support, not speculation.

**Where the literature's taxonomies map onto `docs/research.md`'s proposed labels — and where they
don't:**

- `technical_disagreement` — directly supported (Miller's Trigger:Technical Disagreement;
  Gonçalves's "cognitive disagreement" component).
- `personal_attack` — directly supported (Miller's Insulting + Target:At-people; Ferreira's
  insulting/identity-attacks/name-calling).
- `dismissiveness` — partially supported (Miller's Arrogant; Ferreira's bitter-frustration/
  impatience/entitlement cluster is adjacent, not an exact match).
- `sarcasm` — directly supported (Ferreira's irony/mocking TBDF categories).
- `gatekeeping` — **not present as a named category in any reviewed taxonomy.** None of Raman,
  Miller, Ferreira, or ToxiCR name or operationalize "gatekeeping" as distinct from arrogance/
  entitlement. This project is inventing new ground here; treat this label as **experimental**, with
  no existing benchmark or classifier to borrow calibration from — it needs its own labeled examples
  in the frozen benchmark corpus before it can be trusted at all.
- `constructive_counterargument`, `deescalation`, `resolution` — **not covered by any of the
  toxicity/incivility detection papers reviewed**, all of which are asymmetric (they detect only the
  bad pole; "non-toxic" is a residual catch-all, not a validated positive category). The closest
  support is Gunawardena's constructive-vs-destructive definitions and Gonçalves's
  conflict-resolution-strategy interview findings — but neither produces a validated automated
  classifier for these positive labels. **This is the part of the schema with the least existing
  empirical grounding and should be treated as the highest-risk, most experimental part of the
  Phase 2a validation work.**
- `confidence` (as a provenance/meta-field, not a behavioral label) is independently justified by
  Jongeling et al.'s finding that tool disagreement is rampant — a confidence field is not a
  nicety, it is a direct response to a documented, repeated failure mode in this exact research area.

**Thread-level vs. message-level — a genuine departure from the norm, with a real cost.** Every
automated detector reviewed (Raman/STRUDEL, ToxiCR, Ferreira's classifiers) operates at the comment
or sentence level, not the thread level. Miller et al.'s analysis is thread-aware but manual, not
automated. This means: (a) there is no directly reusable off-the-shelf classifier at this project's
target granularity — thread-level aggregation logic must be built and validated from scratch; (b)
the frozen benchmark corpus will need thread-level labels, which cost more per label than
sentence-level labels (ToxiCR's corpus has 19,651 comment-level labels), so the corpus will likely be
smaller in absolute example count — making inter-rater agreement (target: match or beat Miller's
Cohen's κ=0.82/Fleiss' κ=0.72) proportionally more important to compensate for a smaller N.

**Validation rigor implied by Jongeling et al. — directly justifies DECISIONS.md's gating decision.**
Jongeling's finding that sentiment tools disagree with humans and each other and flip published
conclusions, combined with Ferreira's finding that even a well-performing incivility classifier
(in-domain F1=0.95) failed to transfer cross-platform *within the same OSS/GitHub ecosystem*, and
Miller's finding that every tested detection heuristic had 69–98% false-positive rates in the wild,
together form a strong, convergent empirical case that **no classifier — regardless of its own
paper's reported accuracy — should be trusted on Cassandra's text without validation against a
frozen, Cassandra-specific, human-labeled benchmark corpus first.** DECISIONS.md D1's gating of
Phase 2a "only after a human-labeled benchmark corpus exists and the classifier clears agreement/
precision gates against it" is not just prudent design — it is a direct, literature-supported
response to a well-documented, repeated failure mode in this exact research area.

---

## 10. Cross-cutting takeaways for METRICS.md, SCORING.md, and COMMUNITY-HEALTH.md

- **Bus factor should be DOA-weighted (or at least recency/coverage-weighted), not naive top-N
  commit count** — directly supported by Avelino et al. (2016) and the follow-up comparative study;
  raw commit-count ranking is a known-weaker proxy in this literature.
- **Bus factor and elephant factor should be framed as "concentration exposure," never as a
  validated failure predictor** — both the original truck-factor authors and the 2024 Nourry et al.
  replication (36,464 projects) caution against this; 27% of projects that lost their core
  developers recovered.
- **Elephant factor has no academic validation at all** and must be labeled subjective/
  industry-defined in METRICS.md — its only precedent is Bitergia/CHAOSS/CNCF industry practice, not
  peer-reviewed research.
- **Reviewer concentration should ship alongside review coverage and review participation**, not
  alone — the latter two have real outcome validation (defect rates) that concentration-by-itself
  lacks; consider a Gini/Lorenz-curve option as a complement to HHI for reviewer load specifically.
- **First-PR-to-merge conversion and repeat-contribution rate are among the best-grounded metrics
  in this project's whole plan** — Steinmacher et al.'s quasi-contributor research directly
  validates this framing and its computability from PR/JIRA data alone.
- **Never imply cross-project or absolute-threshold comparability** for any of these metrics —
  Yehudi, Goble, and Jay's empirical finding that context-free indicators can mislead is independent
  academic support for DECISIONS.md's own-history-baseline, no-universal-thresholds stance.
- **The proposed structured, multi-label, thread-level classification schema is well-supported in
  its general shape**, but `gatekeeping` and the positive-pole labels
  (`constructive_counterargument`, `deescalation`, `resolution`) have no existing benchmark or
  classifier precedent anywhere in the literature reviewed — budget disproportionate validation
  effort for these labels specifically in Phase 2a.
- **Every classifier in this literature degrades sharply out-of-domain** (Raman: 91%→50% precision;
  Ferreira: strong in-domain, fails cross-platform within the same GitHub ecosystem) — this is not a
  hypothetical risk to hedge against, it is the empirically documented default outcome, reinforcing
  that Phase 2a's benchmark-corpus gate is a necessity, not an optional nicety.
- **OpenSSF Scorecard is a security tool and should not be blended into any community-health
  dimension** — of its 19 checks, only `Maintained` and `Contributors` touch anything
  community-adjacent, and both are framed as supply-chain-risk signals, not wellbeing measures.
- **GitHub's traffic API is unusable for this project's actual target repo** (requires push access
  this project will not have to `apache/cassandra`), and two of five GitHub statistics endpoints
  (`code_frequency`, `contributors`) degrade or zero out above 10,000 commits — Cassandra is well
  past that threshold, so additions/deletions and per-author breakdowns should be computed from a
  local git clone, which DECISIONS.md D3/D8 already plans to maintain anyway.
- **ASF itself has no quantitative health model** — the Incubator "maturity model" is a procedural
  checklist plus mentor discretion, and board reports are unstructured prose. This project is
  filling a genuine gap in ASF's own practice, not duplicating an existing system, and its dated
  monthly reports (DECISIONS.md D5) are a legitimate, citable upgrade path back into Cassandra's own
  board-reporting process once real data exists.

---

## References

### Existing systems

- [CHAOSS Metrics and Metrics Models](https://www.chaoss.community/kb-metrics-and-metrics-models/)
- [chaoss/metrics](https://github.com/chaoss/metrics)
- [CHAOSS: Contributor Absence Factor (Bus Factor)](https://chaoss.community/kb/metric-bus-factor/)
- [CHAOSS: Elephant Factor](https://www.chaoss.community/kb/metric-elephant-factor/)
- [CHAOSS: Organizational Diversity](https://www.chaoss.community/kb/metric-organizational-diversity/)
- [CHAOSS: Time to First Response](https://www.chaoss.community/kb/metric-time-to-first-response/)
- [CHAOSS: Change Request Closure Ratio](https://chaoss.community/kb/metric-change-request-closure-ratio/)
- [CHAOSS: Committers](https://chaoss.community/kb/metric-committers/)
- [CHAOSS Community Handbook: Working Groups](https://handbook.chaoss.community/community-handbook/community-initiatives/working-groups)
- [chaoss/augur (archived 2026-07-23)](https://github.com/chaoss/augur)
- [Public Update on Recent Events at CHAOSS](https://chaoss.community/public-update-on-recent-events-at-chaoss/)
- [chaoss/grimoirelab](https://github.com/chaoss/grimoirelab)
- [chaoss/grimoirelab-sortinghat](https://github.com/chaoss/grimoirelab-sortinghat)
- [GrimoireLab Tutorial: Merging profiles](https://chaoss.github.io/grimoirelab-tutorial/docs/sortinghat/profiles/merge/)
- [GrimoireLab grows up to power LFX Insights (PR Newswire)](https://www.prnewswire.com/news-releases/grimoirelab-grows-up-to-power-the-linux-foundations-lfx-insights-platform-301173642.html)
- [cncf/devstats](https://github.com/cncf/devstats)
- [cncf/devstats DASHBOARDS.md](https://github.com/cncf/devstats/blob/master/DASHBOARDS.md)
- [Japan's CNCF DevStats 2025 (CNCF blog, 2026-03-12)](https://www.cncf.io/blog/2026/03/12/japans-cncf-devstats-2025/)
- [LFX Insights: A new way to understand open source projects (CNCF blog, 2025-10-22)](https://www.cncf.io/blog/2025/10/22/lfx-insights-a-new-way-to-understand-open-source-projects/)
- [cncf/toc: graduation_criteria.md (redirect)](https://github.com/cncf/toc/blob/main/process/graduation_criteria.md)
- [cncf/toc: template-graduation-application.md](https://github.com/cncf/toc/blob/main/.github/ISSUE_TEMPLATE/template-graduation-application.md)
- [cncf/gitdm](https://github.com/cncf/gitdm)
- [cncf/gitdm PR #1264 (affiliation correction example)](https://github.com/cncf/gitdm/pull/1264)
- [LFX Insights](https://insights.linuxfoundation.org/)
- [LFX Insights: Health Score methodology](https://insights.linuxfoundation.org/docs/metrics/health-score)
- [ossf/criticality_score](https://github.com/ossf/criticality_score)
- [ASF: Board reporting guidance](https://www.apache.org/foundation/board/reporting)
- [Apache Committee Reporter](https://reporter.apache.org/)
- [Whimsy](https://whimsy.apache.org/)
- [Whimsy: Cassandra board minutes](https://whimsy.apache.org/board/minutes/Cassandra.html)
- [ASF Incubator: Incubation Policy](https://incubator.apache.org/policy/incubation.html)
- [GitHub Docs: Community profiles for public repositories](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)
- [GitHub REST API: Community profile metrics](https://docs.github.com/en/rest/metrics/community)
- [GitHub REST API: Traffic](https://docs.github.com/en/rest/metrics/traffic)
- [GitHub REST API: Repository statistics](https://docs.github.com/en/rest/metrics/statistics)
- [OpenSSF Scorecard: checks.md](https://github.com/ossf/scorecard/blob/main/docs/checks.md)
- [OpenSSF Scorecard README (scoring methodology)](https://github.com/ossf/scorecard)

### Academic literature

- Yin, L., Chen, Z., Xuan, Q., Filkov, V. (2021). Sustainability Forecasting for Apache Incubator
  Projects. *ESEC/FSE '21*. DOI: [10.1145/3468264.3468563](https://doi.org/10.1145/3468264.3468563).
  [arXiv:2105.14252](https://arxiv.org/abs/2105.14252)
- Yin, L. et al. (2021). Apache Software Foundation Incubator Project Sustainability Dataset.
  *MSR 2021*. [IEEE](https://ieeexplore.ieee.org/document/9463075)
- Exploring Apache Incubator Project Trajectories with APEX. *MSR 2022*.
  DOI: [10.1145/3524842.3528506](https://doi.org/10.1145/3524842.3528506)
- Avelino, G., Passos, L., Hora, A., Valente, M.T. (2016). A Novel Approach for Estimating Truck
  Factors. *ICPC 2016*. DOI: [10.1109/ICPC.2016.7503718](https://doi.org/10.1109/ICPC.2016.7503718).
  [arXiv:1604.06766](https://arxiv.org/abs/1604.06766)
- Ferreira, M., Mombach, T., Valente, M.T., Bigonha, M. (2019). Algorithms for estimating truck
  factors: a comparative study. *Software Quality Journal* 27, 1583–1617.
  DOI: [10.1007/s11219-019-09457-2](https://doi.org/10.1007/s11219-019-09457-2)
- Nourry, O., Kondo, M., Saito, S., Iimura, Y., Ubayashi, N., Kamei, Y. (2024). Myth: The loss of
  core developers is a critical issue for OSS communities. [arXiv:2412.00313](https://arxiv.org/abs/2412.00313)
- CHAOSS Community. Metric: Elephant Factor. https://www.chaoss.community/kb/metric-elephant-factor/
  (industry knowledge base, not peer-reviewed)
- Steinmacher, I., Conte, T., Gerosa, M.A., Redmiles, D. (2015). Social Barriers Faced by Newcomers
  Placing Their First Contribution in Open Source Software Projects. *CSCW '15*, 1379–1392.
- Steinmacher, I., Conte, T.U., Treude, C., Gerosa, M.A. (2016). Overcoming Open Source Project
  Entry Barriers with a Portal for Newcomers. *ICSE '16*, 273–284.
  DOI: [10.1145/2884781.2884806](https://doi.org/10.1145/2884781.2884806)
- Steinmacher, I., Pinto, G., Wiese, I.S., Gerosa, M.A. (2018). Almost There: A Study on
  Quasi-Contributors in Open-Source Software Projects. *ICSE '18*.
- Rigby, P.C., Storey, M.A. (2011). Understanding Broadcast Based Peer Review on Open Source
  Software Projects. *ICSE 2011*, 541–550.
- Rigby, P.C., Germán, D.M., Cowen, L., Storey, M.A. (2014). Peer Review on Open-Source Software
  Projects: Parameters, Statistical Models, and Theory. *ACM TOSEM* 23(4).
  DOI: [10.1145/2594458](https://doi.org/10.1145/2594458) (author list corrected in this pass —
  Semantic Scholar shows four authors, not two)
- Baysal, O., Kononenko, O., Holmes, R., Godfrey, M.W. (2015). Investigating Technical and
  Non-Technical Factors Influencing Modern Code Review. *Empirical Software Engineering*.
- Kononenko, O., Baysal, O., Godfrey, M.W. (2016). Code Review Quality: How Developers See It.
  *ICSE 2016*.
- McIntosh, S., Kamei, Y., Adams, B., Hassan, A.E. (2016). An Empirical Study of the Impact of
  Modern Code Review Practices on Software Quality. *Empirical Software Engineering*.
  DOI: [10.1007/s10664-015-9381-9](https://doi.org/10.1007/s10664-015-9381-9)
- Hajari, F., Malmir, S., Mirsaeedi, E., Rigby, P.C. (2024). Factoring Expertise, Workload, and
  Turnover into Code Review Recommendation. *IEEE Transactions on Software Engineering*.
  [IEEE Xplore 10444097](https://ieeexplore.ieee.org/document/10444097)
- Yehudi, Y., Goble, C., Jay, C. (2023/2024). Individual context-free online community health
  indicators fail to identify open source software sustainability.
  [arXiv:2309.12120](https://arxiv.org/abs/2309.12120)
- Adejumo, E.K., Johnson, B. (2024/2025). An Empirical Validation of Open Source Repository
  Stability Metrics. [arXiv:2508.01358](https://arxiv.org/abs/2508.01358)
- Raman, N., Cao, M., Tsvetkov, Y., Kästner, C., Vasilescu, B. (2020). Stress and Burnout in Open
  Source: Toward Finding, Understanding, and Mitigating Unhealthy Interactions. *ICSE-NIER '20*.
  DOI: [10.1145/3377816.3381732](https://doi.org/10.1145/3377816.3381732)
- Miller, C., Cohen, S., Klug, D., Vasilescu, B., Kästner, C. (2022). "Did You Miss My Comment or
  What?" Understanding Toxicity in Open Source Discussions. *ICSE '22*, 710–722.
  DOI: [10.1145/3510003.3510111](https://doi.org/10.1145/3510003.3510111)
- Sarker, J., Turzo, A.K., Dong, M., Bosu, A. (2023). Automated Identification of Toxic Code
  Reviews Using ToxiCR. *ACM TOSEM* 32(1), Article 1. DOI: [10.1145/3583562](https://doi.org/10.1145/3583562)
- Ferreira, I., Cheng, J., Adams, B. (2021). The "Shut the f**k up" Phenomenon: Characterizing
  Incivility in Open Source Code Review Discussions. *Proc. ACM Hum.-Comput. Interact.* 5(CSCW2),
  Article 353. DOI: [10.1145/3479497](https://doi.org/10.1145/3479497)
- Ferreira, I., Rafiq, A., Cheng, J. (2023). Incivility Detection in Open Source Code Review and
  Issue Discussions. Preprint, *Journal of Systems and Software*.
  [arXiv:2206.13429](https://arxiv.org/abs/2206.13429)
- Calefato, F., Lanubile, F., Maiorano, F., Novielli, N. (2018). Sentiment Polarity Detection for
  Software Development. *Empirical Software Engineering* 23(3), 1352–1382.
  DOI: [10.1007/s10664-017-9546-9](https://doi.org/10.1007/s10664-017-9546-9)
- Jongeling, R., Sarkar, P., Datta, S., Serebrenik, A. (2017). On negative results when using
  sentiment analysis tools for software engineering research. *Empirical Software Engineering*
  22(5), 2543–2584. DOI: [10.1007/s10664-016-9493-x](https://doi.org/10.1007/s10664-016-9493-x)
- Gonçalves, P.W., Çalıklı, G., Bacchelli, A. (2022). Interpersonal Conflicts During Code Review:
  Developers' Experience and Practices. *Proc. ACM Hum.-Comput. Interact.* 6(CSCW1), Article 33.
  DOI: [10.1145/3512945](https://doi.org/10.1145/3512945)
- Qiu, H.S., Vasilescu, B., Kästner, C., Egelman, C.D., Jaspan, C., Murphy-Hill, E. (2022).
  Detecting Interpersonal Conflict in Issues and Code Review: Cross Pollinating Open- and
  Closed-Source Approaches. *ICSE-SEIS '22*. DOI: [10.1145/3510458.3513019](https://doi.org/10.1145/3510458.3513019)
  (title/authors/DOI verified via Semantic Scholar; an earlier draft of this document wrongly
  attributed this DOI to Gonçalves, Çalıklı, and Bacchelli — corrected in this pass; full
  abstract/findings not independently confirmed, ACM access blocked)
- Gunawardena, S.D., Devine, P., Beaumont, I., Garden, L.P., Murphy-Hill, E., Blincoe, K. (2022).
  Destructive Criticism in Software Code Review Impacts Inclusion. *Proc. ACM Hum.-Comput.
  Interact.* 6(CSCW2), Article 292. DOI: [10.1145/3555183](https://doi.org/10.1145/3555183)

---

## 11. Could not verify — consolidated

- **CHAOSS**: no standalone KB page found for "Types of Contributions" or "Contributor Retention" as
  independently named metrics; current (2026) meeting cadence of the Risk and Value working groups;
  GrimoireLab's current governance ownership relative to CHAOSS vs. LF/Bitergia; SortingHat's
  merge-candidate-recommendation algorithm and whether any large-scale deployment auto-applies
  merges without human confirmation.
- **CNCF/LFX**: DevStats' exact current commit velocity (evidence supports "operational," not
  precisely dated); whether OpenSSF badge tier for CNCF graduation is "passing" only vs.
  "silver/gold" (primary template text says "passing"; an earlier search-summarized source claimed
  "silver/gold" — treat the primary source as authoritative and the discrepancy as flagged); LFX
  Insights' exact current identity-resolution algorithm; the specific claim that "LFX Insights
  implements CHAOSS metrics" (only shared GrimoireLab ancestry is confirmed).
- **ASF**: the exact underlying data sources reporter.apache.org auto-populates into its report
  template (tool is committer-auth-gated; only the reporting-guidance page's description of it was
  available).
- **Academic — Apache incubator**: "Code, Quality, and Process Metrics in Graduated and Retired ASFI
  Projects" (found only via a single secondary ASF-blog summary, no confirmed DOI/venue — do not
  cite without further verification); exact ML algorithm/cross-validation protocol in Yin et al.
  2021 (headline 93%+ accuracy figure is confirmed from the paper's own abstract; methodological
  detail was not independently extracted).
- **Academic — truck factor**: full content of Ferreira et al. 2019's comparative-algorithm
  findings (citation verified, content not independently re-extracted); Cosentino/Izquierdo-Cabot/
  Cabot's ICPC 2017 comparison paper and a Torchiano/Ricca truck-factor-threshold paper (both
  located via citation only, not independently confirmed in detail).
- **Academic — sustainability**: "Venters et al. 2014" on software sustainability as a
  non-functional requirement (repeatedly cited by others, never independently located as a primary
  source in this research pass — do not cite without further verification); Adejumo & Johnson's
  specific tested metrics and correlation results (citation verified, content not deeply extracted).
- **Academic — toxicity/incivility**: Ferreira, Rafiq & Cheng's final journal DOI (preprint DOI
  confirmed via arXiv; journal-of-record DOI not independently confirmed); Qiu et al.'s ICSE-SEIS
  2022 conflict-detection paper's abstract/findings beyond title/authors/DOI (title, correct
  author list, and DOI confirmed via Semantic Scholar in this pass — an earlier draft of this
  document had misattributed this DOI to Gonçalves et al.; content itself blocked by ACM access
  restrictions); Gunawardena et al.'s exact prevalence percentages (confirmed present in the
  paper's own abstract/intro, not independently extracted from the full results section); Senti4SD's
  precise underlying learning algorithm (feature set confirmed, algorithm family not independently
  re-confirmed); an emerging 2025 argumentation-mining paper on OSS usability discussions
  (arXiv:2512.08032) — title only, not read or verified, not usable as a citation as-is.

Where any of the above is later needed for METRICS.md, SCORING.md, or COMMUNITY-HEALTH.md with more
precision than this document provides, re-verify directly against the primary source rather than
propagating the flagged uncertainty forward.
