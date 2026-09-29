"""End-to-end orchestration for `project-health private-run` (issue #110;
DECISIONS.md D1, D10, D17, D18, D22, D23).

sample (stratified threads, seeded, weighted) -> fetch (transient dev@/JIRA
text, never persisted) -> classify (pinned Jev, D10 cost-capped,
input-hash cached and therefore resumable, D22) -> aggregate (per venue x
quarter/year, §5.1 floors, D23 sensitivity) -> `report.md` + `aggregates.json`
(both aggregate-only -- COMMUNITY-HEALTH.md §7.3/§7.4).

**Nothing here ever writes message text, thread ids, message ids, or any
per-person identifier to disk.** The Jev classification cache
(`ClassificationCache`, reused unchanged from `classify/classifier.py`)
never carries text (`ClassificationRecord`'s schema forbids it, D17); this
module's own working state (fetched bodies, per-message author strings used
only to count *distinct* authors for the §5.1 floor) lives in memory for
the duration of one `run_private_run` call and is discarded once
`aggregates.json`/`report.md` are written. This is what makes a re-run
"resumable" in exactly the sense D22 means: the paid Jev call is skipped
for any message whose input hash is already cached, while the (free)
dev@/JIRA text fetch simply happens again every run -- the same
resumability shape `pilot-classify`/`benchmark-public` already use.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_health.classify.classifier import (
    ClassificationCache,
    ClassificationRecord,
    CostCap,
    JevClassifier,
    NormalizedMessage,
    ParentContext,
    RunResult,
    compute_input_hash,
)
from project_health.classify.preprocess import is_automated_sender, preprocess_text
from project_health.classify.text_fetch import (
    JiraCommentRef,
    JiraCommentTextFetcher,
    MailMessageRef,
    PonyMailTextFetcher,
    RawJiraComment,
    build_state,
)
from project_health.config import ProjectConfig
from project_health.private_run import aggregate, frame, report, sensitivity
from project_health.private_run.quarters import quarter_bounds  # noqa: F401  (re-exported for callers)
from project_health.private_run.sample import DEFAULT_K, DEFAULT_SEED, StratumSample, sample_stratum
from project_health.private_run.stats import ThreadCluster

# "Cap 60 messages/thread, earliest first" (issue #110) -- distinct from
# `sample.DEFAULT_K` (60 *threads* per stratum); both default to 60, which
# is a coincidence of the issue's own numbers, not a shared constant.
MAX_MESSAGES_PER_THREAD = 60

# D10's owner-funded monthly cap, issue #110's own stated default.
DEFAULT_MONTHLY_CAP_USD = 25.0

DEFAULT_CACHE_FILENAME = "jev_cache.jsonl"
DEFAULT_AGGREGATES_FILENAME = "aggregates.json"
DEFAULT_REPORT_FILENAME = "report.md"
DEFAULT_SAMPLE_MANIFEST_FILENAME = "sample_manifest.json"

MAILING_LIST_VENUE = "mailing_list"
JIRA_COMMENT_VENUE = "jira_comment"
VENUES: tuple[str, ...] = (MAILING_LIST_VENUE, JIRA_COMMENT_VENUE)


# --- Working state (in-memory only; never written to disk verbatim) --------


@dataclass(frozen=True)
class _PendingMessage:
    call_id: str  # our own id: "mail:<message_id>" | "jira:<issue_key>:<comment_id>"
    thread_id: str
    venue: str
    quarter: str
    weight: float
    author_raw: str  # used only to build an in-memory *distinct-author count*
    normalized: NormalizedMessage
    context: ParentContext


def _year_month(value: datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"


# --- dev@ collection ---------------------------------------------------------


def collect_dev_pending(
    data_dir: str | Path,
    list_name: str,
    domain: str,
    strata: dict[str, StratumSample],
    fetcher: PonyMailTextFetcher,
    automated_sender_patterns: Any,
) -> tuple[list[_PendingMessage], dict[str, int]]:
    """Fetch text (transiently) for every message in every sampled dev@
    thread across `strata`. Returns `(pending, truncated_thread_counts)`;
    `truncated_thread_counts[quarter]` is how many of that quarter's
    sampled threads had more than `MAX_MESSAGES_PER_THREAD` messages
    (issue #110: "record truncation").
    """
    all_thread_ids = {tid for stratum in strata.values() for tid in stratum.sampled_ids}
    messages_by_thread = frame.load_dev_messages_for_threads(data_dir, list_name, all_thread_ids)

    pending: list[_PendingMessage] = []
    truncated_thread_counts: dict[str, int] = {}

    for quarter, stratum in strata.items():
        truncated = 0
        for thread_id in stratum.sampled_ids:
            rows = sorted(messages_by_thread.get(thread_id, []), key=lambda row: row["occurred_at"])
            if len(rows) > MAX_MESSAGES_PER_THREAD:
                truncated += 1
            capped = rows[:MAX_MESSAGES_PER_THREAD]
            survivors = [
                row
                for row in capped
                if not is_automated_sender(row["sender_raw_value"], automated_sender_patterns)
            ]
            if not survivors:
                continue

            refs = [
                MailMessageRef(
                    list_name, domain, _year_month(row["occurred_at"]), row["message_id"]
                )
                for row in survivors
            ]
            raws = fetcher.fetch_messages(refs)
            for ref, row in zip(refs, survivors):
                raw = raws.get(ref.message_id)
                if raw is None:
                    continue
                parent_raw = fetcher.resolve_parent(ref, raw)
                parent_text = (
                    preprocess_text(parent_raw.text, "mailing_list")
                    if parent_raw is not None
                    else None
                )
                text = preprocess_text(raw.text, "mailing_list")
                call_id = f"mail:{ref.message_id}"
                pending.append(
                    _PendingMessage(
                        call_id=call_id,
                        thread_id=thread_id,
                        venue=MAILING_LIST_VENUE,
                        quarter=quarter,
                        weight=stratum.weight,
                        author_raw=row["sender_raw_value"] or "",
                        normalized=NormalizedMessage(
                            message_id=call_id,
                            thread_id=f"mail:{thread_id}",
                            source="mailing_list",
                            text=text,
                        ),
                        context=ParentContext(text=parent_text),
                    )
                )
        truncated_thread_counts[quarter] = truncated

    return pending, truncated_thread_counts


# --- JIRA collection ----------------------------------------------------------


def collect_jira_pending(
    strata: dict[str, StratumSample],
    fetcher: JiraCommentTextFetcher,
    automated_sender_patterns: Any,
) -> tuple[list[_PendingMessage], dict[str, int]]:
    """Fetch the full comment stream (paged, not the pipeline's 20-comment
    cap -- issue #110) for every sampled JIRA issue across `strata`, cap
    each to the earliest `MAX_MESSAGES_PER_THREAD`, and fetch/preprocess
    each surviving comment's text. Returns `(pending,
    truncated_thread_counts)`, same shape as `collect_dev_pending`.
    """
    pending: list[_PendingMessage] = []
    truncated_thread_counts: dict[str, int] = {}

    for quarter, stratum in strata.items():
        truncated = 0
        for issue_key in stratum.sampled_ids:
            comments: list[RawJiraComment] = fetcher.fetch_issue_comments(issue_key)
            ordered = sorted(comments, key=lambda c: c.created_at or "")
            if len(ordered) > MAX_MESSAGES_PER_THREAD:
                truncated += 1
            capped = ordered[:MAX_MESSAGES_PER_THREAD]

            for comment in capped:
                if is_automated_sender(comment.author, automated_sender_patterns):
                    continue
                ref = JiraCommentRef(issue_key, comment.comment_id)
                parent_raw = fetcher.resolve_parent(ref)
                parent_text = (
                    preprocess_text(parent_raw.text, "jira_comment")
                    if parent_raw is not None
                    else None
                )
                text = preprocess_text(comment.text, "jira_comment")
                call_id = f"jira:{issue_key}:{comment.comment_id}"
                pending.append(
                    _PendingMessage(
                        call_id=call_id,
                        thread_id=issue_key,
                        venue=JIRA_COMMENT_VENUE,
                        quarter=quarter,
                        weight=stratum.weight,
                        author_raw=comment.author or "",
                        normalized=NormalizedMessage(
                            message_id=call_id,
                            thread_id=f"jira:{issue_key}",
                            source="jira_comment",
                            text=text,
                        ),
                        context=ParentContext(text=parent_text),
                    )
                )
        truncated_thread_counts[quarter] = truncated

    return pending, truncated_thread_counts


# --- Classification fan-out (avoids the duplicate-input-hash bug) -----------


def _fan_out_by_call_id(
    pending: list[_PendingMessage], cache: ClassificationCache, classifier: JevClassifier
) -> dict[str, ClassificationRecord]:
    """`{call_id: ClassificationRecord}` for every pending message that has
    a cached record, joined by *its own* recomputed `input_hash` rather
    than `ClassificationRecord.message_id`. Mirrors `benchmark_public.
    runner._fan_out_by_input_hash`'s fix for the same bug: two distinct
    pending messages can preprocess to byte-identical `(text, parent_text,
    source)` (e.g. two "+1" JIRA comments) and therefore share one cached
    record, whose `.message_id` field can only ever hold the *first*
    message's id to reach that hash -- looking records up by `record.
    message_id` would silently drop every later duplicate from
    aggregation.
    """
    result: dict[str, ClassificationRecord] = {}
    for pm in pending:
        state = build_state(pm.normalized.text, pm.normalized.source, pm.context.text)
        input_hash = compute_input_hash(state, classifier.question_set_version, classifier.model_id)
        record = cache.get(input_hash)
        if record is not None:
            result[pm.call_id] = record
    return result


def build_clusters(
    pending: list[_PendingMessage], records_by_call_id: dict[str, ClassificationRecord]
) -> tuple[dict[tuple[str, str], list[ThreadCluster]], dict[tuple[str, str], set[str]]]:
    """Group classified pending messages into `ThreadCluster`s per (venue,
    quarter) cell, plus the cell's set of distinct raw author strings (used
    only in-memory for the §5.1 floor -- never persisted).
    """
    by_thread: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pm in pending:
        record = records_by_call_id.get(pm.call_id)
        if record is None:
            continue
        key = (pm.venue, pm.quarter, pm.thread_id)
        entry = by_thread.setdefault(key, {"weight": pm.weight, "messages": [], "authors": set()})
        probabilities = {label_id: label.probability for label_id, label in record.labels.items()}
        entry["messages"].append(probabilities)
        entry["authors"].add(pm.author_raw)

    clusters_by_cell: dict[tuple[str, str], list[ThreadCluster]] = {}
    authors_by_cell: dict[tuple[str, str], set[str]] = {}
    for (venue, quarter, thread_id), entry in by_thread.items():
        cell_key = (venue, quarter)
        clusters_by_cell.setdefault(cell_key, []).append(
            ThreadCluster(
                thread_id=thread_id, weight=entry["weight"], messages=tuple(entry["messages"])
            )
        )
        authors_by_cell.setdefault(cell_key, set()).update(entry["authors"])
    return clusters_by_cell, authors_by_cell


# --- Sample manifest (--sample-only) -----------------------------------------


def _build_sample_manifest(
    seed: int,
    k: int,
    quarters: list[str],
    dev_strata: dict[str, StratumSample],
    jira_strata: dict[str, StratumSample],
) -> dict[str, Any]:
    def _strata_dict(strata: dict[str, StratumSample]) -> dict[str, Any]:
        return {
            q: {"population": s.population, "sampled": len(s.sampled_ids), "weight": s.weight}
            for q, s in strata.items()
        }

    return {
        "seed": seed,
        "k": k,
        "quarters": quarters,
        MAILING_LIST_VENUE: _strata_dict(dev_strata),
        JIRA_COMMENT_VENUE: _strata_dict(jira_strata),
    }


# --- Result -------------------------------------------------------------------


@dataclass(frozen=True)
class PrivateRunResult:
    aggregates: dict[str, Any]
    aggregates_path: Path | None
    report_path: Path | None
    sample_manifest_path: Path | None
    run_result: RunResult | None
    elapsed_seconds: float


# --- End-to-end orchestration -------------------------------------------------


def run_private_run(
    *,
    project_config: ProjectConfig,
    data_dir: str | Path,
    out_dir: str | Path,
    quarters: list[str],
    seed: int = DEFAULT_SEED,
    k: int = DEFAULT_K,
    concurrency: int = 4,
    monthly_cap_usd: float = DEFAULT_MONTHLY_CAP_USD,
    classifier_version: str = "1.0.0",
    sample_only: bool = False,
    api_key: str | None = None,
    pricing_config_path: str | Path | None = None,
    ponymail_transport: Any = None,
    jira_transport: Any = None,
    jev_async_transport: Any = None,
    question_set: Any = None,
    public_benchmark_path: str | Path | None = None,
    cache_filename: str = DEFAULT_CACHE_FILENAME,
    aggregates_filename: str = DEFAULT_AGGREGATES_FILENAME,
    report_filename: str = DEFAULT_REPORT_FILENAME,
    sample_manifest_filename: str = DEFAULT_SAMPLE_MANIFEST_FILENAME,
    bootstrap_iterations: int = aggregate.DEFAULT_BOOTSTRAP_ITERATIONS,
    clock: Any = lambda: datetime.now(timezone.utc),
) -> PrivateRunResult:
    """Run the whole private-run pipeline once. `out_dir` must already have
    been checked by the caller to be outside the public repo (the CLI does
    this via `label.safety.assert_outside_repo`, mirroring every other
    private-output command in this project) -- this function does not
    re-check it, matching `pilot_classify.run_pilot_classify`'s own
    division of responsibility.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    quarters = sorted(quarters)
    mailing_lists = project_config.mailing_lists
    issue_tracker = project_config.issue_tracker
    if mailing_lists is None or issue_tracker is None:
        raise ValueError(
            "private-run requires mailing_lists and issue_tracker in the project config"
        )

    list_name = "dev"  # issue #110: dev@ only, not user@
    domain = mailing_lists.domain
    project_key = issue_tracker.project_key
    base_url = issue_tracker.base_url
    automated_sender_patterns = project_config.automated_senders

    quarter_set = set(quarters)
    dev_frame = frame.load_dev_thread_frame(data_dir, list_name, quarter_set)
    jira_frame = frame.load_jira_thread_frame(data_dir, project_key, quarter_set)

    dev_strata = {
        q: sample_stratum(MAILING_LIST_VENUE, q, dev_frame.get(q, []), seed=seed, k=k)
        for q in quarters
    }
    jira_strata = {
        q: sample_stratum(JIRA_COMMENT_VENUE, q, jira_frame.get(q, []), seed=seed, k=k)
        for q in quarters
    }

    if sample_only:
        manifest = _build_sample_manifest(seed, k, quarters, dev_strata, jira_strata)
        manifest_path = out_dir / sample_manifest_filename
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return PrivateRunResult(
            aggregates={},
            aggregates_path=None,
            report_path=None,
            sample_manifest_path=manifest_path,
            run_result=None,
            elapsed_seconds=0.0,
        )

    pending: list[_PendingMessage] = []
    truncated_thread_counts: dict[str, dict[str, int]] = {}

    with PonyMailTextFetcher(transport=ponymail_transport) as ponymail_fetcher:
        dev_pending, dev_truncated = collect_dev_pending(
            data_dir, list_name, domain, dev_strata, ponymail_fetcher, automated_sender_patterns
        )
    pending += dev_pending
    truncated_thread_counts[MAILING_LIST_VENUE] = dev_truncated

    with JiraCommentTextFetcher(base_url, transport=jira_transport) as jira_fetcher:
        jira_pending, jira_truncated = collect_jira_pending(
            jira_strata, jira_fetcher, automated_sender_patterns
        )
    pending += jira_pending
    truncated_thread_counts[JIRA_COMMENT_VENUE] = jira_truncated

    cache = ClassificationCache(out_dir / cache_filename)
    cost_cap = CostCap.from_config(
        monthly_cap_usd=monthly_cap_usd, pricing_config_path=pricing_config_path
    )
    classifier = JevClassifier(
        api_key=api_key,
        question_set=question_set,
        cache=cache,
        cost_cap=cost_cap,
        concurrency=concurrency,
        classifier_version=classifier_version,
        async_transport=jev_async_transport,
    )

    calls = [(pm.normalized, pm.context) for pm in pending]
    start = time.monotonic()
    if calls:
        run_result = classifier.run(calls)
    else:
        run_result = RunResult(
            status="completed",
            records=[],
            calls_made=0,
            cache_hits=0,
            input_tokens_used=0,
            output_tokens_used=0,
            estimated_cost_usd=0.0,
        )
    elapsed_seconds = time.monotonic() - start

    records_by_call_id = _fan_out_by_call_id(pending, cache, classifier)
    clusters_by_cell, authors_by_cell = build_clusters(pending, records_by_call_id)

    benchmark_path = public_benchmark_path or sensitivity.DEFAULT_PUBLIC_BENCHMARK_PATH
    sensitivity_thresholds = sensitivity.load_gating_thresholds(benchmark_path)

    strata_by_venue = {MAILING_LIST_VENUE: dev_strata, JIRA_COMMENT_VENUE: jira_strata}

    cells_by_quarter: dict[str, dict[str, Any]] = {venue: {} for venue in VENUES}
    cells_by_year: dict[str, dict[str, Any]] = {venue: {} for venue in VENUES}
    year_clusters: dict[str, dict[str, list[ThreadCluster]]] = {venue: {} for venue in VENUES}
    year_authors: dict[str, dict[str, set[str]]] = {venue: {} for venue in VENUES}

    for venue in VENUES:
        strata = strata_by_venue[venue]
        for quarter in quarters:
            clusters = clusters_by_cell.get((venue, quarter), [])
            authors = authors_by_cell.get((venue, quarter), set())
            stratum = strata[quarter]
            cells_by_quarter[venue][quarter] = aggregate.aggregate_cell(
                clusters,
                authors,
                seed=seed,
                cell_key=f"{venue}:{quarter}",
                sensitivity_thresholds=sensitivity_thresholds,
                bootstrap_iterations=bootstrap_iterations,
                threads_population=stratum.population,
                threads_sampled=len(stratum.sampled_ids),
            )
            year = quarter[:4]
            year_clusters[venue].setdefault(year, []).extend(clusters)
            year_authors[venue].setdefault(year, set()).update(authors)

        for year, clusters in year_clusters[venue].items():
            authors = year_authors[venue][year]
            year_quarters = [q for q in quarters if q.startswith(year)]
            cells_by_year[venue][year] = aggregate.aggregate_cell(
                clusters,
                authors,
                seed=seed,
                cell_key=f"{venue}:year:{year}",
                sensitivity_thresholds=sensitivity_thresholds,
                bootstrap_iterations=bootstrap_iterations,
                threads_population=sum(strata[q].population for q in year_quarters),
                threads_sampled=sum(len(strata[q].sampled_ids) for q in year_quarters),
            )

    venue_totals: dict[str, Any] = {}
    for venue in VENUES:
        strata = strata_by_venue[venue]
        venue_totals[venue] = {
            "messages_classified": sum(
                cell["messages_classified"] for cell in cells_by_quarter[venue].values()
            ),
            "threads_sampled": sum(len(s.sampled_ids) for s in strata.values()),
            "threads_population": sum(s.population for s in strata.values()),
        }

    mean_latency = elapsed_seconds / run_result.calls_made if run_result.calls_made else None
    cost_summary = {
        "status": run_result.status,
        "calls_made": run_result.calls_made,
        "cache_hits": run_result.cache_hits,
        "input_tokens_used": run_result.input_tokens_used,
        "output_tokens_used": run_result.output_tokens_used,
        "estimated_cost_usd": run_result.estimated_cost_usd,
        "elapsed_seconds": elapsed_seconds,
        "mean_latency_seconds_per_call": mean_latency,
        "classifier_version": classifier.classifier_version,
        "question_set_version": classifier.question_set_version,
        "model_id_pinned": classifier.model_id,
    }

    aggregates: dict[str, Any] = {
        "generated_at": clock().isoformat(),
        "seed": seed,
        "k": k,
        "quarters": quarters,
        "venues": list(VENUES),
        "frame_definition": {
            MAILING_LIST_VENUE: (
                "thread = a dev@ Pony Mail thread (ponymail/message_thread); "
                "quarter = the calendar quarter of the thread's started_at (root "
                "message time); every message in a sampled thread is attributed to "
                "that one quarter, even if later replies land in a following "
                "quarter."
            ),
            JIRA_COMMENT_VENUE: (
                "thread = one CASSANDRA JIRA issue's comment stream; quarter = the "
                "calendar quarter of the issue's created_at (jira/issue), since a "
                "JIRA issue has no separate thread-start concept in this schema "
                "and created_at is the closest analog to a mailing-list thread's "
                "root message time."
            ),
        },
        "scope_note": (
            "GitHub PR comments are out of scope for v1 (DECISIONS.md D7) -- only "
            "dev@ and JIRA comments are covered by this run."
        ),
        "sensitivity_thresholds": sensitivity_thresholds,
        "floors": {
            "min_messages": aggregate.MIN_MESSAGES,
            "min_distinct_authors": aggregate.MIN_DISTINCT_AUTHORS,
        },
        "truncated_thread_counts": truncated_thread_counts,
        "cost_summary": cost_summary,
        "venue_totals": venue_totals,
        "cells_by_quarter": cells_by_quarter,
        "cells_by_year": cells_by_year,
    }

    aggregates_path = out_dir / aggregates_filename
    aggregates_path.write_text(
        json.dumps(aggregates, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    report_markdown = report.render_report_markdown(aggregates)
    report_path = out_dir / report_filename
    report_path.write_text(report_markdown, encoding="utf-8")

    return PrivateRunResult(
        aggregates=aggregates,
        aggregates_path=aggregates_path,
        report_path=report_path,
        sample_manifest_path=None,
        run_result=run_result,
        elapsed_seconds=elapsed_seconds,
    )
