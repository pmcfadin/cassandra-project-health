"""Private and public report rendering for the Jev pilot evaluation (issue
#47; DECISIONS.md D18; COMMUNITY-HEALTH.md §7).

Two renderers, deliberately kept in one module so the contrast between them
is visible at a glance:

- `render_private_report_markdown` -- written into the private benchmark
  repo (D18). May reference corpus item ids (a join key back into that same
  private repo's corpus file) but never message text.
- `render_public_report_markdown` -- written into the public repo's
  `docs/pilot/`. **Aggregate-only**: no message text, no item/message ids,
  no rater names (raters are numbered `rater 1`, `rater 2`, ... in file
  order), no archive URLs (§7.3/§7.4). `tests/test_pilot_report.py` asserts
  this by construction: every corpus id and every distinctive text
  substring from a synthetic fixture corpus is checked absent from the
  rendered public markdown.
"""

from __future__ import annotations

from datetime import datetime, timezone

from project_health.pilot.evaluate import GATE_FLOORS, PilotEvaluationResult


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _fmt_ci(ci: tuple[float, float] | None) -> str:
    if ci is None:
        return "n/a"
    lo, hi = ci
    return f"[{lo:.3f}, {hi:.3f}]"


# --- Private report (per-item detail; ids ok, text never) ------------------------------


