"""Owner-only private report rendering for the private Cassandra
communication run (issue #110; COMMUNITY-HEALTH.md §7.3, §7.4).

**Aggregate-only, by construction**: this module reads nothing but
`runner.py`'s `aggregates` dict (counts, weighted rates, CIs, thresholds) --
it never has access to message text, thread ids, message ids, or any
per-person identifier in the first place, so there is nothing here that
could leak one into `report.md` (`tests/test_private_run_report.py` checks
this the same way `pilot/report.py`'s own tests do: assert distinctive
fixture substrings are absent from the rendered output).
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


def _fmt_sensitivity(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _by_period_table(cells_by_period: dict[str, dict[str, Any]], period_label: str) -> list[str]:
    lines = [
        "| " + period_label + " | Messages | Authors | " + " | ".join(_SORTED_LABELS) + " |",
        "|" + "---|" * (3 + len(_SORTED_LABELS)),
    ]
    for period in sorted(cells_by_period):
        cell = cells_by_period[period]
        row = [period, str(cell["messages_classified"]), str(cell["distinct_authors"])]
        for label_id in _SORTED_LABELS:
            row.append(_fmt_rate(cell["rates_per_1000_messages"][label_id]))
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
        row = [period] + [_fmt_sensitivity(sensitivity.get(label)) for label in strong_labels]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def render_report_markdown(aggregates: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Private Cassandra communication run -- owner-only report",
        "",
        "Issue #110; DECISIONS.md D1, D10, D17, D18, D22, D23. **Private, not "
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
        "",
        "## Frame definition",
        "",
    ]
    for venue, definition in aggregates["frame_definition"].items():
        lines.append(f"- **{venue}**: {definition}")
    lines.append("")

    lines += [
        "## Sensitivity thresholds",
        "",
        "Applied only to labels with a strong public mapping (D23: `personal_attack`, "
        "`hostility`, `sarcasm`; see `docs/benchmark/public-v1.md`, whichever strong+"
        "gating row there reports the highest F1). Every rate column above the "
        "sensitivity tables is threshold-free (weighted mean probability x 1000) -- no "
        "label has a calibrated production threshold yet (`questions_v1.yaml`'s "
        "`threshold: null`).",
        "",
    ]
    if aggregates["sensitivity_thresholds"]:
        lines += ["| Label | Threshold |", "|---|---|"]
        for label, threshold in sorted(aggregates["sensitivity_thresholds"].items()):
            lines.append(f"| {label} | {threshold} |")
    else:
        lines.append(
            "(none found -- `docs/benchmark/public-v1.md` had no strong+gating row at run time)"
        )
    lines.append("")

    cost = aggregates["cost_summary"]
    lines += [
        "## Cost and latency",
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

    strong_labels = sorted(aggregates["sensitivity_thresholds"])
    for venue in aggregates["venues"]:
        lines.append(f"## {venue} -- by year")
        lines.append("")
        lines += _by_period_table(aggregates["cells_by_year"].get(venue, {}), "Year")

        if strong_labels:
            lines.append(f"### {venue} -- sensitivity column, by year")
            lines.append("")
            year_cells = aggregates["cells_by_year"].get(venue, {})
            lines += _sensitivity_table(year_cells, "Year", strong_labels)

        lines.append(f"## {venue} -- by quarter")
        lines.append("")
        lines += _by_period_table(aggregates["cells_by_quarter"].get(venue, {}), "Quarter")

        if strong_labels:
            lines.append(f"### {venue} -- sensitivity column, by quarter")
            lines.append("")
            lines += _sensitivity_table(
                aggregates["cells_by_quarter"].get(venue, {}), "Quarter", strong_labels
            )

    return "\n".join(lines) + "\n"
