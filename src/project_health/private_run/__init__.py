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
- `identity` (issue #114) -- the per-`--out` author salt used only to hash
  author identity for the per-message private index (`message_index.py`);
  every other computation in this package works on raw, in-memory-only
  author strings, exactly like the message-level pipeline already does.
- `thread_derive` (issue #114) -- COMMUNITY-HEALTH.md §2.2's intensity
  tiers and §2.3's deterministic thread-level derivation (escalation,
  de-escalation, pile-on, resolution, abandonment-after-friction), computed
  purely from message-level labels and the reply graph, never the LLM
  (§1.3).
- `thread_aggregate` (issue #114) -- §5.1-floored, thread-weighted
  aggregation of the four §5.2 thread-level rates plus the two newcomer
  response rates (§2.3 rule 8), with thread-level bootstrap 95% CIs.
- `newcomer` (issue #114) -- newcomer determination against the *whole*
  Phase-1 author history for a venue (`frame.load_dev_author_history`/
  `load_jira_author_history`), not just this run's sample.
- `message_index` (issue #114) -- the per-message private index written to
  `--out/message_index.jsonl`: venue, thread key, position, timestamp,
  parent, a salted `author_key`, and the `input_hash` joining to the Jev
  cache. Never read by `report.md`/`aggregates.json`.
"""

from __future__ import annotations
