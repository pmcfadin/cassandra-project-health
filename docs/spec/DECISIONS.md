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

## D13. Site structure: separate pages with tab-style navigation
Three top-level pages share one nav bar: `/community/` (code and contributor metrics), `/conversations/` (mailing lists and, later, Slack) and `/governance/` (per-commit minimums). Each page has a stable, linkable URL and works without JavaScript. The home page summarizes all three and links to them.

## D14. Governance: per-commit minimums from a versioned, owner-approved policy
- The minimums every commit must meet (e.g. review, testing, CI) are defined in `governance-policy.yaml`. Each rule cites its source (Cassandra's commit documentation, a CEP, or a dev@ decision), carries an effective-from date, and states exactly how it is checked.
- An agent drafts the policy from Cassandra's documented rules. The project owner approves it before any compliance is published.
- Each commit is judged against the policy in force on its commit date. A policy change is a dated version bump and never rescores history silently (D2 rule 6).
- Each check reports `pass`, `fail` or `unknown`. Unknown means the evidence isn't available, and it is never counted as a fail.

## D15. Governance transparency: full per-commit detail, including names
Owner decision (2026-09-25): the governance page lists individual commits with author, committer and reviewer names alongside each check result. These are deterministic facts about public commits, not classified judgments, so D2 rule 4 (which covers classified output) does not forbid it. Because named results raise the cost of a false positive:
- every check result shows the evidence it is based on (the trailer text, the JIRA field, the CI link or the test paths), so a reader can verify it;
- `unknown` is displayed as unknown, never as a fail;
- every row links to the corrections process, and a correction is a PR-reviewed override with the reason recorded;
- rules are applied only from their effective date.

## D16. Conversations page ships now with metadata-only metrics
Before the Phase 2 gates clear (D1), `/conversations/` shows mailing-list metrics computed from metadata only: sender, timestamp and thread structure, never message bodies. Examples are time to first reply on dev@ and the unanswered-thread rate. The page explains that interaction-health metrics arrive after classifier validation (Phase 2a) and that Slack arrives after PMC and ASF Infra approval (Phase 2b).

## D17. Classifier provider: TypeSafe Jev
Owner decision (2026-09-25): all communication classification (Phase 2a mailing lists, JIRA comments and PR comments; later Phase 2b Slack) uses TypeSafe's Jev System One model through the `typesafe-sdk` Python SDK. This replaces the Anthropic-based classifier assumed in `COMMUNITY-HEALTH.md` §4 and `ARCHITECTURE.md` §6. The provider-agnostic classifier interface stays.
- **Pinned model.** Requests pin a versioned id (e.g. `jev-1.13.0`), never `jev-latest`, and the answering version from the response's `model` field is stored with every observation. A model change is a classifier version bump, validated against the frozen benchmark before it is used (D2 rule 6).
- **Labels as probabilities.** Each message-level label is a Noul question (multi-label), and intensity is a Score. Raw probabilities are stored, and thresholds are applied in code, per label, calibrated against the human-labeled benchmark. Re-thresholding therefore never needs re-inference.
- **Thread-level outcomes stay deterministic.** Escalation, pile-on, resolution and abandonment are derived in code from message-level observations (`COMMUNITY-HEALTH.md` §2). Jev never judges a whole thread, and counting is never delegated to the model.
- **Secret.** `TYPESAFE_API_KEY` is set locally from `.env`, which is gitignored, and as a GitHub Actions repo secret for scheduled runs. D10's owner-funded monthly cap now applies to TypeSafe usage.
- **Phase 2 gates are unchanged.** Nothing classified is published until the benchmark and agreement gates in `COMMUNITY-HEALTH.md` §6 pass. Slack text is sent to TypeSafe only after PMC and ASF Infra approval (D1), and that approval request names TypeSafe as a third-party processor.

## D18. Phase 2a Jev pilot
Owner decisions (2026-09-25):
- **Purpose.** A pilot before the full benchmark (`COMMUNITY-HEALTH.md` §6). It tests whether the taxonomy is workable, how Jev compares with a human rater label by label, how long rating takes, and whether the transient text pipeline works end to end. The results size the full benchmark. Nothing from the pilot is published except aggregate numbers.
- **Scope.** 250 messages from dev@ and JIRA comments, 2017–2026: a 150-message prevalence stratum sampled in proportion to source volume and time, and a 100-message rare-label enrichment stratum from a keyword pre-filter. The two strata are never mixed when estimating prevalence. GitHub PR comments come later.
- **Rater.** The project owner rates alone for the pilot, so there is no inter-rater agreement statistic yet. More raters, including at least one without a Cassandra PMC or committer affiliation, join before the full benchmark (§6.2). Jev's answers are compared against the human labels and never count as a rater.
- **Tool.** A labeling page hosted as a claude.ai artifact with a shared database and viewer identity. It is private by default, shared only with raters, and exportable.
- **Where labels live.** The messages are public, but the labels are unvalidated judgments about named people, so the pilot corpus and its labels stay private (the artifact's database plus a private repo, `pmcfadin/cassandra-project-health-benchmark`). The public repo holds the sampling code, the versioned Jev question set and aggregate results. Publishing the corpus can be revisited once it is multi-rater and validated.
- **Text handling.** Message bodies are fetched only for the moment of classification or labeling and are never written to the public repo or the `data` branch (D1). Pilot texts live only in the private corpus.
