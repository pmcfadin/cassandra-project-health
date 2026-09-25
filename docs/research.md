# Open Source Project Health — Research and Project Bootstrap

I want to create an open-source project that measures, analyzes, and visualizes the health of open-source software communities.

The long-term goal is a system that can run automatically every night against an open-source project, collect publicly available project/community data, calculate reproducible health metrics, classify community interactions, preserve historical measurements, and publish a rich static dashboard through GitHub Pages.

Apache Cassandra should be the initial reference project, but the system must be designed to work with other GitHub/open-source projects without embedding Cassandra-specific assumptions into the core metrics.

## Primary Research Question

**Can we construct a transparent, reproducible, evidence-based model for measuring the health of an open-source project—including both engineering activity and the health of human interactions within the community?**

The project should investigate this question before committing to a scoring model or architecture.

## Research Existing Work

Start with a thorough investigation of existing approaches to open-source project health.

Research at minimum:

- CNCF project-health practices and metrics
- Linux Foundation / LFX Insights
- CHAOSS metrics, software, and working groups
- Apache Software Foundation community-health practices
- GitHub community/project metrics
- academic research on open-source project sustainability
- contributor retention research
- bus-factor and contributor-concentration research
- organizational/“elephant factor” measurements
- code-review health and reviewer concentration
- community sentiment analysis
- toxicity detection in software-development communities
- toxicity research involving GitHub, mailing lists, issue trackers, Slack, Discord, Stack Overflow, etc.
- newcomer experience and contributor-retention research
- research connecting communication patterns with contributor abandonment or retention
- methods for measuring constructive versus destructive disagreement

For each relevant system or paper, determine:

1. What does it measure?
2. What data does it require?
3. How is the metric calculated?
4. Is the calculation deterministic?
5. Is there empirical validation?
6. What are its limitations?
7. Can it be reproduced from publicly available data?
8. What ideas should this project adopt or avoid?

Document sources and methodology carefully.

## Core Design Principle: Evidence Before Scores

Do not begin by inventing a single arbitrary “health score.”

First identify measurable dimensions of project health and determine which have evidence supporting their usefulness.

Separate metrics into at least two categories.

### Deterministic Metrics

Metrics calculated directly from observable project data, such as:

- active contributors
- new contributors
- contributor retention
- contribution frequency
- PR volume
- PR response latency
- PR merge latency
- issue response latency
- stale PRs/issues
- release frequency
- release regularity
- reviewer population
- reviewer concentration
- contributor concentration
- organizational concentration
- bus factor
- elephant factor
- contributor funnel/conversion
- contributor tenure
- maintainer/committer growth
- newcomer-to-regular-contributor conversion

Investigate mathematically defensible measures such as HHI, effective population size, concentration ratios, distributions, percentiles, cohort analysis, survival analysis, and historical baselines.

### Classified / Probabilistic Metrics

Human communication requires interpretation and must be treated differently.

Investigate methods for analyzing Slack conversations, mailing-list discussions, GitHub issues, PR discussions, and other community communication.

Do **not** equate negative sentiment with unhealthy communication.

Technical disagreement can be intense and still be productive.

Explore classification of observable interaction phenomena including:

- technical disagreement
- constructive counterargument
- personal attack
- hostility
- dismissiveness
- sarcasm
- gatekeeping
- status/authority invocation
- pile-ons
- escalation
- de-escalation
- acknowledgment
- evidence-based argument
- compromise
- resolution
- thread abandonment
- newcomer treatment

Investigate whether these can form higher-level measurements such as:

- interaction health
- disagreement safety
- escalation rate
- constructive resolution rate
- newcomer interaction quality
- toxicity rate
- community inclusiveness

Sentiment may be collected, but it should not automatically be treated as a proxy for community health.

## Thread-Level Analysis

Research whether the appropriate unit of communication analysis is the **conversation/thread rather than the individual message**.

A thread should potentially contain:

- participants
- messages
- timestamps
- reply relationships
- participant roles
- topic
- sentiment trajectory
- disagreement events
- toxicity events
- escalation/de-escalation
- resolution
- participant abandonment
- subsequent participation

Investigate methods for modeling the trajectory of a conversation.

