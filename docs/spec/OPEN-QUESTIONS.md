# Open Questions for the Project Owner

A single, deduplicated list of every open question for the project owner (Patrick McFadin) found across
`docs/spec/*.md`, compiled by grepping each document for "open question", "unverified", "TBD", and "owner".
Purely technical/engineering follow-ups that don't need owner input (e.g., confirming an unexercised API's
exact response shape) are left in each document's own "Unverified" section and not duplicated here. Two
items that appeared as "open questions" in `ROADMAP.md` §5 were found, during this review, to already be
answered elsewhere in the spec set (`COMMUNITY-HEALTH.md` §6.3/§6.4) — `ROADMAP.md` has been corrected in
place; they are not repeated below.

Each item lists its source and a recommended default — a reasonable starting position to proceed with if the
owner doesn't weigh in before it's needed, not a claim that the decision has been made.

## Phase 1 — Deterministic MVP

1. **Identity-merge confidence tiers**: which specific heuristics count as `exact`/`high` (auto-mergeable)
   vs. `medium`/`low` (human-review-required)? E.g., is a GitHub-noreply-email → login mapping `exact` or
   `high`? — `ROADMAP.md` §5.4, `ARCHITECTURE.md` §3.
   **Default:** treat exact string matches (same email, same login) as `exact`; treat the noreply-email→login
   pattern and other mechanically-derived-but-not-literal matches as `high`; everything else `medium`/`low`.
2. **Reviewer-source conflict resolution**: when the commit trailer and the JIRA reviewer field(s) disagree
   for the same change, does `unique_reviewers_monthly`/`reviewer_hhi` use the union, the intersection, or
   prefer one source? — `ROADMAP.md` §5.5, `METRICS.md` §0.4.
   **Default:** union (credit a reviewer named by either source), with the per-source breakdown always shown
   per `METRICS.md`'s existing auditability language, and a mismatch flagged as a data-quality signal rather
   than silently resolved.
3. **Bot-pattern maintenance**: where's the line for automation accounts run by a human (e.g., a
   release-manager script), and who owns updating `bot_patterns` as edge cases surface? — `ROADMAP.md` §5.6.
   **Default:** the project owner reviews and merges `bot_patterns` changes the same way as `affiliations.yaml`
   (D6) — PR-based, dated, auditable — starting from the placeholder list in `projects/cassandra.yaml`.
4. **Minimum sample size / "insufficient data" floors**: do the floors in `METRICS.md` §0.6 need to vary
   per metric given Cassandra's small absolute numbers (e.g., 49 PMC members)? — `ROADMAP.md` §5.11.
   **Default:** keep `METRICS.md` §0.6's stated floors (5 for rate/ratio and concentration metrics, 30 for
   cohort/survival, 5 for latency medians) as binding; revisit only if the Phase 1 pilot shows them
   systematically too strict or too loose for a specific metric.
5. **Metric-definition-version retention**: are superseded `metric_value` rows kept forever or pruned after
   some period? — `ROADMAP.md` §5.9.
   **Default:** keep forever (append-only), consistent with D2 rule 6 and D3; the projected `data`-branch size
   (comfortably under 500 MB through Phase 1/2a per `ARCHITECTURE.md` §4.2) doesn't currently justify pruning.
6. **Cassandra 4.0 freeze date range**: the exact stabilization-freeze window hasn't been pulled from real
   release history, so `structural-breaks.yaml` can't be populated yet. — `SCORING.md` §11, §6.
   **Default:** confirm the date range from `archive.apache.org`/JIRA fixVersion history before Phase 1's M3
   ships; until confirmed, `release_regularity`/`time_since_last_release` stay `none`-direction (already the
   case), so nothing downstream is blocked on this.
7. **COVID-era (2020–2021) anomaly**: whether Cassandra's own contribution data shows a genuine,
   measurable disruption in this window is an open empirical question, not assumed. — `SCORING.md` §11.
   **Default:** no structural-break entry until verified against real data; treat any 2020–2021 deviation as
   ordinary baseline variation until then.
