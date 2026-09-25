# Specification

The spec for cassandra-project-health. The original brief is [`../research.md`](../research.md).

Read in this order:

| Doc | What it settles |
|---|---|
| [DECISIONS.md](DECISIONS.md) | Binding decisions: phases, principles, storage, scoring, cadence, affiliation, stack, repo. Every other doc must agree with this one. |
| [RESEARCH.md](RESEARCH.md) | Review of existing systems (CHAOSS, CNCF, LFX, ASF, GitHub, OpenSSF) and the academic literature, with an evidence tier for each claim. |
| [DATA-SOURCES.md](DATA-SOURCES.md) | Each source checked live: access, history depth, limits, incremental strategy, identity resolution, and required secrets. |
| [data-probe.md](data-probe.md) | Measured Cassandra volumes. The Corrections section overrides the earlier sections of the file. |
| [METRICS.md](METRICS.md) | About 40 metrics: formula, population, window, tier, phase, direction of good, and key/supporting role. |
| [SCORING.md](SCORING.md) | Status for each dimension against a baseline of the last 24 completed months. Worst key metric wins. No composite score. |
| [COMMUNITY-HEALTH.md](COMMUNITY-HEALTH.md) | Phase 2: communication taxonomy, thread model, classifier provenance, validation gates, ethics. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Pipeline, data model, orphan `data` branch storage, provenance, Actions workflows, site layout, and testing. |
| [ROADMAP.md](ROADMAP.md) | Milestones from the smallest prototype through Phase 3 (Kafka), with exit criteria and risks. |
| [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) | 17 questions for the owner, each with a recommended default. |
