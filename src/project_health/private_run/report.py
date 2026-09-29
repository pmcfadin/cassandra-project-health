"""Owner-only private report rendering for the private Cassandra
communication run (issue #110; COMMUNITY-HEALTH.md §7.3, §7.4, D25).

**Aggregate-only, by construction**: this module reads nothing but
`runner.py`'s `aggregates` dict (counts, weighted rates, CIs, thresholds) --
it never has access to message text, thread ids, message ids, or any
per-person identifier in the first place, so there is nothing here that
could leak one into `report.md` (`tests/test_private_run_report.py` checks
this the same way `pilot/report.py`'s own tests do: assert distinctive
fixture substrings are absent from the rendered output).

Issue #110 fixup round 1 changed what's *headline* here: "weighted mean
probability x 1000" alone is not a rate (every message carries a small
residual probability for every label, giving the number a floor even where
a label essentially never applies). The headline numbers below are now
**counts at fixed, uncalibrated probability cutoffs** (0.5 in the main
tables, 0.7/0.9 in a sensitivity table); the old weighted-mean number is
kept only as a secondary "probability index (uncalibrated; use for trend
only)", paired with a run-wide floor (the median message probability) so a
reader can see how much of the index is just baseline. A "Trend summary"
table at the top reports plain rates and whether confidence intervals
overlap between two pooled year windows -- neutral wording, no verdicts
(D25: this project's site/reports are informational, not a judgment).
"""

from __future__ import annotations

from typing import Any

from project_health.classify.questions import MESSAGE_LEVEL_LABELS

_SORTED_LABELS = sorted(MESSAGE_LEVEL_LABELS)