8. **Corrections/appeals SLA**: is there a committed response time for the `/corrections/` page, and who
   adjudicates (owner alone, or a wider reviewer group)? — `ROADMAP.md` §5.10.
   **Default:** owner-adjudicated with no fixed SLA at launch; revisit once real dispute volume exists.

## Phase 2a — Classification

9. **Who rates the benchmark corpus**: community volunteers (domain expertise, possible in-group bias) vs.
   paid/independent raters (less bias, less context)? — `ROADMAP.md` §5.3, `COMMUNITY-HEALTH.md` open
   questions #2.
   **Default:** a mixed panel — at least one rater with no current Cassandra PMC/committer affiliation, as
   `COMMUNITY-HEALTH.md` §6.2 already requires — recruited before benchmark labeling starts, since it's on
   the critical path.
10. **Resolution-corroboration sequencing**: wire in JIRA-status/PR-merge signals as corroborating evidence
    for thread `resolved` calls at Phase 2a launch, or defer to a later iteration? — `COMMUNITY-HEALTH.md`
    open questions #3.
    **Default:** defer to a later iteration; message-level evidence alone (§2.3) is sufficient to ship, and
    corroboration is an enhancement, not a blocker.
11. **Rare-class pre-filter review**: the keyword/heuristic pre-filter used to surface candidate rare-label
    messages for rater review should itself be reviewed before use, to avoid biasing which rare instances
    raters ever see. — `COMMUNITY-HEALTH.md` open questions #4.
    **Default:** have a second person (not the filter's author) review the filter's candidate set against a
    small random sample before the enrichment stratum is finalized.
12. **PMC engagement timing**: is the PMC comfortable being approached before Phase 2a labeling begins
    (`COMMUNITY-HEALTH.md` §7.8's stronger bar), or only after a working prototype exists? —
    `COMMUNITY-HEALTH.md` open questions #5.
    **Default:** approach early, per §7.8 as written — a heads-up before labeling starts costs little and
    avoids the appearance of a surprise launch.
13. **Anthropic API cost ownership**: who pays for Batches API usage, is there a monthly cap, and what
    happens mid-run if a cap is hit? — `ROADMAP.md` §5.8.
    **Default:** the project owner funds it with a modest monthly cap; on cap-hit, classification pauses
    with a visible gap noted in the run manifest rather than a hard pipeline failure.
14. **Numeric defaults for classified-metric floors**: the 30-messages/threads and 5–10-distinct-individuals
    floors in `COMMUNITY-HEALTH.md` §5/§7 are starting points, not derived from real Cassandra volume. —
    `COMMUNITY-HEALTH.md` open questions #1.
    **Default:** ship with the stated defaults; revisit once real `dev@`/JIRA/PR classified volumes are
    observed in the Phase 2a pilot.

## Phase 2b — ASF Slack

15. **ASF Infra's approval process**: is a read-only Slack bot approved via an INFRA JIRA ticket, a direct
    request, or something else, and what's the typical timeline? — `ROADMAP.md` §5.7.
    **Default:** open an INFRA JIRA ticket as the starting point (the most common documented pattern for
    ASF Infra requests) and confirm directly with Infra before assuming the timeline.

## Cross-cutting

16. **Pre-2016 reviewer-signal alternatives**: whether an alternative signal (e.g., mailing-list patch-review
    threads) could recover any usable reviewer data for the pre-2017 period hasn't been attempted. —
    `METRICS.md` §8.
    **Default:** out of scope unless a specific research need arises; the 2017+ baseline floor already
    handles the known data-availability gap.
17. **Trailer/JIRA-field agreement rate**: how often the commit trailer and the JIRA reviewer field(s) name
    the same people for the same issue hasn't been measured, only that both are independently populated. —
    `METRICS.md` §8.
    **Default:** measure this before promoting `unique_reviewers_monthly` and its dependents from `proxy` to
    `established`, per `METRICS.md`'s own stated gate.
