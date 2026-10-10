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


def _fmt_thread_rate(entry: dict[str, Any] | None) -> str:
    """Issue #114: format one §5.2 thread-level rate entry (`escalation_rate`,
    `constructive_resolution_rate`, `thread_abandonment_rate_post_friction`)."""
    if not entry or entry.get("insufficient_data") or entry.get("rate") is None:
        return "insufficient data"
    rate = entry["rate"]
    ci = entry.get("ci95")
    if ci:
        return f"{rate:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]"
    return f"{rate:.3f}"


def _fmt_pile_on_rate(entry: dict[str, Any] | None) -> str:
    if not entry or entry.get("insufficient_data") or entry.get("per_100_threads") is None:
        return "insufficient data"
    value = entry["per_100_threads"]
    ci = entry.get("ci95")
    if ci:
        return f"{value:.2f} [{ci[0]:.2f}, {ci[1]:.2f}]"
    return f"{value:.2f}"


def _thread_metrics_table(
    thread_metrics_by_period: dict[str, dict[str, Any]], period_label: str, cutoff_key: str
) -> list[str]:
    lines = [
        "| "
        + period_label
        + " | Threads | Escalation rate | Constructive resolution rate | "
        "Thread abandonment rate (post-friction) | Pile-on rate (per 100 threads) |",
        "|---|---|---|---|---|---|",
    ]
    for period in sorted(thread_metrics_by_period):
        cell = thread_metrics_by_period[period].get(cutoff_key, {})
        row = [
            period,
            str(cell.get("threads_total", 0)),
            _fmt_thread_rate(cell.get("escalation_rate")),
            _fmt_thread_rate(cell.get("constructive_resolution_rate")),
            _fmt_thread_rate(cell.get("thread_abandonment_rate_post_friction")),
            _fmt_pile_on_rate(cell.get("pile_on_rate")),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def _fmt_newcomer_row(label: str, cell: dict[str, Any] | None) -> str:
    cell = cell or {}
    messages = cell.get("messages_directed_at_newcomers", 0)
    distinct = cell.get("distinct_newcomers", 0)
    if cell.get("insufficient_data", True):
        constructive = "insufficient data"
        hostile = "insufficient data"
    else:
        c_rate = cell.get("constructive_response_rate")
        c_ci = cell.get("constructive_response_rate_ci95")
        h_rate = cell.get("dismissive_hostile_response_rate")
        h_ci = cell.get("dismissive_hostile_response_rate_ci95")
        constructive = (
            f"{c_rate:.3f} [{c_ci[0]:.3f}, {c_ci[1]:.3f}]" if c_ci else f"{c_rate:.3f}"
        )
        hostile = f"{h_rate:.3f} [{h_ci[0]:.3f}, {h_ci[1]:.3f}]" if h_ci else f"{h_rate:.3f}"
    return f"| {label} | {messages} | {distinct} | {constructive} | {hostile} |"


def _newcomer_table(newcomer_by_period: dict[str, dict[str, Any]], period_label: str) -> list[str]:
    lines = [
        "| "
        + period_label
        + " | Newcomer-directed messages | Distinct newcomers | Constructive response rate | "
        "Dismissive/hostile response rate |",
        "|---|---|---|---|---|",
    ]
    for period in sorted(newcomer_by_period):
        lines.append(_fmt_newcomer_row(period, newcomer_by_period[period]))
    lines.append("")
    return lines


def _thread_trend_summary_lines(aggregates: dict[str, Any]) -> list[str]:
    trend = aggregates.get("thread_trend_summary") or {}
    headline_key = aggregates.get("thread_derive_headline_cutoff", "0.5")
    lines = [
        "## Thread-level trend summary (§2.3)",
        "",
        "The derived thread-level events -- escalation, constructive resolution, "
        "abandonment-after-friction, pile-on -- pooled across the same early/recent year "
        "windows as the message-level trend summary above, at the headline (0.5) "
        "probability cutoff (see 'Thread-level metrics, by year' below for the full "
        "per-year breakdown and the 0.7 sensitivity cutoff). Plain rates with "
        "thread-level bootstrap 95% CIs; no verdicts (D25).",
        "",
    ]
    for venue in aggregates.get("venues", []):
        venue_trend = trend.get(venue)
        if not venue_trend:
            continue
        early = venue_trend["windows"]["2017_2019"].get(headline_key, {})
        recent = venue_trend["windows"]["2023_2025"].get(headline_key, {})
        lines += [
            f"### {venue}",
            "",
            "| Metric | 2017-2019 | 2023-2025 |",
            "|---|---|---|",
            f"| Escalation rate | {_fmt_thread_rate(early.get('escalation_rate'))} | "
            f"{_fmt_thread_rate(recent.get('escalation_rate'))} |",
            "| Constructive resolution rate | "
            f"{_fmt_thread_rate(early.get('constructive_resolution_rate'))} | "
            f"{_fmt_thread_rate(recent.get('constructive_resolution_rate'))} |",
            "| Thread abandonment rate (post-friction) | "
            f"{_fmt_thread_rate(early.get('thread_abandonment_rate_post_friction'))} | "
            f"{_fmt_thread_rate(recent.get('thread_abandonment_rate_post_friction'))} |",
            f"| Pile-on rate (per 100 threads) | {_fmt_pile_on_rate(early.get('pile_on_rate'))} | "
            f"{_fmt_pile_on_rate(recent.get('pile_on_rate'))} |",
            "",
        ]
    return lines


def _newcomer_trend_summary_lines(aggregates: dict[str, Any]) -> list[str]:
    trend = aggregates.get("newcomer_trend_summary") or {}
    lines = [
        "## Newcomer treatment trend summary (§5.2)",
        "",
        "Constructive and dismissive/hostile response rates for messages directed at a "
        "newcomer (fewer than "
        f"{aggregates.get('newcomer_n')} prior messages in this venue, across the whole "
        "Phase-1 metadata, at message time -- COMMUNITY-HEALTH.md §2.3 rule 8), pooled "
        "across the same early/recent year windows. Reported as two separate rates, "
        "never netted into one (§5.1's no-composite rule).",
        "",
    ]
    for venue in aggregates.get("venues", []):
        venue_trend = trend.get(venue)
        if not venue_trend:
            continue
        lines += [
            f"### {venue}",
            "",
            "| Window | Newcomer-directed messages | Distinct newcomers | "
            "Constructive response rate | Dismissive/hostile response rate |",
            "|---|---|---|---|---|",
            _fmt_newcomer_row("2017-2019", venue_trend["windows"].get("2017_2019")),
            _fmt_newcomer_row("2023-2025", venue_trend["windows"].get("2023_2025")),
            "",
        ]
    return lines


_TONE_TIER_ORDER: tuple[str, ...] = ("-2", "-1", "0", "1", "2", "3", "4")


def _fmt_tier_share(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "insufficient data"
    lo, hi = entry["ci95"]
    return f"{entry['share'] * 100:.1f}% [{lo * 100:.1f}%, {hi * 100:.1f}%]"


def _tone_mix_table(
    tone_mix_by_period: dict[str, dict[str, Any]], period_label: str, cutoff_key: str
) -> list[str]:
    """Issue #153: §2.2 intensity-tier mix table -- one row per period,
    one column per tier, bottom-to-top (-2 closing/positive .. 4 attack)."""
    lines = [
        "| "
        + period_label
        + " | Messages | Authors | "
        + " | ".join(_TONE_TIER_ORDER)
        + " |",
        "|" + "---|" * (3 + len(_TONE_TIER_ORDER)),
    ]
    for period in sorted(tone_mix_by_period):
        cell = tone_mix_by_period[period].get(cutoff_key, {})
        row = [
            period,
            str(cell.get("messages_classified", 0)),
            str(cell.get("distinct_authors", 0)),
        ]
        tiers = cell.get("tiers", {})
        for tier in _TONE_TIER_ORDER:
            row.append(_fmt_tier_share(tiers.get(tier)))
        lines.append("| " + " | ".join(row) + " |")
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
    message was classified and nothing needed truncating or skipping.

    Issue #115 also renders this section (with an accurate, non-alarming
    intro line, not the "did not classify every sampled message" claim)
    whenever a run truncated or skipped any message but still classified
    every sampled one -- the coverage section is where those per-message
    counts belong (`runner.py`'s own `partial_run` trigger condition).
    """
    partial = aggregates.get("partial_run")
    if not partial:
        return []
    sampled = partial["messages_sampled"]
    classified = partial["messages_classified"]
    truncated = partial.get("messages_truncated", 0)
    skipped = partial.get("messages_skipped", 0)

    lines = ["## PARTIAL RUN", ""]
    if classified < sampled:
        lines += [
            f"**This run did not classify every sampled message.** Status: "
            f"`{partial['status']}`. **{classified} of "
            f"{sampled} sampled messages were classified**; every "
            "number below reflects only what was classified so far. Already-classified "
            "messages are cached and are never re-sent -- re-run (with TypeSafe credits "
            "added, a higher --monthly-cap-usd, or without --no-classify) to continue.",
            "",
        ]
    else:
        lines += [
            f"Status: `{partial['status']}`. **{classified} of {sampled} sampled "
            "messages were classified** -- every sampled message got a record, but "
            "at least one needed the length-budget/skip handling below (issue #115).",
            "",
        ]
    lines += [
        f"- Truncated to fit Jev's per-request length budget: {truncated}",
        f"- Skipped (never classified -- a non-retryable 400, or `max_tokens_exceeded` "
        f"again after a halved-budget retry): {skipped}",
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
        "Issue #110, #114; DECISIONS.md D1, D10, D17, D18, D22, D23, D25. **Private, not "
        "published**: nothing in this file is published to the site, the public "
        "repo, or the `data` branch (COMMUNITY-HEALTH.md §7.8 -- PMC preview and "
        "explicit acknowledgment come first). No message text, no names, and no "
        "message ids appear anywhere below (COMMUNITY-HEALTH.md §7.3/§7.4).",
        "",
        "**Thread-level derivation (§2.2/§2.3) ambiguity resolutions**: COMMUNITY-"
        "HEALTH.md §2.3 is written in prose, not pseudocode; issue #114 asks that any "
        "ambiguity be resolved with the simplest reading and documented. Every reading "
        "chosen (directed_at = parent author only, no @-mention extraction; parent "
        "resolution scoped to this run's own thread survivors; escalation's "
        "'two different author_refs' = two distinct authors each striking a new "
        "running-maximum tier; de-escalation keyed off the thread's *last* tier >= 3 "
        "message; resolution's tail-window evidence gated to the last k messages; "
        "abandonment's target = the temporally last tier >= 2 message; only "
        "thread-scoped abandonment is computed, not the project-scoped nightly-job "
        "upgrade) is documented verbatim in `private_run/thread_derive.py`'s module "
        "docstring.",
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
    lines += _thread_trend_summary_lines(aggregates)
    lines += _newcomer_trend_summary_lines(aggregates)

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

        thread_metrics_years = aggregates.get("thread_metrics_by_year", {}).get(venue, {})
        thread_headline_key = aggregates.get("thread_derive_headline_cutoff", "0.5")
        thread_derive_cutoffs = aggregates.get("thread_derive_cutoffs", [])
        thread_sensitivity_keys = [c for c in thread_derive_cutoffs if c != thread_headline_key]

        lines.append(
            f"### {venue} -- thread-level metrics (§2.3, >= {thread_headline_key}), by year"
        )
        lines.append("")
        lines += _thread_metrics_table(thread_metrics_years, "Year", thread_headline_key)

        for cutoff_key in thread_sensitivity_keys:
            lines.append(
                f"### {venue} -- thread-level metrics sensitivity cutoff (>= {cutoff_key}), by year"
            )
            lines.append("")
            lines += _thread_metrics_table(thread_metrics_years, "Year", cutoff_key)

        newcomer_years = aggregates.get("newcomer_by_year", {}).get(venue, {})
        lines.append(f"### {venue} -- newcomer treatment (§5.2), by year")
        lines.append("")
        lines += _newcomer_table(newcomer_years, "Year")

        # Issue #153: §2.2 intensity-tier mix ("tone over time"), by year
        # and by quarter, at the headline (0.5) and sensitivity (0.7)
        # cutoffs.
        tone_mix_years = aggregates.get("tone_mix_by_year", {}).get(venue, {})
        tone_mix_quarters = aggregates.get("tone_mix_by_quarter", {}).get(venue, {})
        tone_mix_headline = aggregates.get("tone_mix_headline_cutoff", "0.5")
        tone_mix_cutoffs = aggregates.get("tone_mix_cutoffs", [])
        tone_mix_sensitivity = [c for c in tone_mix_cutoffs if c != tone_mix_headline]

        lines.append(f"### {venue} -- tone mix (§2.2, >= {tone_mix_headline}), by year")
        lines.append("")
        lines += _tone_mix_table(tone_mix_years, "Year", tone_mix_headline)
        for cutoff_key in tone_mix_sensitivity:
            lines.append(f"### {venue} -- tone mix sensitivity cutoff (>= {cutoff_key}), by year")
            lines.append("")
            lines += _tone_mix_table(tone_mix_years, "Year", cutoff_key)

        lines.append(f"## {venue} -- headline (>= {headline_cutoff}), by quarter")
        lines.append("")
        lines += _cutoff_table(quarter_cells, "Quarter", headline_cutoff)

        lines.append(f"### {venue} -- tone mix (§2.2, >= {tone_mix_headline}), by quarter")
        lines.append("")
        lines += _tone_mix_table(tone_mix_quarters, "Quarter", tone_mix_headline)

    return "\n".join(lines) + "\n"