def _fmt_rate(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "insufficient data"
    lo, hi = entry["ci95"]
    return f"{entry['per_1000']:.2f} [{lo:.2f}, {hi:.2f}]"


def _fmt_sensitivity_value(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "n/a"
    return f"{entry['per_1000']:.2f}"


def _fmt_overlap(value: bool | None) -> str:
    if value is None:
        return "insufficient data"
    return "overlapping" if value else "not overlapping"


def _cutoff_table(
    cells_by_period: dict[str, dict[str, Any]], period_label: str, cutoff_key: str
) -> list[str]:
    lines = [
        "| " + period_label + " | Messages | Authors | " + " | ".join(_SORTED_LABELS) + " |",
        "|" + "---|" * (3 + len(_SORTED_LABELS)),
    ]
    for period in sorted(cells_by_period):
        cell = cells_by_period[period]
        row = [period, str(cell["messages_classified"]), str(cell["distinct_authors"])]
        for label_id in _SORTED_LABELS:
            entry = cell["cutoff_rates_per_1000_messages"][label_id].get(cutoff_key)
            row.append(_fmt_rate(entry))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def _probability_index_table(
    cells_by_period: dict[str, dict[str, Any]], period_label: str
) -> list[str]:
    lines = [
        "| " + period_label + " | " + " | ".join(_SORTED_LABELS) + " |",
        "|" + "---|" * (1 + len(_SORTED_LABELS)),
    ]
    for period in sorted(cells_by_period):
        cell = cells_by_period[period]
        row = [period]
        for label_id in _SORTED_LABELS:
            row.append(_fmt_rate(cell["probability_index_per_1000_messages"][label_id]))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def _sensitivity_table(
    cells_by_period: dict[str, dict[str, Any]], period_label: str, strong_labels: list[str]
) -> list[str]:
    lines = [
        "| " + period_label + " | " + " | ".join(strong_labels) + " |",
        "|" + "---|" * (1 + len(strong_labels)),
    ]
    for period in sorted(cells_by_period):
        cell = cells_by_period[period]
        sensitivity = cell["sensitivity_per_1000_messages"]
        row = [period] + [_fmt_sensitivity_value(sensitivity.get(label)) for label in strong_labels]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def _trend_summary_lines(aggregates: dict[str, Any]) -> list[str]:
    trend = aggregates.get("trend_summary") or {}
    lines = [
        "## Trend summary",
        "",
        "Plain rates at the headline (0.5) probability cutoff, pooled and weighted "
        "across each window's sampled quarters, with thread-level bootstrap 95% CIs. "
        "This table reports whether the two windows' confidence intervals overlap -- "
        "it does not characterize any difference as an improvement or a decline "
        "(D25: this report is informational, not a judgment; the same plain-rates, "
        "no-verdicts stance the site takes).",
        "",
    ]
    for venue in aggregates.get("venues", []):
        venue_trend = trend.get(venue)
        if not venue_trend:
            continue
        headline_key = venue_trend["headline_cutoff"]
        early = venue_trend["windows"]["2017_2019"]
        recent = venue_trend["windows"]["2023_2025"]
        overlap = venue_trend["ci_overlap_by_label"]
        lines += [
            f"### {venue}",
            "",
            f"- 2017-2019: {early['messages_classified']} messages, "
            f"{early['distinct_authors']} authors"
            + (" (insufficient data)" if early["insufficient_data"] else ""),
            f"- 2023-2025: {recent['messages_classified']} messages, "
            f"{recent['distinct_authors']} authors"
            + (" (insufficient data)" if recent["insufficient_data"] else ""),
            "",
            "| Label | 2017-2019 | 2023-2025 | CIs overlap |",
            "|---|---|---|---|",
        ]
        for label_id in _SORTED_LABELS:
            early_entry = early["cutoff_rates_per_1000_messages"][label_id].get(headline_key)
            recent_entry = recent["cutoff_rates_per_1000_messages"][label_id].get(headline_key)
            lines.append(
                f"| {label_id} | {_fmt_rate(early_entry)} | {_fmt_rate(recent_entry)} | "
                f"{_fmt_overlap(overlap.get(label_id))} |"
            )
        lines.append("")
    return lines


def _fmt_coverage(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def _partial_run_lines(aggregates: dict[str, Any]) -> list[str]:
    """Issue #110 fixup round 2: a prominent, top-of-report notice whenever
    fewer messages were classified than were sampled -- whether because
    this run's own classify step paused partway (D10's cost cap, or
    TypeSafe returning HTTP 402/no credits), `--no-classify` was pointed at
    a not-yet-complete cache, or the cache was already incomplete for some
    other reason. Returns `[]` (no section at all) when every sampled
    message was classified.
    """
    partial = aggregates.get("partial_run")
    if not partial:
        return []
    lines = [
        "## PARTIAL RUN",
        "",
        f"**This run did not classify every sampled message.** Status: "
        f"`{partial['status']}`. **{partial['messages_classified']} of "
        f"{partial['messages_sampled']} sampled messages were classified**; every "
        "number below reflects only what was classified so far. Already-classified "
        "messages are cached and are never re-sent -- re-run (with TypeSafe credits "
        "added, a higher --monthly-cap-usd, or without --no-classify) to continue.",
        "",
        "### Strata coverage",
        "",
        "| Venue | Quarter | Sampled | Classified | Coverage |",
        "|---|---|---|---|---|",
    ]
    for row in partial["coverage_by_stratum"]:
        lines.append(
            f"| {row['venue']} | {row['quarter']} | {row['messages_sampled']} | "
            f"{row['messages_classified']} | {_fmt_coverage(row['coverage'])} |"
        )
    lines.append("")
    return lines


def render_report_markdown(aggregates: dict[str, Any]) -> str:
    cutoffs: list[str] = aggregates.get("cutoffs", [])
    headline_cutoff: str = aggregates.get("headline_cutoff", "")
    sensitivity_cutoffs = [c for c in cutoffs if c != headline_cutoff]

    lines: list[str] = [
        "# Private Cassandra communication run -- owner-only report",
        "",
        "Issue #110; DECISIONS.md D1, D10, D17, D18, D22, D23, D25. **Private, not "
        "published**: nothing in this file is published to the site, the public "
        "repo, or the `data` branch (COMMUNITY-HEALTH.md §7.8 -- PMC preview and "
        "explicit acknowledgment come first). No message text, no names, and no "
        "message ids appear anywhere below (COMMUNITY-HEALTH.md §7.3/§7.4).",
        "",
        f"- Generated at: {aggregates['generated_at']}",
        f"- Sampler seed: {aggregates['seed']}  |  K (threads/stratum): {aggregates['k']}",
        f"- Quarters covered: {aggregates['quarters'][0]} .. {aggregates['quarters'][-1]} "
        f"({len(aggregates['quarters'])} quarter(s))",
        f"- Floors: >= {aggregates['floors']['min_messages']} messages and "
        f">= {aggregates['floors']['min_distinct_authors']} distinct authors per cell, "
        "else `insufficient data` (COMMUNITY-HEALTH.md §5.1).",
        f"- {aggregates['scope_note']}",
        f"- Headline numbers below are **counts per 1,000 messages at probability >= "
        f"{headline_cutoff}** -- a fixed, uncalibrated cutoff (no label has a "
        "Cassandra-calibrated production threshold yet, issue #47), not a rate in "
        "the sense of a validated label. Counts at "
        f"{', '.join(f'>= {c}' for c in sensitivity_cutoffs)} follow in a sensitivity "
        "table per venue.",
        "",
    ]

    lines += _partial_run_lines(aggregates)
    lines += _trend_summary_lines(aggregates)

    lines += ["## Frame definition", ""]
    for venue, definition in aggregates["frame_definition"].items():
        lines.append(f"- **{venue}**: {definition}")
    lines.append("")

    lines += [
        "## Public-benchmark sensitivity thresholds",
        "",
        "Applied only to labels with a strong public mapping (D23: `personal_attack`, "
        "`hostility`, `sarcasm`), each calibrated against a specific public dataset's "
        "best-F1 threshold (see `docs/benchmark/public-v1.md`, whichever strong+gating "
        "row there reports the highest F1). A threshold below 0.3 is flagged "
        '**permissive** -- it counts more of the classifier\'s raw output as "present" '
        "than the fixed 0.5 headline cutoff would.",
        "",
    ]
    sensitivity_thresholds = aggregates.get("sensitivity_thresholds") or {}
    if sensitivity_thresholds:
        lines += ["| Label | Threshold | Dataset | Permissive |", "|---|---|---|---|"]
        for label, info in sorted(sensitivity_thresholds.items()):
            permissive = "yes" if info.get("permissive") else "no"
            lines.append(
                f"| {label} | {info['threshold']} | {info['dataset_name']} | {permissive} |"
            )
    else:
        lines.append(
            "(none found -- `docs/benchmark/public-v1.md` had no strong+gating row at run time)"
        )
    lines.append("")

    lines += [
        "## Probability index floor (run-wide, uncalibrated)",
        "",
        "The **probability index** (shown per venue/period below, as a secondary, "
        "trend-only number) is the weighted mean of every message's raw probability "
        "for a label, x1000. It is not a rate: every message carries a small residual "
        "probability for every label, so the index has a floor even where a label "
        "essentially never applies. This table is that floor -- the **unweighted "
        "median** message probability for each label, x1000, computed once across "
        "every classified message in this run (not per cell) -- so a reader can see "
        "how much of any index reading is just baseline.",
        "",
        "| Label | Floor (median, per 1000) |",
        "|---|---|",
    ]
    floor = aggregates.get("probability_index_floor_per_1000_messages") or {}
    for label_id in _SORTED_LABELS:
        value = floor.get(label_id)
        lines.append(
            f"| {label_id} | {value:.2f} |" if value is not None else f"| {label_id} | n/a |"
        )
    lines.append("")

    cost = aggregates["cost_summary"]
    ledger = aggregates.get("cost_ledger") or {}
    cumulative = ledger.get("cumulative_from_cache") or {}
    ledger_runs = ledger.get("cumulative_from_ledger_runs") or {}
    lines += [
        "## Cost and latency",
        "",
        "### Latest run",
        "",
        f"- Status: {cost.get('status')}",
        f"- Calls made / cache hits: {cost.get('calls_made')} / {cost.get('cache_hits')}",
        f"- Input / output tokens: {cost.get('input_tokens_used')} / "
        f"{cost.get('output_tokens_used')}",
        f"- Estimated cost (USD): {cost.get('estimated_cost_usd')}",
        f"- Elapsed seconds: {cost.get('elapsed_seconds')}",
        f"- Mean latency per call (seconds): {cost.get('mean_latency_seconds_per_call')}",
        f"- classifier_version / question_set_version / model_id: "
        f"{cost.get('classifier_version')} / {cost.get('question_set_version')} / "
        f"{cost.get('model_id_pinned')}",
        "",
        "### Cumulative (lifetime, this --out directory)",
        "",
        "Two independent cumulative figures, since each has a different blind spot:",
        "",
        "- **From the persistent cache** -- summed from every record in the Jev "
        "classification cache (the durable record of every message ever billed here), "
        "so a re-run started before this feature existed still reports true lifetime "
        "cost. Can slightly *undercount*: a known race in the classifier's concurrent "
        "in-run dedup means two workers can both send (and both get billed for) the "
        "same not-yet-cached input before either finishes, but only one record "
        "survives to be persisted.",
        "- **From the cost ledger** -- summed from every run recorded below, each "
        "entry's cost taken directly from that run's own billed total, so it is exact "
        "for every run the ledger has seen. Its blind spot is the mirror image: it is "
        "0 for any spend that happened before the ledger file existed.",
        "",
        f"- Distinct messages ever classified (from cache): "
        f"{cumulative.get('distinct_messages_ever_classified')}",
        f"- Cumulative input / output tokens (from cache): {cumulative.get('input_tokens_used')} / "
        f"{cumulative.get('output_tokens_used')}",
        f"- Cumulative estimated cost (USD, from cache): {cumulative.get('estimated_cost_usd')}",
        f"- Cumulative estimated cost (USD, from ledger runs): "
        f"{ledger_runs.get('estimated_cost_usd')}",
        f"- Runs recorded in the cost ledger: {ledger.get('runs_recorded')}",
        f"- Cumulative elapsed across ledger runs (seconds): {ledger_runs.get('elapsed_seconds')}",
        "",
        "## Venue totals",
        "",
        "| Venue | Messages classified | Threads sampled | Threads population |",
        "|---|---|---|---|",
    ]
    for venue, totals in aggregates["venue_totals"].items():
        lines.append(
            f"| {venue} | {totals['messages_classified']} | {totals['threads_sampled']} | "
            f"{totals['threads_population']} |"
        )
    lines.append("")

    strong_labels = sorted(sensitivity_thresholds)
    for venue in aggregates["venues"]:
        year_cells = aggregates["cells_by_year"].get(venue, {})
        quarter_cells = aggregates["cells_by_quarter"].get(venue, {})

        lines.append(f"## {venue} -- headline (>= {headline_cutoff}), by year")
        lines.append("")
        lines += _cutoff_table(year_cells, "Year", headline_cutoff)

        for cutoff in sensitivity_cutoffs:
            lines.append(f"### {venue} -- sensitivity cutoff (>= {cutoff}), by year")
            lines.append("")
            lines += _cutoff_table(year_cells, "Year", cutoff)

        lines.append(f"### {venue} -- probability index (uncalibrated; trend only), by year")
        lines.append("")
        lines += _probability_index_table(year_cells, "Year")

        if strong_labels:
            lines.append(f"### {venue} -- public-benchmark sensitivity column, by year")
            lines.append("")
            lines += _sensitivity_table(year_cells, "Year", strong_labels)

        lines.append(f"## {venue} -- headline (>= {headline_cutoff}), by quarter")
        lines.append("")
        lines += _cutoff_table(quarter_cells, "Quarter", headline_cutoff)

    return "\n".join(lines) + "\n"
