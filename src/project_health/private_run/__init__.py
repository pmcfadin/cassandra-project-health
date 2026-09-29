"""Private, owner-only run over the full Cassandra communication history
(issue #110; DECISIONS.md D1, D10, D17, D18, D22, D23; COMMUNITY-HEALTH.md
§1.2, §4, §5, §7).

**Nothing produced by this package is published** to the site, the public
repo, or the `data` branch -- COMMUNITY-HEALTH.md §7.8 requires a PMC
preview and explicit acknowledgment before any Phase 2a metric goes live
publicly, and this run does not attempt that; it exists only to answer the
project owner's own "what does conversation-pattern analysis find for
Cassandra?" question, privately, per the issue's own framing.

Modules:

- `quarters` -- calendar-quarter string utilities (`YYYYQN`).
- `frame` -- builds the population-of-threads sampling frame per (venue,
  quarter) stratum from the locally cached Phase 1 raw tables
  (`project_health.storage.read_table`), never a live crawl.
- `sample` -- seeded, reproducible stratified thread sampling
  (`classify.sample.deterministic_sample`) plus per-stratum inclusion
  weights (population / sampled).
- `stats` -- weighted "rate per 1,000 messages" plus a thread-level
  (cluster) nonparametric bootstrap 95% CI.
- `sensitivity` -- parses `docs/benchmark/public-v1.md` for the D23 strong,
  gating public-benchmark mappings (`personal_attack`, `hostility`,
  `sarcasm`) and picks one best-F1 threshold per label.
- `aggregate` -- per-cell (venue × quarter, venue × year) aggregation,
  applying COMMUNITY-HEALTH.md §5.1's minimum-sample floors.
- `report` -- renders the private `report.md` (aggregate-only: no message
  text, no names, no message ids, per COMMUNITY-HEALTH.md §7.3).
- `runner` -- end-to-end orchestration: sample -> fetch (transient, never
  persisted) -> classify (pinned Jev, D10 cost-capped, input-hash cached
  and therefore resumable) -> aggregate -> report/aggregates.json.
"""

from __future__ import annotations