def render_private_report_markdown(
    result: PilotEvaluationResult, *, generated_at: str | None = None
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    lines: list[str] = [
        "# Jev pilot evaluation -- private detail report",
        "",
        "Issue #47; DECISIONS.md D18. **Private repo only** -- per-item detail below "
        "joins back to the corpus JSONL in this same private repo by id. Never copy "
        "this file, or any table in it, into the public repo.",
        "",
        f"- Generated at: {generated_at}",
        f"- Corpus items: {result.n_corpus_items}",
        f"- Items classified by Jev: {result.n_classified_items}",
        f"- Raters: {', '.join(result.rater_names) if result.rater_names else '(none)'}",
        "",
    ]

    if result.jev_summary:
        s = result.jev_summary
        lines += [
            "## Jev cost and latency",
            "",
            f"- Status: {s.get('status')}",
            f"- Calls made / cache hits: {s.get('calls_made')} / {s.get('cache_hits')}",
            f"- Input / output tokens: "
            f"{s.get('input_tokens_used')} / {s.get('output_tokens_used')}",
            f"- Estimated cost (USD): {s.get('estimated_cost_usd')}",
            f"- Elapsed seconds: {s.get('elapsed_seconds')}",
            f"- Mean latency per call (seconds): {s.get('mean_latency_seconds_per_call')}",
            f"- classifier_version / question_set_version / model_id: "
            f"{s.get('classifier_version')} / {s.get('question_set_version')} / "
            f"{s.get('model_id_pinned')}",
            "",
        ]

    lines += [
        "## Rater time (minutes per message)",
        "",
        "| Rater | n | Median | p90 |",
        "|---|---|---|---|",
    ]
    for rater, r in sorted(result.rater_time.per_rater.items()):
        lines.append(
            f"| {rater} | {r['n']} | {_fmt(r['median_minutes'], 2)} | {_fmt(r['p90_minutes'], 2)} |"
        )
    lines.append(
        f"| **overall** | {result.rater_time.overall['n']} | "
        f"{_fmt(result.rater_time.overall['median_minutes'], 2)} | "
        f"{_fmt(result.rater_time.overall['p90_minutes'], 2)} |"
    )
    lines.append("")

    lines += [
        "## Tone agreement (human level vs. Jev argmax)",
        "",
        f"- Paired items: {result.tone.n_paired}",
        f"- Exact agreement: {_fmt(result.tone.exact_agreement)}",
        f"- Off-by-one agreement: {_fmt(result.tone.off_by_one)}",
        f"- Weighted kappa: {_fmt(result.tone.weighted_kappa)}",
        "",
    ]

    if result.agreement:
        lines += [
            "## Krippendorff's alpha per label (2+ raters)",
            "",
            "| Label | alpha |",
            "|---|---|",
        ]
        for label_id in sorted(result.agreement):
            alpha = result.agreement[label_id]
            alpha_text = _fmt(alpha) if alpha is not None else "undefined (n<2 pairable)"
            lines.append(f"| {label_id} | {alpha_text} |")
        lines.append("")
    else:
        lines += [
            "## Inter-rater agreement",
            "",
            "Only one rater labeled this pilot -- **no agreement statistic exists** "
            "(COMMUNITY-HEALTH.md §6.2/§6.3 require 2+ raters for Krippendorff's alpha).",
            "",
        ]

    for label_id, ev in sorted(result.label_evaluations.items()):
        lines += [
            f"## `{label_id}` ({ev.label_group})",
            "",
            f"- Candidates classified: {ev.n_candidates}; scored: {ev.n_scored}; "
            f"excluded (unsure/tie): {ev.n_excluded}; positives: {ev.n_positives}",
            f"- Recommended threshold (max F1): {ev.best.threshold} -- "
            f"P={_fmt(ev.best.precision)} {_fmt_ci(ev.best.precision_ci)}, "
            f"R={_fmt(ev.best.recall)} {_fmt_ci(ev.best.recall_ci)}, "
            f"F1={_fmt(ev.best.f1)} {_fmt_ci(ev.best.f1_ci)}",
            f"- Gate verdict: **{ev.gate['status']}** -- {ev.gate['reason']}",
            f"- Prevalence stratum: {ev.prevalence['successes']}/{ev.prevalence['n']} "
            f"= {_fmt(ev.prevalence['proportion'])} {_fmt_ci(ev.prevalence['ci'])}",
            f"- Full-benchmark size estimate (this label): {ev.full_benchmark_n_estimate}",
            "",
            "Per-stratum breakdown (at recommended threshold):",
            "",
            "| Stratum | n | Precision | Recall | F1 |",
            "|---|---|---|---|---|",
        ]
        for stratum_name, breakdown in ev.stratum_breakdown.items():
            lines.append(
                f"| {stratum_name} | {breakdown['n']} | {_fmt(breakdown['precision'])} | "
                f"{_fmt(breakdown['recall'])} | {_fmt(breakdown['f1'])} |"
            )
        lines += [
            "",
            "Threshold sweep:",
            "",
            "| Threshold | TP | FP | TN | FN | Precision | Recall | F1 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for m in ev.sweep:
            lines.append(
                f"| {m.threshold} | {m.confusion.tp} | {m.confusion.fp} | {m.confusion.tn} | "
                f"{m.confusion.fn} | {_fmt(m.precision)} | {_fmt(m.recall)} | {_fmt(m.f1)} |"
            )
        lines += [
            "",
            "Reliability diagram (predicted bin vs. observed rate):",
            "",
            "| Bin | n | Mean predicted | Observed rate |",
            "|---|---|---|---|",
        ]
        for b in ev.reliability:
            lines.append(
                f"| [{b.bin_lo:.2f}, {b.bin_hi:.2f}) | {b.n} | {_fmt(b.mean_predicted)} | "
                f"{_fmt(b.observed_rate)} |"
            )
        lines += [
            "",
            "Item ids included in this label's scored set (join key into the corpus "
            "JSONL in this same private repo -- never the text itself):",
            "",
            ", ".join(ev.item_ids_scored) if ev.item_ids_scored else "(none)",
            "",
        ]

    return "\n".join(lines) + "\n"


# --- Public report (aggregate-only) -----------------------------------------------------


def _recommendation_section(result: PilotEvaluationResult) -> list[str]:
    lines = [
        "## Recommendation",
        "",
        "Deterministic, rule-based (issue #47) -- not a judgment call. Rules:",
        "",
        "- **Gate verdict** per label: `likely_pass` if precision, recall, and F1 point "
        "estimates at the recommended threshold all clear their §6.4 floor; "
        "`drop_or_revise_taxonomy` if any point estimate is more than 0.20 below its floor "
        "*and* at least 10 ground-truth positives support that estimate (a shortfall this "
        "large, on this much data, reads as a real taxonomy/classifier problem, not pilot "
        "noise); otherwise `revise_or_gather_more_data`.",
        "- **Recommended threshold** per label: the threshold in the sweep that maximizes "
        "F1 (ties broken by higher precision, then the lower threshold).",
        "- **Full-benchmark size** per label: "
        "`ceil(1.96^2 * p*(1-p) / 0.1^2)`, evaluated once at the label's observed "
        "prevalence-stratum proportion and once at its observed F1 (each clamped to "
        "[0.01, 0.99]), reporting the larger of the two -- the sample size a normal-"
        "approximation binomial 95% CI would need to reach a half-width of 0.1 at that "
        "proportion.",
        "",
        "| Label | Gate verdict | Recommended threshold | Full-benchmark size estimate |",
        "|---|---|---|---|",
    ]
    for label_id, ev in sorted(result.label_evaluations.items()):
        lines.append(
            f"| {label_id} | {ev.gate['status']} | {ev.best.threshold} | "
            f"{ev.full_benchmark_n_estimate} |"
        )
    lines.append("")
    return lines


def render_public_report_markdown(
    result: PilotEvaluationResult, *, generated_at: str | None = None
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    n_raters = len(result.rater_names)

    lines: list[str] = [
        "# Jev pilot evaluation",
        "",
        "Phase 2a pilot (DECISIONS.md D18; issue #47), evaluating TypeSafe Jev against "
        f"{n_raters} independent human rater(s) on the pilot corpus. This report is "
        "aggregate-only, per COMMUNITY-HEALTH.md §7: no message text, no item/message "
        "ids, no rater names, and no archive links appear anywhere below.",
        "",
        f"- Generated at: {generated_at}",
        f"- Corpus items: {result.n_corpus_items}",
        f"- Items Jev classified: {result.n_classified_items}",
        f"- Independent raters: {n_raters}",
        "",
    ]

    if result.jev_summary:
        s = result.jev_summary
        lines += [
            "## Jev cost and latency",
            "",
            f"- Calls made: {s.get('calls_made')} (cache hits: {s.get('cache_hits')})",
            f"- Estimated cost (USD): {s.get('estimated_cost_usd')}",
            f"- Mean latency per call (seconds): {s.get('mean_latency_seconds_per_call')}",
            "",
        ]

    lines += [
        "## Rater time (minutes per message, pooled across raters)",
        "",
        f"- Median: {_fmt(result.rater_time.overall['median_minutes'], 2)}",
        f"- p90: {_fmt(result.rater_time.overall['p90_minutes'], 2)}",
        "",
        "## Tone agreement (human level vs. Jev argmax)",
        "",
        f"- Exact agreement: {_fmt(result.tone.exact_agreement)}",
        f"- Off-by-one agreement: {_fmt(result.tone.off_by_one)}",
        f"- Weighted kappa: {_fmt(result.tone.weighted_kappa)}",
        "",
    ]

    if result.agreement:
        lines += [
            "## Inter-rater agreement (Krippendorff's alpha, §6.3)",
            "",
            "| Label | alpha | Gate (>= 0.667 usable, >= 0.80 target) |",
            "|---|---|---|",
        ]
        for label_id in sorted(result.agreement):
            alpha = result.agreement[label_id]
            if alpha is None:
                gate = "undefined"
            elif alpha >= 0.80:
                gate = "clears target"
            elif alpha >= 0.667:
                gate = "usable (tentative)"
            else:
                gate = "below usable floor"
            lines.append(f"| {label_id} | {_fmt(alpha) if alpha is not None else 'n/a'} | {gate} |")
        lines.append("")
    else:
        lines += [
            "## Inter-rater agreement",
            "",
            f"With {n_raters} rater, **no agreement statistic exists** "
            "(COMMUNITY-HEALTH.md §6.2/§6.3 require 2+ raters for Krippendorff's alpha). "
            "More raters, including at least one without a Cassandra PMC/committer "
            "affiliation, join before the full benchmark (§6.2).",
            "",
        ]

    for label_id, ev in sorted(result.label_evaluations.items()):
        floors = GATE_FLOORS[ev.label_group]
        lines += [
            f"## `{label_id}` ({ev.label_group})",
            "",
            f"- Scored items: {ev.n_scored} (excluded as unsure/tie: {ev.n_excluded}); "
            f"ground-truth positives: {ev.n_positives}",
            f"- §6.4 floors for this group: precision >= {floors['precision']}, "
            f"recall >= {floors['recall']}, F1 >= {floors['f1']}",
            f"- At the recommended threshold ({ev.best.threshold}): "
            f"precision {_fmt(ev.best.precision)} {_fmt_ci(ev.best.precision_ci)}, "
            f"recall {_fmt(ev.best.recall)} {_fmt_ci(ev.best.recall_ci)}, "
            f"F1 {_fmt(ev.best.f1)} {_fmt_ci(ev.best.f1_ci)}",
            f"- Gate verdict: **{ev.gate['status']}**",
            f"- Prevalence (prevalence stratum only): {_fmt(ev.prevalence['proportion'])} "
            f"{_fmt_ci(ev.prevalence['ci'])} (n={ev.prevalence['n']})",
            "",
            "Per-stratum breakdown:",
            "",
            "| Stratum | n | Precision | Recall | F1 |",
            "|---|---|---|---|---|",
        ]
        for stratum_name, breakdown in ev.stratum_breakdown.items():
            lines.append(
                f"| {stratum_name} | {breakdown['n']} | {_fmt(breakdown['precision'])} | "
                f"{_fmt(breakdown['recall'])} | {_fmt(breakdown['f1'])} |"
            )
        lines += [
            "",
            "Reliability diagram (predicted bin vs. observed rate):",
            "",
            "| Bin | n | Mean predicted | Observed rate |",
            "|---|---|---|---|",
        ]
        for b in ev.reliability:
            lines.append(
                f"| [{b.bin_lo:.2f}, {b.bin_hi:.2f}) | {b.n} | {_fmt(b.mean_predicted)} | "
                f"{_fmt(b.observed_rate)} |"
            )
        lines.append("")

    lines += _recommendation_section(result)
    return "\n".join(lines) + "\n"