For example:

    neutral
      → disagreement
      → strong disagreement
      → technical resolution

should be distinguishable from:

    neutral
      → disagreement
      → dismissal
      → personal attack
      → pile-on
      → abandonment

## Contributor Funnel

Investigate treating community participation as a funnel:

    first contribution
          ↓
    second contribution
          ↓
    5 contributions
          ↓
    sustained contributor
          ↓
    committer/maintainer
          ↓
    project leadership

Measure conversion rates and cohort behavior over time.

Explore whether communication experiences correlate with movement through or abandonment of this funnel.

For example:

    P(return | constructive interaction)

versus:

    P(return | hostile interaction)

Be extremely careful to distinguish **correlation from causation**.

## Reviewer Health

Investigate reviewer concentration separately from contributor concentration.

Potential measures include:

- unique reviewers/month
- reviews by top 1/3/5/10 reviewers
- reviewer HHI
- effective reviewer population
- merge-authority concentration
- review latency
- review load
- contributor-to-reviewer ratio

A project may have hundreds of contributors while depending on only a handful of people capable of reviewing and merging work.

This should be visible.

## Historical Analysis

The system must prioritize trends over snapshots.

Design metrics so we can answer questions such as:

- Is contributor diversity improving?
- Are fewer newcomers returning?
- Is review latency increasing?
- Is reviewer concentration increasing?
- Is organizational concentration changing?
- Is communication becoming more hostile?
- Are disagreements becoming less likely to resolve constructively?
- Are governance discussions more toxic than technical discussions?
- Did major releases or governance changes affect community behavior?

Where appropriate, compare a project's current state against its own historical baseline rather than arbitrary universal thresholds.

## Data Sources

Research practical access to:

- Git repositories
- GitHub REST API
- GitHub GraphQL API
- GitHub Issues
- GitHub Pull Requests
- GitHub Discussions
- GitHub releases/tags
- Apache mailing-list archives
- public Slack archives where legally/ethically accessible
- ASF project metadata
- contributor/committer/PMC metadata
- organization affiliation data

Determine:

- historical availability
- API limits
- incremental collection strategy
- identity resolution challenges
- bot detection
- deleted/edited messages
- privacy concerns
- licensing/terms-of-service constraints

Do not assume a data source is available until verified.

## Identity Resolution

Research how existing systems such as CHAOSS/LFX handle contributor identity.

A single person may appear as:

    Patrick McFadin
    pmcfadin
    patrick@example.com
    different git email addresses
    Slack identity
    mailing-list identity

Determine what can be resolved reliably and what should remain uncertain.

Never silently merge uncertain identities.

## Classification Architecture

For subjective communication analysis, investigate a versioned classification pipeline:

    source messages
          ↓
    immutable normalized conversation
          ↓
    versioned classifier
          ↓
    structured observations
          ↓
    deterministic aggregation
          ↓
    community metrics

Prefer structured classification over asking an LLM to produce arbitrary numerical toxicity scores.

For example, investigate schemas resembling:

    technical_disagreement
    personal_attack
    dismissiveness
    gatekeeping
    sarcasm
    constructive_counterargument
    deescalation
    resolution
    confidence

The scoring system should convert classifications into metrics rather than allowing the model to determine the final project score directly.

## Classifier Validation

Design a methodology for validating communication classifiers.

Investigate:

- human-labeled benchmark datasets
- multiple independent human raters
- inter-rater agreement
- precision
- recall
- F1
- confusion matrices
- calibration
- false-positive analysis
- classifier drift
- model-version comparisons

A new model or prompt must not silently rewrite historical project health.

Every classification should record enough provenance to reproduce it:

    classifier version
    model/version
    prompt/schema version
    confidence
    input hash
    timestamp

Consider maintaining a frozen human-labeled benchmark corpus against which classifier changes are tested.

## Ethics and Responsible Reporting

Research the ethical implications of analyzing community communication.

The purpose is to understand **community systems and interactions**, not create public rankings of “toxic people.”

Prefer aggregate reporting such as:

    dismissive interactions / 1,000 messages
    escalation rate
    constructive resolution rate
    newcomer interaction quality

