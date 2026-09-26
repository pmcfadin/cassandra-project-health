"""`project-health pilot-evaluate` (issue #47; DECISIONS.md D17, D18, D22;
COMMUNITY-HEALTH.md §6).

Joins the pilot corpus (`classify/sample.py`), Jev's classification records
(`pilot/classify_runner.py`'s output), and one or more raters' label JSONL
files (`label/store.py`), then computes everything COMMUNITY-HEALTH.md §6
asks the pilot to measure: per-label precision/recall/F1 at a threshold
sweep with bootstrap 95% CIs, a reliability diagram, prevalence estimates
from the prevalence stratum only, tone agreement, rater time, and (with 2+
raters) Krippendorff's alpha.

Ground-truth aggregation across raters (`aggregate_human_mark`) is a fixed,
documented, deterministic rule -- never an LLM tie-break (D22): a rater's
"unsure" mark is dropped, and if every remaining mark then agrees, that is
the aggregate; if raters disagree (a real tie, e.g. one "yes" and one "no"),
the item is excluded from that label's scoring, exactly as an all-"unsure"
item is. Both cases count toward `n_excluded`; §6.2's adjudicator process is
what the *full* benchmark uses to resolve these, not this pilot (D18: the
owner rates alone in the pilot; more raters join before the full
benchmark).

Everything here is a deterministic, offline computation over already-loaded
JSONL -- no network access, no model call (that happens once, earlier, in
`pilot-classify`; D22).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from project_health.classify.classifier import ClassificationRecord
from project_health.classify.questions import MESSAGE_LEVEL_LABELS, load_question_set
from project_health.label.store import CorpusItem, load_corpus, read_label_records
from project_health.pilot import stats

DEFAULT_RECORDS_FILENAME = "classifications.jsonl"
DEFAULT_SUMMARY_FILENAME = "summary.json"
DEFAULT_SEED = 47
DEFAULT_BOOTSTRAP_ITERATIONS = 1000

GATE_FLOORS: dict[str, dict[str, float]] = {
    # COMMUNITY-HEALTH.md §6.4's table, verbatim.
    "reputational_harm": {"precision": 0.85, "recall": 0.60, "f1": 0.70},
    "friction": {"precision": 0.80, "recall": 0.60, "f1": 0.70},
    "argument": {"precision": 0.75, "recall": 0.75, "f1": 0.75},
}

# A point estimate more than this far below its gate floor is treated as a
# likely real (not just pilot-noise) shortfall -- see `classify_gate`'s
# docstring for the full documented rule.
CATASTROPHIC_MARGIN = 0.20
# Below this many ground-truth positives, a shortfall is attributed to pilot
# sample-size noise (revise/gather more data) rather than treated as
# confident evidence the label needs taxonomy work or should be dropped.
MIN_POSITIVES_FOR_CONFIDENT_VERDICT = 10

_VALID_MARKS = frozenset({"yes", "no", "unsure"})


# --- Loading ---------------------------------------------------------------------------


def load_classification_records(
    results_dir: str | Path, records_filename: str = DEFAULT_RECORDS_FILENAME
) -> dict[str, ClassificationRecord]:
    """`{message_id: ClassificationRecord}` from `pilot-classify`'s output
    directory (or a direct path to its records JSONL)."""
    results_path = Path(results_dir)
    path = results_path / records_filename if results_path.is_dir() else results_path
    records: dict[str, ClassificationRecord] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = ClassificationRecord.model_validate(json.loads(line))
            records[record.message_id] = record
    return records


def load_jev_summary(
    results_dir: str | Path, summary_filename: str = DEFAULT_SUMMARY_FILENAME
) -> dict[str, Any] | None:
    """`pilot-classify`'s cost/latency summary, or `None` if not found
    (e.g. `results_dir` is a bare records file, not a `pilot-classify`
    output directory)."""
    results_path = Path(results_dir)
    path = (
        results_path / summary_filename
        if results_path.is_dir()
        else results_path.with_name(summary_filename)
    )
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_rater_labels(paths: list[str | Path]) -> dict[str, dict[str, dict[str, Any]]]:
    """`{rater_name: {item_id: label_record}}`, one entry per `--labels`
    path. The rater name is read from the records themselves (`store.py`'s
    `rater` field) when consistent within the file, falling back to the
    file's stem -- so two files that both happen to say `rater: "pmcfadin"`
    are still kept distinct by filename, never silently merged."""
    raters: dict[str, dict[str, dict[str, Any]]] = {}
    for path in paths:
        records = read_label_records(path)
        if not records:
            continue
        names = {r.get("rater") for r in records.values() if r.get("rater")}
        rater_name = next(iter(names)) if len(names) == 1 else Path(path).stem
        if rater_name in raters:
            rater_name = f"{rater_name}:{Path(path).stem}"
        raters[rater_name] = records
    return raters


# --- Ground-truth aggregation ------------------------------------------------------------


def aggregate_human_mark(marks: list[str]) -> str:
    """The pilot's fixed, documented ground-truth rule (module docstring):
    drop "unsure" marks, then "yes"/"no" if every remaining rater agrees,
    else "unsure" (excluded) on a real tie/disagreement. `marks` with
    nothing left after dropping "unsure" is also "unsure"."""
    effective = [m for m in marks if m in ("yes", "no")]
    if not effective:
        return "unsure"
    if all(m == effective[0] for m in effective):
        return effective[0]
    return "unsure"


def aggregate_human_tone(tones: list[int]) -> int:
    """The single "human level" issue #47 compares Jev's argmax against:
    the rounded mean of every rater's tone rating for the item (identity
    for a single rater, the pilot's normal case per D18). Python's
    banker's rounding on an exact `.5` is an acceptable, documented
    tie-break here since it only ever affects the boundary between two
    adjacent tone levels, never a yes/no ground-truth call."""
    return round(statistics.mean(tones))


def _jev_tone_argmax(record: ClassificationRecord) -> int | None:
    if record.tone_intensity is None or not record.tone_intensity.probabilities:
        return None
    return int(max(record.tone_intensity.probabilities.items(), key=lambda kv: kv[1])[0])


# --- Per-label evaluation ----------------------------------------------------------------


@dataclass(frozen=True)
class LabelEvaluation:
    label_id: str
    label_group: str
    n_candidates: int  # items Jev classified for this label
    n_scored: int  # items with usable (non-excluded) ground truth
    n_excluded: int  # aggregate "unsure" (all-unsure or a real tie)
    n_positives: int
    sweep: list[stats.ThresholdMetrics]
    best: stats.ThresholdMetrics
    reliability: list[stats.ReliabilityBin]
    prevalence: dict[str, Any]
    stratum_breakdown: dict[str, dict[str, Any]]
    gate: dict[str, Any]
    full_benchmark_n_estimate: int
    item_ids_scored: list[str] = field(default_factory=list)  # private-report detail only


def classify_gate(
    label_group: str, best: stats.ThresholdMetrics, n_positives: int
) -> dict[str, Any]:
    """Deterministic §6.4 gate verdict at the recommended threshold.
    **Documented rule** (issue #47's "computed by rules you document"):

    - `likely_pass`: every point estimate (precision, recall, F1) already
      clears its §6.4 floor.
    - Otherwise, if any point estimate falls more than
      `CATASTROPHIC_MARGIN` (0.20) below its floor *and* there are at least
      `MIN_POSITIVES_FOR_CONFIDENT_VERDICT` (10) ground-truth positives to
      trust that shortfall isn't just pilot sample-size noise:
      `drop_or_revise_taxonomy` -- a shortfall this large, on this much
      data, reads as a genuine taxonomy/classifier problem
      (COMMUNITY-HEALTH.md §6.3's "a taxonomy problem, not a rater problem"
      framing, applied here to precision/recall).
    - Otherwise: `revise_or_gather_more_data` -- either the shortfall is
      small enough that recalibrating the threshold or tightening the
      question wording might clear it, or there simply isn't enough
      pilot-scale data yet to tell a real problem from noise.
    """
    floors = GATE_FLOORS[label_group]
    meets = (
        best.precision >= floors["precision"]
        and best.recall >= floors["recall"]
        and best.f1 >= floors["f1"]
    )
    if meets:
        status = "likely_pass"
        reason = (
            "point estimates at the recommended threshold clear every §6.4 floor for this group"
        )
    else:
        catastrophic = (
            best.precision < floors["precision"] - CATASTROPHIC_MARGIN
            or best.recall < floors["recall"] - CATASTROPHIC_MARGIN
            or best.f1 < floors["f1"] - CATASTROPHIC_MARGIN
        )
        if catastrophic and n_positives >= MIN_POSITIVES_FOR_CONFIDENT_VERDICT:
            status = "drop_or_revise_taxonomy"
            reason = (
                f"a point estimate is more than {CATASTROPHIC_MARGIN} below its §6.4 floor "
                f"with {n_positives} ground-truth positives to support the estimate"
            )
        else:
            status = "revise_or_gather_more_data"
            reason = (
                "at least one §6.4 floor is not met, but the shortfall is small and/or "
                f"only {n_positives} ground-truth positive(s) support the estimate "
                f"(floor: {MIN_POSITIVES_FOR_CONFIDENT_VERDICT}) -- not enough to "
                "distinguish a real problem from pilot-scale noise"
            )
    return {
        "label_group": label_group,
        "floors": floors,
        "status": status,
        "reason": reason,
        "precision": best.precision,
        "recall": best.recall,
        "f1": best.f1,
        "threshold": best.threshold,
    }


def evaluate_label(
    label_id: str,
    label_group: str,
    corpus_by_id: dict[str, CorpusItem],
    classification_by_id: dict[str, ClassificationRecord],
    rater_labels: dict[str, dict[str, dict[str, Any]]],
    *,
    seed: int,
    bootstrap_iterations: int,
) -> LabelEvaluation:
    y_true: list[int] = []
    y_prob: list[float] = []
    strata: list[str] = []
    item_ids: list[str] = []
    n_candidates = 0
    n_excluded = 0

    for item_id, record in classification_by_id.items():
        if label_id not in record.labels:
            continue
        n_candidates += 1
        marks = [
            rater_labels[rater][item_id]["labels"][label_id]
            for rater in rater_labels
            if item_id in rater_labels[rater]
            and label_id in rater_labels[rater][item_id].get("labels", {})
        ]
        if not marks:
            continue  # no human ever rated this item at all -- not "excluded", just unscored
        aggregate = aggregate_human_mark(marks)
        if aggregate == "unsure":
            n_excluded += 1
            continue
        y_true.append(1 if aggregate == "yes" else 0)
        y_prob.append(record.labels[label_id].probability)
        item = corpus_by_id.get(item_id)
        strata.append(item.stratum if item is not None else "unknown")
        item_ids.append(item_id)

    sweep = stats.sweep_thresholds(y_true, y_prob, seed=seed, iterations=bootstrap_iterations)
    best = stats.best_threshold_by_f1(sweep)
    reliability = stats.reliability_bins(y_true, y_prob)
    n_positives = sum(y_true)

    prevalence_true = [t for t, s in zip(y_true, strata) if s == "prevalence"]
    successes = sum(prevalence_true)
    n_prev = len(prevalence_true)
    prevalence_ci = stats.wilson_interval(successes, n_prev)
    prevalence = {
        "successes": successes,
        "n": n_prev,
        "proportion": (successes / n_prev) if n_prev else None,
        "ci": prevalence_ci,
    }

    stratum_breakdown: dict[str, dict[str, Any]] = {}
    for stratum_name in ("prevalence", "enrichment"):
        idxs = [i for i, s in enumerate(strata) if s == stratum_name]
        if idxs:
            st_true = [y_true[i] for i in idxs]
            st_prob = [y_prob[i] for i in idxs]
            confusion = stats.confusion_at_threshold(st_true, st_prob, best.threshold)
            p, r, f1 = stats.precision_recall_f1(confusion)
            stratum_breakdown[stratum_name] = {
                "n": len(idxs),
                "precision": p,
                "recall": r,
                "f1": f1,
            }
        else:
            stratum_breakdown[stratum_name] = {
                "n": 0,
                "precision": None,
                "recall": None,
                "f1": None,
            }

    gate = classify_gate(label_group, best, n_positives)
    full_benchmark_n_estimate = max(
        stats.required_n_for_ci_half_width(
            prevalence["proportion"] if prevalence["proportion"] is not None else 0.5
        ),
        stats.required_n_for_ci_half_width(best.f1),
    )

    return LabelEvaluation(
        label_id=label_id,
        label_group=label_group,
        n_candidates=n_candidates,
        n_scored=len(y_true),
        n_excluded=n_excluded,
        n_positives=n_positives,
        sweep=sweep,
        best=best,
        reliability=reliability,
        prevalence=prevalence,
        stratum_breakdown=stratum_breakdown,
        gate=gate,
        full_benchmark_n_estimate=full_benchmark_n_estimate,
        item_ids_scored=item_ids,
    )


# --- Tone agreement --------------------------------------------------------------------


@dataclass(frozen=True)
class ToneEvaluation:
    n_paired: int
    exact_agreement: float | None
    off_by_one: float | None
    weighted_kappa: float | None


def evaluate_tone(
    classification_by_id: dict[str, ClassificationRecord],
    rater_labels: dict[str, dict[str, dict[str, Any]]],
    *,
    levels: list[int] | None = None,
) -> ToneEvaluation:
    """`levels` defaults to `questions_v1.yaml`'s full `tone_intensity` scale
    (0-4), not just the values actually observed in this pilot's data --
    the weighted kappa's distance weighting is defined against the real
    ordinal scale (COMMUNITY-HEALTH.md §1.2's tone levels), and using only
    the observed range would both misrepresent "how far apart" two levels
    are and (at the extreme, if every human/Jev tone call in a small pilot
    happens to coincide) divide by zero when only one distinct value was
    ever observed (`stats.weighted_kappa`'s `k - 1` denominator)."""
    human: list[int] = []
    jev: list[int] = []
    for item_id, record in classification_by_id.items():
        jev_argmax = _jev_tone_argmax(record)
        if jev_argmax is None:
            continue
        tones = [
            rater_labels[rater][item_id]["tone"]
            for rater in rater_labels
            if item_id in rater_labels[rater]
        ]
        if not tones:
            continue
        human.append(aggregate_human_tone(tones))
        jev.append(jev_argmax)

    if not human:
        return ToneEvaluation(
            n_paired=0, exact_agreement=None, off_by_one=None, weighted_kappa=None
        )

    if levels is None:
        levels = sorted(level["level"] for level in load_question_set().score["levels"])
    return ToneEvaluation(
        n_paired=len(human),
        exact_agreement=stats.exact_agreement_rate(human, jev),
        off_by_one=stats.off_by_one_rate(human, jev),
        weighted_kappa=stats.weighted_kappa(human, jev, levels),
    )


# --- Rater time --------------------------------------------------------------------------


@dataclass(frozen=True)
class RaterTimeEvaluation:
    per_rater: dict[str, dict[str, Any]]
    overall: dict[str, Any]


def evaluate_rater_time(rater_labels: dict[str, dict[str, dict[str, Any]]]) -> RaterTimeEvaluation:
    per_rater: dict[str, dict[str, Any]] = {}
    all_minutes: list[float] = []
    for rater, records in rater_labels.items():
        minutes = [rec["seconds"] / 60.0 for rec in records.values() if "seconds" in rec]
        all_minutes.extend(minutes)
        per_rater[rater] = {
            "n": len(minutes),
            "median_minutes": stats.median(minutes),
            "p90_minutes": stats.percentile(minutes, 90),
        }
    overall = {
        "n": len(all_minutes),
        "median_minutes": stats.median(all_minutes),
        "p90_minutes": stats.percentile(all_minutes, 90),
    }
    return RaterTimeEvaluation(per_rater=per_rater, overall=overall)


# --- Inter-rater agreement (Krippendorff's alpha, §6.3) -----------------------------------


def evaluate_agreement(
    label_ids: list[str],
    corpus_items: list[CorpusItem],
    rater_labels: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, float | None]:
    """`{label_id: alpha}` across every ratable label, `None` per label with
    fewer than 2 items having >=2 non-missing ratings (`krippendorff_alpha_
    nominal`'s own "undefined" case). Returns `{}` (not computed at all)
    with fewer than 2 raters -- callers must check `len(rater_labels) < 2`
    themselves and report "no agreement statistic exists" (issue #47)
    rather than reading a missing/`None` alpha as "zero raters agreed"."""
    if len(rater_labels) < 2:
        return {}
    raters = sorted(rater_labels)
    result: dict[str, float | None] = {}
    for label_id in label_ids:
        rows: list[list[int | None]] = []
        for item in corpus_items:
            row: list[int | None] = []
            for rater in raters:
                record = rater_labels[rater].get(item.id)
                mark = record["labels"].get(label_id) if record else None
                if mark in ("yes", "no"):
                    row.append(1 if mark == "yes" else 0)
                else:
                    row.append(None)  # missing or "unsure" -- excluded, per module docstring
            rows.append(row)
        result[label_id] = stats.krippendorff_alpha_nominal(rows)
    return result


# --- End-to-end evaluation -----------------------------------------------------------------


@dataclass(frozen=True)
class PilotEvaluationResult:
    label_evaluations: dict[str, LabelEvaluation]
    tone: ToneEvaluation
    rater_time: RaterTimeEvaluation
    agreement: dict[str, float | None]
    jev_summary: dict[str, Any] | None
    rater_names: list[str]
    n_corpus_items: int
    n_classified_items: int


def evaluate_pilot(
    *,
    corpus_path: str | Path,
    results_dir: str | Path,
    label_paths: list[str | Path],
    seed: int = DEFAULT_SEED,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
) -> PilotEvaluationResult:
    items = load_corpus(corpus_path)
    corpus_by_id = {item.id: item for item in items}
    classification_by_id = load_classification_records(results_dir)
    jev_summary = load_jev_summary(results_dir)
    rater_labels = load_rater_labels(label_paths)

    question_set = load_question_set()
    label_evaluations: dict[str, LabelEvaluation] = {}
    for index, label_id in enumerate(sorted(MESSAGE_LEVEL_LABELS)):
        group = question_set.labels[label_id]["label_group"]
        label_evaluations[label_id] = evaluate_label(
            label_id,
            group,
            corpus_by_id,
            classification_by_id,
            rater_labels,
            seed=seed + index * 1000,
            bootstrap_iterations=bootstrap_iterations,
        )

    tone = evaluate_tone(classification_by_id, rater_labels)
    rater_time = evaluate_rater_time(rater_labels)
    agreement = evaluate_agreement(sorted(MESSAGE_LEVEL_LABELS), items, rater_labels)

    return PilotEvaluationResult(
        label_evaluations=label_evaluations,
        tone=tone,
        rater_time=rater_time,
        agreement=agreement,
        jev_summary=jev_summary,
        rater_names=sorted(rater_labels),
        n_corpus_items=len(items),
        n_classified_items=len(classification_by_id),
    )
