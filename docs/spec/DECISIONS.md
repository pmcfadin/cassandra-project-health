# Decisions Log

Decisions made with the project owner (Patrick McFadin) during spec brainstorming, 2026-09-25.
Every spec document in `docs/spec/` must be consistent with these. The original brief is `docs/research.md`.

## D1. Scope is phased
- **Phase 0 — Research.** The seven research deliverables from `docs/research.md`, written as inputs to the spec.
- **Phase 1 — Deterministic MVP.** Git, ASF JIRA, GitHub PRs, mailing-list *metadata* (sender, timestamp, thread structure — not content), releases. Nightly dashboard plus a frozen monthly report.
- **Phase 2a — Classification.** Mailing lists (dev@, user@), JIRA comments, GitHub PR comments. Gated: begins only after a human-labeled benchmark corpus exists and the classifier clears agreement/precision gates against it.
- **Phase 2b — ASF Slack.** Gated: requires PMC consensus on dev@ and ASF Infra approval of a read-only bot on public channels (#cassandra, #cassandra-dev). Raw Slack text is never committed to the public repo or data branch; it is classified in-run and only aggregate counts persist. No quotes, no links, no individual attribution.
- **Phase 3 — Expansion.** A second project (Apache Kafka — same JIRA + lists shape) to prove the core has no Cassandra-specific code.

## D2. Principles (rules, not aspirations)
1. Evidence before scores. Every metric carries a tier: `established`, `proxy`, `experimental`, `classified`. Displayed wherever the metric appears.
2. Trends over snapshots. Status compares the project to its own history, never to universal thresholds.
3. Every number is auditable: definition, formula, population, window, exclusions, source, version — linked from the chart.
4. Aggregates, not individuals, for anything classified. No per-person toxicity/hostility scores ever.
5. Uncertainty is shown: unknown affiliations stay `unknown`; identities below a confidence threshold are not merged; small samples render as "insufficient data".
6. Nothing changes silently. A metric or classifier version bump triggers a visible, full-history recompute and a changelog entry.
7. Non-goals: ranking people, a single headline health number, causal claims.

## D3. Storage: hybrid
- Raw source data is cached incrementally (nightly deltas) as Parquet, stored off `main` — an orphan `data` branch or GitHub Release assets (the architecture doc should recommend one with size estimates).
- Metrics are **recomputed from the full raw cache on every run**, stamped with a metric-definition version.
- Each run's computed outputs are also preserved as dated snapshots.
- A definition change recomputes all history under the new version, explicitly, never mixing versions silently.

## D4. Scoring: dimensions + historical baseline
- No composite/overall score.
- Dimensions (initial): contributor sustainability, reviewer capacity, responsiveness, organizational diversity, release cadence; later interaction health (phase 2).
- Each dimension gets a status vs. the project's own trailing baseline: `improving` / `stable` / `declining` / `insufficient data`. Raw values and the calculation are one click away.

## D5. Cadence: nightly + monthly editions
- Nightly: incremental collection, recompute, refresh the live dashboard.
- 1st of each month: freeze a permanent dated monthly report (citable from dev@ or ASF board reports).
- Status calls use completed months only; partial months never drive status.

## D6. Organizational affiliation: curated file + heuristics
- Seed from email domains and GitHub profile `company` fields.
- A reviewed `affiliations.yaml` in the repo with dated ranges (like CNCF gitdm). Contributors can PR their own entries.
- Unresolved = `unknown`, never guessed. Only aggregate org shares published — no per-person affiliation listings on the site.

## D7. Communication sources (phase 2)
dev@/user@ mailing lists, JIRA comments, GitHub PR comments; ASF Slack under D1 gating rules.

## D8. Stack
- Pipeline: Python, DuckDB over Parquet (Polars acceptable where it helps).
- Site: Python + Jinja2 templates + Vega-Lite charts, fully static. Every chart's data is published as a downloadable JSON/CSV beside it.
- CI: GitHub Actions; publish via GitHub Pages.

## D9. Repository
- GitHub repo: `pmcfadin/cassandra-project-health` (public, personal account), published to GitHub Pages.
- The core stays project-agnostic: projects are added via config + adapters; the name is branding only.

## Cassandra-specific facts that shape the design (verify during research)
- Issues and most review discussion live in ASF JIRA (`issues.apache.org/jira`, project key `CASSANDRA`), not GitHub Issues.
- Commits follow the convention `patch by X; reviewed by Y for CASSANDRA-NNNNN` — likely the best source for reviewer metrics.
- Design debate (incl. CEPs) happens on `dev@cassandra.apache.org`, archived at lists.apache.org (Pony Mail, with a JSON API).
- GitHub PRs exist on `apache/cassandra` but cover only part of review activity.

## D10. Phase 2a API cost
The project owner pays for classifier usage (Anthropic Batches API) with a personal key stored as a repo secret and a monthly spending cap. If a run hits the cap, classification pauses and the run manifest and site show a visible gap; the rest of the pipeline keeps running.

## D11. Community engagement timing
The Cassandra community first hears about the project on dev@ once the Phase 1 prototype exists, so the announcement has something concrete to show. It is not announced at the spec stage. The repo is public from the start but not promoted until then. Phase 2 (communication analysis) is raised as its own dev@ discussion before any benchmark labeling begins.

## D12. Open-question defaults
The recommended defaults in `OPEN-QUESTIONS.md` are accepted (owner decision, 2026-09-25). They can be revisited as pilot data arrives.
