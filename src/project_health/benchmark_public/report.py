"""Public report rendering for `benchmark-public` (issue #89; DECISIONS.md
D23; COMMUNITY-HEALTH.md §7).

**Aggregate-only, by construction**: this module never has access to item
text or item/message ids in the first place -- `DatasetLabelEvaluation`
carries only counts and `stats` objects, never a `DatasetItem` or
`ClassificationRecord`. `tests/test_benchmark_public_report.py` still asserts
this by construction (a synthetic fixture's distinctive item ids/text are
checked absent from the rendered markdown) as a leak-test backstop, mirroring
`pilot/report.py`'s existing convention for the same reason: a backstop
should never be the *only* thing preventing a leak, but it is a cheap check
that a refactor introducing one gets caught immediately.
"""

from __future__ import annotations

from datetime import datetime, timezone

from project_health.benchmark_public.evaluate import CategorySeparation, DatasetLabelEvaluation
from project_health.benchmark_public.registry import DatasetSpec, Registry


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _fmt_ci(ci: tuple[float, float] | None) -> str:
    if ci is None:
        return "n/a"
    lo, hi = ci
    return f"[{lo:.3f}, {hi:.3f}]"


def _fmt_rate(numerator: int | None, denominator: int) -> str:
    if numerator is None or denominator == 0:
        return "n/a"
    return f"{numerator}/{denominator} = {_fmt(numerator / denominator)}"


def render_public_report_markdown(
    registry: Registry,
    evaluations: dict[str, list[DatasetLabelEvaluation]],
    run_manifest: dict,
    *,
    category_separations: dict[str, list[CategorySeparation]] | None = None,
    generated_at: str | None = None,
) -> str:
    category_separations = category_separations or {}
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()

    lines: list[str] = [
        "# Public benchmark: TypeSafe Jev vs. public human-labeled datasets",
        "",
        "DECISIONS.md D22, D23; COMMUNITY-HEALTH.md §1, §6; issue #89. Primary "
        "classifier benchmark: `project-health benchmark-public` scores the pinned "
        "TypeSafe Jev classifier (D17) against public, human-labeled datasets whose own "
        "label schemes are mapped onto this project's 12 message-level labels "
        "(`label_mapping_v1.yaml`, `docs/plans/2026-09-27-public-benchmark-datasets.md`).",
        "",
        "**Aggregate only.** No item text, no item/message ids, and no per-item detail "
        "appear anywhere below -- per COMMUNITY-HEALTH.md §7's \"quotations: default "
        "none\" and D23's \"only aggregate results are published.\" Dataset text is "
        "fetched into a local cache directory at run time and is never committed to this "
        "repo or redistributed.",
        "",
        f"- Generated at: {generated_at}",
        "",
    ]

    classifier_summary = run_manifest.get("classifier", {})
    if classifier_summary:
        lines += [
            "## Cost and latency",
            "",
            f"- Calls made: {classifier_summary.get('calls_made')} "
            f"(cache hits: {classifier_summary.get('cache_hits')})",
            f"- Input / output tokens: "
            f"{classifier_summary.get('input_tokens_used')} / "
            f"{classifier_summary.get('output_tokens_used')}",
            f"- Estimated cost (USD): {classifier_summary.get('estimated_cost_usd')}",
            f"- Elapsed seconds: {classifier_summary.get('elapsed_seconds')}",
            f"- Mean latency per call (seconds): "
            f"{classifier_summary.get('mean_latency_seconds_per_call')}",
            f"- Run status: {classifier_summary.get('status')}",
            f"- classifier_version / question_set_version / model_id: "
            f"{classifier_summary.get('classifier_version')} / "
            f"{classifier_summary.get('question_set_version')} / "
            f"{classifier_summary.get('model_id_pinned')}",
            "",
        ]

    lines += [
        "## Thresholds",
        "",
        "`questions_v1.yaml` (v1) pins every message-level label's `threshold: null` -- "
        "no label has a calibrated production threshold yet (issue #47 calibrates one "
        "against the frozen owner/rater benchmark, D23's \"gaps\" stratum). Every table "
        "below therefore reports the **best-F1 threshold found in this benchmark** "
        "(ties broken by higher precision, then the lower threshold, matching "
        "`pilot/stats.best_threshold_by_f1`) rather than a value at a pinned gate "
        "threshold, which does not exist yet.",
        "",
    ]

    blocked = run_manifest.get("blocked_datasets") or []
    if blocked:
        lines += [
            "## Datasets not run",
            "",
            "Per issue #89: a dataset that fails to download, needs a form/login, or "
            "whose format differs from what was expected is reported here, never "
            "given a fabricated loader.",
            "",
            "| Dataset | Reason |",
            "|---|---|",
        ]
        for entry in blocked:
            lines.append(f"| {entry['name']} | {entry['reason']} |")
        lines.append("")

    lines += [
        "## Datasets run",
        "",
        "`n_sampled` is how many items this run drew from the dataset; `n_distinct_inputs` "
        "is how many of those are byte-distinct after preprocessing (two different items "
        "can read identically, e.g. two \"LGTM\" comments, and hash to the same classifier "
        "input); `n_evaluated` is how many of the `n_sampled` items actually resolved to a "
        "classification record. `n_evaluated` should equal `n_sampled` whenever the run "
        "completed without hitting the cost cap -- every sampled item, including ones "
        "sharing an input with another, is joined back to its record by that shared "
        "input's hash (orchestrator review of issue #89 caught and fixed a bug where "
        "duplicate-input items were silently excluded here).",
        "",
        "| Dataset | Citation | License | n_sampled | n_distinct_inputs | n_evaluated | "
        "Population n |",
        "|---|---|---|---|---|---|---|",
    ]
    dataset_summaries = run_manifest.get("datasets", {})
    # Union, not just `evaluations` -- a dataset can (in principle) have a
    # category-separation table with no scorable label evaluation at all;
    # every dataset either section covers gets one row/section, never silently
    # dropped from one but not the other.
    dataset_ids = sorted(set(evaluations) | set(category_separations))
    for dataset_id in dataset_ids:
        spec = registry.datasets[dataset_id]
        summary = dataset_summaries.get(dataset_id, {})
        lines.append(
            f"| {spec.name} | {spec.citation} | {spec.license} | "
            f"{summary.get('n_sampled')} | {summary.get('n_distinct_inputs')} | "
            f"{summary.get('n_evaluated')} | {summary.get('population_n')} |"
        )
    lines.append("")

    for dataset_id in dataset_ids:
        spec = registry.datasets[dataset_id]
        lines += _dataset_section(
            spec,
            evaluations.get(dataset_id, []),
            dataset_summaries.get(dataset_id, {}),
            category_separations.get(dataset_id),
        )

    return "\n".join(lines) + "\n"