over individual toxicity scores.

Determine appropriate rules for:

- minimum sample sizes
- anonymization
- public versus private data
- quotations
- individual attribution
- uncertainty
- false positives
- classifier disagreement
- appeals/corrections
- historical data retention

The system should make it difficult to weaponize uncertain classifications against individual contributors.

## Reproducibility

A central requirement is auditability.

For every metric displayed, it should eventually be possible to inspect:

    metric definition
    raw value
    population
    time window
    exclusions
    source
    normalization function
    classifier version, if applicable
    weight, if applicable
    final calculation

Someone should be able to reproduce a deterministic metric independently from the same source data.

## Scoring

Research scoring approaches, but do not prematurely select one.

Compare:

- no aggregate score at all
- dimension scores only
- weighted composite score
- percentile/benchmark scores
- historical-baseline scores
- maturity-adjusted scores
- statistical anomaly detection

If an aggregate score is eventually used, the weighting and normalization functions must be explicit, versioned, and auditable.

Avoid hiding important deterioration behind a single composite number.

## Proposed Technical Direction

Evaluate, rather than blindly adopt, a lightweight architecture roughly like:

    collectors
        ↓
    normalized data
        ↓
    Parquet
        ↓
    DuckDB / Polars
        ↓
    metric calculations
        ↓
    versioned classifications
        ↓
    historical snapshots
        ↓
    static-site generator
        ↓
    GitHub Pages

The target operational model is:

    GitHub Actions
          ↓
    nightly incremental collection
          ↓
    calculate/update metrics
          ↓
    update historical datasets
          ↓
    generate charts/tables/pages
          ↓
    publish GitHub Pages

Prefer static artifacts and reproducible datasets over introducing databases and services unless research demonstrates that they are necessary.

## Initial Reference Implementation

Use Apache Cassandra as the initial case study.

However:

**Do not make the framework Cassandra-specific.**

The eventual interface should conceptually support:

    analyze apache/cassandra
    analyze apache/kafka
    analyze apache/airflow
    analyze kubernetes/kubernetes

Project-specific adapters should handle differences such as ASF mailing lists without contaminating the general metric definitions.

## Deliverables for the Research Phase

Before implementing the full system, produce:

1. `RESEARCH.md`
   - literature and existing-system review
   - CNCF/LFX/CHAOSS comparison
   - relevant academic research
   - communication/toxicity research
   - citations and links

2. `METRICS.md`
   - candidate metrics
   - definitions
   - formulas
   - required data
   - strengths/weaknesses
   - deterministic vs classified designation

3. `COMMUNITY-HEALTH.md`
   - proposed communication taxonomy
   - thread-level model
   - disagreement versus toxicity model
   - validation strategy
   - ethical considerations

4. `DATA-SOURCES.md`
   - available sources
   - APIs
   - historical depth
   - rate limits
   - licensing/privacy considerations
   - incremental collection strategies

5. `ARCHITECTURE.md`
   - proposed pipeline
   - storage format
   - provenance model
   - classifier architecture
   - nightly execution model
   - GitHub Pages publishing model

6. `SCORING.md`
   - candidate scoring approaches
   - normalization options
   - historical-baseline strategy
   - uncertainty representation
   - recommendation for what should and should not be combined into aggregate scores

7. `ROADMAP.md`
   - smallest useful prototype
   - research/validation milestones
   - Cassandra pilot
   - expansion to additional projects

8. Initial repository skeleton suitable for implementing the prototype after the research phase.

## Research Standard

Do not merely collect blog posts or repeat existing frameworks.

Trace important claims back to primary sources, documentation, papers, datasets, or source code whenever possible.

Challenge assumptions.

Explicitly identify areas where the evidence is weak.

Separate:

- established measurements
- reasonable proxies
- experimental metrics
- subjective classifications

The objective is not to manufacture a health score.

The objective is to determine **what can legitimately be measured about the health and sustainability of an open-source community, how reliably it can be measured, and how those measurements can be made transparent enough that the community itself can challenge and improve them.**

Begin with the research phase. Do not build the full dashboard yet. Produce the research documents, proposed metric taxonomy, data model, architecture, and prioritized prototype plan first.