def _dataset_section(
    spec: DatasetSpec,
    evals: list[DatasetLabelEvaluation],
    dataset_summary: dict,
    separations: list[CategorySeparation] | None,
) -> list[str]:
    lines = [
        f"## {spec.name}",
        "",
        f"- Citation: {spec.citation}",
        f"- License: {spec.license}",
        f"- Classifier source venue used: `{spec.source_venue}` -- {spec.source_venue_rationale}",
    ]
    if spec.reported_iaa:
        lines.append(f"- Reported inter-rater agreement: {spec.reported_iaa}")
    for caveat in spec.caveats:
        lines.append(f"- Caveat: {caveat}")
    lines.append("")

    lines += [
        "| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | "
        "Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | "
        "Notes |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for ev in sorted(evals, key=lambda e: e.our_label):
        gate_role = "gating" if ev.mapping.gating else "informative"
        strength = ev.mapping.strength or "n/a (synthetic rollup)"
        sample_prevalence = _fmt_rate(ev.n_positives, ev.n_scored)
        population_prevalence = _fmt_rate(ev.population_positives, ev.population_n)
        lines.append(
            f"| `{ev.our_label}` | {strength} | {gate_role} | {ev.best.threshold} | "
            f"{_fmt(ev.best.precision)} {_fmt_ci(ev.best.precision_ci)} | "
            f"{_fmt(ev.best.recall)} {_fmt_ci(ev.best.recall_ci)} | "
            f"{_fmt(ev.best.f1)} {_fmt_ci(ev.best.f1_ci)} | "
            f"{sample_prevalence} | {population_prevalence} | {ev.mapping.notes} |"
        )
    lines.append("")

    lines += [
        "Value at the §6.4 gate threshold: not applicable -- no label has a calibrated "
        "threshold yet (see \"Thresholds\" above).",
        "",
    ]

    if separations:
        lines += _category_separation_section(spec, separations)

    return lines


def _category_separation_section(
    spec: DatasetSpec, separations: list[CategorySeparation]
) -> list[str]:
    lines = [
        "### Separation by source category",
        "",
        "Mean Jev probability of each of our 4 negative labels "
        "(`personal_attack`/`hostility`/`dismissiveness`/`sarcasm`), grouped by this "
        "dataset's own fine-grained category -- independent of any of the label mappings "
        "above. An item belonging to more than one category (e.g. a Ferreira quotation "
        "coded with two TBDF categories) is counted under each.",
        "",
    ]

    if spec.categorizer == "categorize_ferreira":
        lines += [
            "**Reading this table for Ferreira's TBDF scheme**: Ferreira's own inter-rater "
            "agreement was measured on a milder, *sentence-level* construct -- a coder "
            "flagged one quoted sentence within a message, not the message's overall tone. "
            "This benchmark's labels are message-level and calibrated to a broader bar. "
            "The two constructs disagree in both directions: Ferreira sometimes codes a "
            "single mild sentence (\"Did you actually test this?\" -> mocking) that reads as "
            "unremarkable at message level, while Jev sometimes flags a terse, fully "
            "uncoded message (\"No. Just no.\") that Ferreira's coders simply never marked. "
            "If Jev's mean negative-label probability nonetheless separates "
            "TBDF-uncivil-coded messages from civil/uncoded ones clearly (well above vs. "
            "well below the item-level precision/recall table's thresholds), that is "
            "evidence the classifier is picking up on real signal even where item-level F1 "
            "looks modest -- the F1 numbers above are a **lower bound on agreement**, not "
            "proof of a classifier error, because a meaningful share of the disagreement is "
            "definitional (which sentences vs. which messages, and how mild counts) rather "
            "than the classifier misreading the same construct Ferreira's coders used.",
            "",
        ]

    lines += [
        "| Category | n | mean personal_attack | mean hostility | mean dismissiveness | "
        "mean sarcasm |",
        "|---|---|---|---|---|---|",
    ]
    for sep in separations:
        probs = sep.mean_probability
        lines.append(
            f"| {sep.category} | {sep.n} | {_fmt(probs.get('personal_attack'))} | "
            f"{_fmt(probs.get('hostility'))} | {_fmt(probs.get('dismissiveness'))} | "
            f"{_fmt(probs.get('sarcasm'))} |"
        )
    lines.append("")

    return lines
