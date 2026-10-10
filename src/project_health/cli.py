"""`project-health` console script (ARCHITECTURE.md §5, §7.3, §11; issue #9).

Registered as a `console_scripts`-style entry point in `pyproject.toml`
(`[project.scripts] project-health = "project_health.cli:main"`). Usage:

    project-health run --project projects/cassandra.yaml \\
        --data-dir <path> --workdir <path> \\
        [--sources git,jira,ponymail] [--site-out <dir>]

    project-health label --corpus <benchmark-checkout>/corpus/v0.jsonl \\
        --labels <benchmark-checkout>/labels/<rater>.jsonl \\
        --rater <name> [--port 8765] [--no-browser]
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import date
from pathlib import Path

from project_health.benchmark_public.evaluate import DEFAULT_SEED as BENCHMARK_PUBLIC_DEFAULT_SEED
from project_health.benchmark_public.evaluate import compute_all_category_separations
from project_health.benchmark_public.evaluate import evaluate_benchmark
from project_health.benchmark_public.mapping import load_label_mapping
from project_health.benchmark_public.registry import load_registry
import project_health.benchmark_public.report as benchmark_public_report
from project_health.benchmark_public.runner import (
    DEFAULT_MONTHLY_CAP_USD as BENCHMARK_PUBLIC_DEFAULT_CAP,
)
from project_health.benchmark_public.runner import run_benchmark
from project_health.classify.classifier import load_jev_key_from_dotenv
from project_health.classify.sample import (
    DEFAULT_JIRA_BLOCK_SIZE,
    DEFAULT_JIRA_MIN_ISSUES_PER_YEAR,
    DEFAULT_JIRA_TOTAL_ISSUE_TARGET,
    DEFAULT_JIRA_MAX_CALLS,
    run_pilot_sample,
)
from project_health.classify.subsample import (
    DEFAULT_SUBSAMPLE_SEED,
    DEFAULT_SUBSAMPLE_SIZE,
    run_pilot_subsample,
)
from project_health.config import load_project
from project_health.governance.policy import DEFAULT_POLICY_PATH
from project_health.governance.verify_sources import verify_policy_sources
from project_health.label.label_set import LabelSetError
from project_health.label.question_set import QuestionSetReadError
from project_health.label.safety import UnsafePathError, assert_outside_repo, find_public_repo_root
from project_health.label.server import LabelQuestionMismatchError, make_server
from project_health.label.store import CorpusError
from project_health.pilot.classify_runner import (
    DEFAULT_MONTHLY_CAP_USD,
    run_pilot_classify,
)
from project_health.pilot.evaluate import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_SEED,
    evaluate_pilot,
)
from project_health.pilot.report import (
    render_private_report_markdown,
    render_public_report_markdown,
)
from project_health.peers.config import load_peers
from project_health.peers.pipeline import run_peers_collection
from project_health.pipeline import ALL_SOURCES, run_pipeline
from project_health.private_run.quarters import parse_quarters_arg
from project_health.private_run.runner import DEFAULT_MONTHLY_CAP_USD as PRIVATE_RUN_DEFAULT_CAP
from project_health.private_run.runner import run_private_run
from project_health.private_run.newcomer import DEFAULT_NEWCOMER_N
from project_health.private_run.publish import (
    SanitizeError,
    write_conversation_patterns_snapshot,
    write_threads_snapshot,
)
from project_health.private_run.sample import DEFAULT_K as PRIVATE_RUN_DEFAULT_K
from project_health.private_run.sample import DEFAULT_SEED as PRIVATE_RUN_DEFAULT_SEED


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="project-health")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run one collect -> identity -> metrics -> manifest (-> site) pass"
    )
    run_parser.add_argument("--project", required=True, help="Path to a projects/<id>.yaml file")
    run_parser.add_argument(
        "--data-dir", required=True, help="Root of the raw/snapshots/manifests/state data layout"
    )
    run_parser.add_argument(
        "--workdir", required=True, help="Local git working copy/clone used by the git collector"
    )
    run_parser.add_argument(
        "--sources",
        default=None,
        help=f"Comma-separated sources to collect this run (default: all of {list(ALL_SOURCES)})",
    )
    run_parser.add_argument(
        "--site-out", default=None, help="If set, also generate the static site into this directory"
    )
    run_parser.add_argument(
        "--max-jira-issues",
        type=int,
        default=None,
        help="Cap the number of JIRA issues fetched this run (testing / smoke runs)",
    )
    run_parser.add_argument(
        "--max-ponymail-months",
        type=int,
        default=None,
        help="Cap the number of months fetched per mailing list this run (testing / smoke runs)",
    )
    run_parser.add_argument(
        "--max-github-profiles",
        type=int,
        default=None,
        help=(
            "Cap the number of new GitHub profiles fetched this run (API-budget control; "
            "default: pipeline.DEFAULT_MAX_GITHUB_PROFILES_PER_RUN)"
        ),
    )
    run_parser.add_argument(
        "--max-github-commit-author-pages",
        type=int,
        default=None,
        help="Cap the number of GraphQL commit-history pages fetched this run (API-budget control)",
    )
    run_parser.add_argument(
        "--max-prs-per-repo",
        type=int,
        default=None,
        help=(
            "Cap the number of PR nodes fetched per GitHub repo this run (testing / smoke "
            "runs). Production runs leave this unset -- GitHubCollector's own rate_limit_floor "
            "is the real per-run API budget (collectors/github.py)."
        ),
    )
    run_parser.add_argument(
        "--trigger",
        default="manual",
        help="Recorded verbatim in the run manifest's `trigger` field (default: manual)",
    )
    run_parser.add_argument(
        "--identity-overrides",
        default=None,
        help="Path to an identity_overrides.yaml file (default: none applied)",
    )
    run_parser.add_argument(
        "--governance-since",
        default=None,
        help=(
            "Bound the governance compliance engine's first commit walk (issue #36) to commits "
            "on/after this date (a `git log --since=` string, e.g. '2025-01-01'). Only matters "
            "before a `governance_git` watermark exists -- every later run walks forward from "
            "that watermark regardless of this flag. Useful to keep an initial backfill's "
            "JIRA/GitHub evidence backlog a manageable size; the default (unset) walks full "
            "history, which the per-run API budget (`projects/<id>.yaml`'s `governance:` block) "
            "may then take several nightly runs to catch up on."
        ),
    )

    label_parser = subparsers.add_parser(
        "label",
        help="Serve the local, 127.0.0.1-only labeling page for the Phase 2a Jev pilot "
        "(DECISIONS.md D18, D22)",
    )
    label_parser.add_argument(
        "--corpus",
        required=True,
        help="Path to the pilot corpus JSONL (private benchmark checkout)",
    )
    label_parser.add_argument(
        "--labels",
        required=True,
        help="Path to this rater's append-only label JSONL (created if it doesn't exist)",
    )
    label_parser.add_argument("--rater", required=True, help="Rater name recorded on every label")
    label_parser.add_argument(
        "--port", type=int, default=8765, help="Port to bind on 127.0.0.1 (default: 8765)"
    )
    label_parser.add_argument(
        "--no-browser", action="store_true", help="Don't open a browser tab automatically"
    )
    label_parser.add_argument(
        "--label-set",
        choices=["gap", "full"],
        default="full",
        help=(
            "'gap' (issue #90; DECISIONS.md D23) presents the six gap labels (no public "
            "benchmark coverage) as the main form with verbatim definitions, plus a compact "
            "quick-check row for the six labels the public benchmark already covers; 'full' "
            "(default) is the original flat 12-label form. Both accept exactly the labels "
            "they present -- see label/label_set.py's LabelSet.presented."
        ),
    )

    pilot_parser = subparsers.add_parser(
        "pilot-sample",
        help=(
            "Build the Phase 2a Jev pilot corpus v0 (issue #44; DECISIONS.md D18): a "
            "150-message prevalence stratum + 100-message rare-label enrichment stratum "
            "from dev@ + JIRA comments, 2017-2026. Writes a JSONL corpus + manifest to "
            "--corpus-out/--manifest-out (point these at a private repo clone -- this "
            "command has no opinion about where they live) and prints the public "
            "manifest markdown (docs/pilot/corpus-v0-manifest.md's content) to stdout."
        ),
    )
    pilot_parser.add_argument("--project", required=True, help="Path to a projects/<id>.yaml file")
    pilot_parser.add_argument(
        "--data-dir",
        required=True,
        help="Root of the raw/snapshots/manifests/state data layout (dev@ Phase 1 metadata cache)",
    )
    pilot_parser.add_argument(
        "--seed", type=int, required=True, help="Sampler seed (deterministic, reproducible)"
    )
    pilot_parser.add_argument(
        "--corpus-out",
        required=True,
        help="Output path for the corpus v0 JSONL (private repo clone)",
    )
    pilot_parser.add_argument(
        "--manifest-out",
        required=True,
        help="Output path for the full run manifest JSON (private repo clone)",
    )
    pilot_parser.add_argument(
        "--public-manifest-out",
        default=None,
        help="If set, also write the public manifest markdown here (docs/pilot/corpus-v0-manifest)",
    )
    pilot_parser.add_argument(
        "--enrichment-filters",
        default=None,
        help="Path to the enrichment pre-filter YAML (default: classify/enrichment_filters_v2)",
    )
    pilot_parser.add_argument(
        "--jira-total-issue-target", type=int, default=DEFAULT_JIRA_TOTAL_ISSUE_TARGET
    )
    pilot_parser.add_argument(
        "--jira-min-issues-per-year", type=int, default=DEFAULT_JIRA_MIN_ISSUES_PER_YEAR
    )
    pilot_parser.add_argument("--jira-block-size", type=int, default=DEFAULT_JIRA_BLOCK_SIZE)
    pilot_parser.add_argument("--jira-max-calls", type=int, default=DEFAULT_JIRA_MAX_CALLS)

    subsample_parser = subparsers.add_parser(
        "pilot-subsample",
        help=(
            "Subset the Phase 2a pilot corpus v0 into a smaller, gap-focused corpus v1 "
            "(issue #90; DECISIONS.md D23): a seeded, stratified subset that keeps v0's "
            "prevalence/enrichment proportions while reserving enough enrichment items for "
            "the rare gap labels. Nothing is re-fetched -- this only subsets an existing v0 "
            "corpus JSONL, writing pilot.jsonl + manifest.json (counts only) to --out, "
            "mirroring corpus/v0's own layout."
        ),
    )
    subsample_parser.add_argument(
        "--corpus", required=True, help="Path to the corpus v0 JSONL (private repo clone)"
    )
    subsample_parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SUBSAMPLE_SIZE,
        help=f"Target v1 size (default: {DEFAULT_SUBSAMPLE_SIZE})",
    )
    subsample_parser.add_argument(
        "--out",
        required=True,
        help="Output directory for pilot.jsonl + manifest.json (private repo clone)",
    )
    subsample_parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SUBSAMPLE_SEED,
        help=f"Subsampler seed, deterministic and reproducible (default: {DEFAULT_SUBSAMPLE_SEED})",
    )
    subsample_parser.add_argument(
        "--enrichment-filters",
        default=None,
        help="Path to the enrichment pre-filter YAML (default: classify/enrichment_filters_v2)",
    )

    classify_parser = subparsers.add_parser(
        "pilot-classify",
        help=(
            "Run the pinned Jev classifier over every item in the pilot corpus (issue #47; "
            "DECISIONS.md D17, D18, D22) and write classification records + a cost/latency "
            "summary to --out. Re-running is free: --out's input-hash cache is reused, so an "
            "already-classified message is never re-sent."
        ),
    )
    classify_parser.add_argument(
        "--corpus",
        required=True,
        help="Path to the pilot corpus JSONL (private benchmark checkout)",
    )
    classify_parser.add_argument(
        "--out",
        required=True,
        help="Output directory for records/cache/summary (private benchmark checkout)",
    )
    classify_parser.add_argument(
        "--concurrency", type=int, default=4, help="Max concurrent system_one calls (default: 4)"
    )
    classify_parser.add_argument(
        "--monthly-cap-usd",
        type=float,
        default=DEFAULT_MONTHLY_CAP_USD,
        help=(
            f"D10 cost cap for this run (default: {DEFAULT_MONTHLY_CAP_USD}, i.e. "
            "effectively unbounded)"
        ),
    )
    classify_parser.add_argument(
        "--classifier-version", default="1.0.0", help="classifier_version recorded on every record"
    )
    classify_parser.add_argument(
        "--dotenv",
        default=None,
        help="If set, load TYPESAFE_API_KEY from this .env file's jev_key= entry before running "
        "(pilot/local use only -- see classify.classifier.load_jev_key_from_dotenv)",
    )

    evaluate_parser = subparsers.add_parser(
        "pilot-evaluate",
        help=(
            "Evaluate Jev against one or more raters' labels for the pilot corpus (issue #47; "
            "COMMUNITY-HEALTH.md §6): threshold-swept precision/recall/F1 with bootstrap 95%% "
            "CIs, reliability diagram, prevalence-stratum-only prevalence estimates, tone "
            "agreement, rater time, and (2+ raters) Krippendorff's alpha. Writes a per-item "
            "private detail report to --private-out and an aggregate-only public report to "
            "--public-out."
        ),
    )
    evaluate_parser.add_argument(
        "--corpus",
        required=True,
        help="Path to the pilot corpus JSONL (private benchmark checkout)",
    )
    evaluate_parser.add_argument(
        "--results",
        required=True,
        help="pilot-classify's --out directory (or its records JSONL directly)",
    )
    evaluate_parser.add_argument(
        "--labels",
        required=True,
        action="append",
        help="Path to one rater's label JSONL; repeat --labels for additional raters",
    )
    evaluate_parser.add_argument(
        "--private-out",
        required=True,
        help="Output path for the private detail report markdown",
    )
    evaluate_parser.add_argument(
        "--public-out",
        required=True,
        help="Output path for the public aggregate-only report markdown",
    )
    evaluate_parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="Bootstrap RNG seed"
    )
    evaluate_parser.add_argument(
        "--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS
    )

    benchmark_public_parser = subparsers.add_parser(
        "benchmark-public",
        help=(
            "Run the pinned Jev classifier against the D23 public human-labeled dataset "
            "shortlist (issue #89): download + verify each dataset's pinned files, draw a "
            "seeded stratified sample, classify with the cached/cost-capped Jev client, "
            "and render an aggregate-only public report to --public-out."
        ),
    )
    benchmark_public_parser.add_argument(
        "--cache-dir",
        required=True,
        help="Directory for downloaded dataset files, the Jev cache, and the run manifest "
        "(must be outside this repo checkout -- D23: never commit dataset text)",
    )
    benchmark_public_parser.add_argument(
        "--public-out",
        required=True,
        help="Output path for the public aggregate-only report markdown "
        "(e.g. docs/benchmark/public-v1.md)",
    )
    benchmark_public_parser.add_argument(
        "--registry",
        default=None,
        help="Path to the dataset registry YAML (default: benchmark_public/datasets_v1.yaml)",
    )
    benchmark_public_parser.add_argument(
        "--mapping",
        default=None,
        help="Path to the label mapping YAML (default: benchmark_public/label_mapping_v1.yaml)",
    )
    benchmark_public_parser.add_argument(
        "--concurrency", type=int, default=4, help="Max concurrent system_one calls (default: 4)"
    )
    benchmark_public_parser.add_argument(
        "--monthly-cap-usd",
        type=float,
        default=BENCHMARK_PUBLIC_DEFAULT_CAP,
        help=(
            f"Cost cap for this run (default: {BENCHMARK_PUBLIC_DEFAULT_CAP}; issue #89's $10 cap)"
        ),
    )
    benchmark_public_parser.add_argument(
        "--classifier-version", default="1.0.0", help="classifier_version recorded on every record"
    )
    benchmark_public_parser.add_argument(
        "--seed", type=int, default=BENCHMARK_PUBLIC_DEFAULT_SEED, help="Bootstrap RNG base seed"
    )
    benchmark_public_parser.add_argument(
        "--dotenv",
        default=None,
        help="If set, load TYPESAFE_API_KEY from this .env file's jev_key= entry before running",
    )

    verify_policy_sources_parser = subparsers.add_parser(
        "verify-policy-sources",
        help=(
            "D24 (issue #93): re-fetch every scored governance-policy.yaml rule/exemption/"
            "sub_pattern's source_url and confirm its source_quote still appears there. "
            "Exits non-zero if any source fails to fetch or no longer contains its quote."
        ),
    )
    verify_policy_sources_parser.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY_PATH),
        help="Path to governance-policy.yaml (default: the repo-root policy file)",
    )

    peers_collect_parser = subparsers.add_parser(
        "peers-collect",
        help=(
            "Collect + compute the issue #145 peer-context metrics (DECISIONS.md D30): "
            "GitHub PR/review/comment + git-commit + GA-release data for every configured "
            "peer repo, plus the same five metrics recomputed for Cassandra itself, "
            "written to the data dir's raw/peers/<id>/... and snapshots/peers/<run_id>/..."
        ),
    )
    peers_collect_parser.add_argument(
        "--peers", default="projects/peers.yaml", help="Path to a projects/peers.yaml file"
    )
    peers_collect_parser.add_argument(
        "--data-dir", required=True, help="Root of the raw/snapshots/manifests data layout"
    )
    peers_collect_parser.add_argument(
        "--workdir", required=True, help="Scratch directory for each peer's disk-safe git clone"
    )
    peers_collect_parser.add_argument(
        "--run-id", required=True, help="Identifier for this run (used in partition filenames)"
    )
    peers_collect_parser.add_argument(
        "--min-free-disk-gb",
        type=float,
        default=None,
        help=(
            "Stop collecting any further peer's git/release data once free space on / drops "
            "at or below this many GB (issue #145's disk-budget rule for a constrained "
            "collection machine); unset disables the check"
        ),
    )

    private_run_parser = subparsers.add_parser(
        "private-run",
        help=(
            "Owner-only private run over Cassandra history (issue #110; DECISIONS.md D1, "
            "D10, D17, D18, D22, D23): stratified thread sampling (venue x quarter, "
            "seeded, weighted), full-thread classification with the pinned, cost-capped "
            "Jev classifier, and a private, aggregate-only report.md/aggregates.json "
            "(no message text, no names, no message ids). Nothing produced here is "
            "published to the site, the public repo, or the data branch."
        ),
    )
    private_run_parser.add_argument(
        "--project", required=True, help="Path to a projects/<id>.yaml file"
    )
    private_run_parser.add_argument(
        "--data-dir",
        required=True,
        help="Root of the raw/snapshots/manifests/state data layout (a data-branch checkout)",
    )
    private_run_parser.add_argument(
        "--out",
        default=str(Path.home() / "project-health-private"),
        help="Output directory for report.md/aggregates.json/Jev cache -- must be outside "
        "this repo checkout (default: ~/project-health-private/)",
    )
    private_run_parser.add_argument(
        "--dotenv",
        default=None,
        help="If set, load TYPESAFE_API_KEY from this .env file's jev_key= entry before running",
    )
    private_run_parser.add_argument(
        "--sample-only",
        action="store_true",
        help="Build and write the stratified sample manifest only -- no fetch, no classify",
    )
    private_run_parser.add_argument(
        "--no-classify",
        action="store_true",
        help="Fetch and aggregate from the existing --out cache only -- zero Jev calls, no "
        "API key needed. Useful to re-render report.md/aggregates.json (e.g. after a run "
        "paused on the D10 cost cap or a TypeSafe 402/no-credits response) without risking "
        "a real API call.",
    )
    private_run_parser.add_argument(
        "--quarters",
        default=None,
        help="Comma-separated YYYYQN list (e.g. 2024Q1,2025Q1) for a scoped/smoke run; "
        "default: the full 2012Q1-2026Q3 range",
    )
    private_run_parser.add_argument(
        "--concurrency", type=int, default=4, help="Max concurrent system_one calls (default: 4)"
    )
    private_run_parser.add_argument(
        "--monthly-cap-usd",
        type=float,
        default=PRIVATE_RUN_DEFAULT_CAP,
        help=f"D10 cost cap for this run (default: {PRIVATE_RUN_DEFAULT_CAP})",
    )
    private_run_parser.add_argument(
        "--seed",
        type=int,
        default=PRIVATE_RUN_DEFAULT_SEED,
        help=f"Sampler seed, deterministic and recorded (default: {PRIVATE_RUN_DEFAULT_SEED})",
    )
    private_run_parser.add_argument(
        "--k",
        type=int,
        default=PRIVATE_RUN_DEFAULT_K,
        help=f"Threads sampled per (venue, quarter) stratum (default: {PRIVATE_RUN_DEFAULT_K})",
    )
    private_run_parser.add_argument(
        "--classifier-version", default="1.0.0", help="classifier_version recorded on every record"
    )
    private_run_parser.add_argument(
        "--newcomer-threshold",
        type=int,
        default=DEFAULT_NEWCOMER_N,
        help=(
            "Issue #114: fewer than this many prior messages in a venue (across the whole "
            f"Phase-1 metadata, not just the sample) makes an author a newcomer at message "
            f"time (default: {DEFAULT_NEWCOMER_N})"
        ),
    )

    publish_conv_parser = subparsers.add_parser(
        "publish-conversation-aggregates",
        help=(
            "D26 (issue #118): sanitize one private-run aggregates.json (issue #110/#114) "
            "through an allowlist and publish it to "
            "snapshots/conversation_patterns/<run date>.json on a data-branch checkout -- "
            "the one bridge from the owner-only private run to the public site. Refuses to "
            "publish (nonzero exit, nothing written) if anything looks like it could "
            "identify a message or a person, or if a number is published below its "
            "COMMUNITY-HEALTH.md §5.1/§5.2 floor."
        ),
    )
    publish_conv_parser.add_argument(
        "--from",
        dest="from_path",
        required=True,
        help="Path to the private run's aggregates.json (private_run/runner.py output)",
    )
    publish_conv_parser.add_argument(
        "--data-dir",
        required=True,
        help="Root of a data-branch checkout to write "
        "snapshots/conversation_patterns/<run date>.json into",
    )
    publish_conv_parser.add_argument(
        "--run-date",
        default=None,
        help="Override the snapshot filename's date (YYYY-MM-DD); default: the date "
        "portion of the aggregates' own generated_at",
    )
    publish_conv_parser.add_argument(
        "--threads",
        dest="threads_path",
        default=None,
        help="Issue #122 (D27): path to the private run's threads.jsonl (private_run/"
        "runner.py output) -- if given, also sanitizes and publishes "
        "snapshots/conversation_patterns/threads-<run date>.json (thread-level rows, "
        "linked to the public archive -- dev@ threads to their Pony Mail permalink, "
        "JIRA threads to issues.apache.org/jira/browse/<KEY>)",
    )

    return parser


def _cmd_label(args: argparse.Namespace) -> int:
    rater = args.rater.strip()
    if not rater:
        print("error: --rater must not be empty", file=sys.stderr)
        return 2

    repo_root = find_public_repo_root()
    try:
        corpus_path = assert_outside_repo(args.corpus, repo_root, label="corpus")
        labels_path = assert_outside_repo(args.labels, repo_root, label="labels")
    except UnsafePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        httpd = make_server(
            corpus_path=corpus_path,
            labels_path=labels_path,
            rater=rater,
            port=args.port,
            label_set_mode=args.label_set,
        )
    except (
        CorpusError,
        LabelSetError,
        QuestionSetReadError,
        LabelQuestionMismatchError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    host, bound_port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{bound_port}/"
    app = httpd.RequestHandlerClass.app  # type: ignore[attr-defined]
    print(f"Serving the labeling page at {url}")
    print(f"Rater: {rater}  Items: {len(app.items)}  Labeled so far: {len(app.records)}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_project(args.project)
    sources = args.sources.split(",") if args.sources else None
    result = run_pipeline(
        config=config,
        data_dir=args.data_dir,
        workdir=args.workdir,
        sources=sources,
        site_out=args.site_out,
        max_jira_issues=args.max_jira_issues,
        max_ponymail_months=args.max_ponymail_months,
        max_github_profiles=args.max_github_profiles,
        max_github_commit_author_pages=args.max_github_commit_author_pages,
        max_prs_per_repo=args.max_prs_per_repo,
        trigger=args.trigger,
        identity_overrides_path=args.identity_overrides,
        governance_since=args.governance_since,
    )
    return result.exit_code


def _cmd_peers_collect(args: argparse.Namespace) -> int:
    peers_config = load_peers(args.peers)
    min_free_disk_bytes = (
        int(args.min_free_disk_gb * 1024**3) if args.min_free_disk_gb is not None else None
    )
    report = run_peers_collection(
        peers_config,
        data_dir=args.data_dir,
        workdir=args.workdir,
        run_id=args.run_id,
        min_free_disk_bytes=min_free_disk_bytes,
    )
    print(json.dumps(report.to_json_dict(), indent=2, sort_keys=True))
    return 0


def _cmd_pilot_sample(args: argparse.Namespace) -> int:
    config = load_project(args.project)
    mailing_lists = config.mailing_lists
    issue_tracker = config.issue_tracker
    if mailing_lists is None or issue_tracker is None:
        print(
            "pilot-sample requires mailing_lists and issue_tracker in the project config",
            file=sys.stderr,
        )
        return 2

    enrichment_filters = args.enrichment_filters or str(
        Path(__file__).resolve().parent / "classify" / "enrichment_filters_v2.yaml"
    )

    result = run_pilot_sample(
        data_dir=args.data_dir,
        seed=args.seed,
        domain=mailing_lists.domain,
        list_name="dev",
        jira_base_url=issue_tracker.base_url,
        jira_project_key=issue_tracker.project_key,
        automated_sender_patterns=config.automated_senders,
        enrichment_filters_path=enrichment_filters,
        corpus_output_path=args.corpus_out,
        manifest_output_path=args.manifest_out,
        jira_total_issue_target=args.jira_total_issue_target,
        jira_min_issues_per_year=args.jira_min_issues_per_year,
        jira_block_size=args.jira_block_size,
        jira_max_calls=args.jira_max_calls,
    )

    if args.public_manifest_out:
        public_path = Path(args.public_manifest_out)
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text(result.public_manifest_markdown, encoding="utf-8")
    else:
        print(result.public_manifest_markdown)

    print(
        f"corpus v0: {len(result.items)} items written to {result.corpus_path} "
        f"(checksum {result.manifest['corpus_checksum_sha256']})",
        file=sys.stderr,
    )
    return 0


def _cmd_pilot_subsample(args: argparse.Namespace) -> int:
    repo_root = find_public_repo_root()
    try:
        corpus_path = assert_outside_repo(args.corpus, repo_root, label="corpus")
        out_path = assert_outside_repo(args.out, repo_root, label="out")
    except UnsafePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(out_path)
    corpus_output = out_dir / "pilot.jsonl"
    manifest_output = out_dir / "manifest.json"

    try:
        result = run_pilot_subsample(
            source_corpus_path=corpus_path,
            seed=args.seed,
            size=args.size,
            corpus_output_path=corpus_output,
            manifest_output_path=manifest_output,
            enrichment_filters_path=args.enrichment_filters,
        )
    except (CorpusError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"corpus v1: {len(result.row_ids)} items written to {result.corpus_path} "
        f"(checksum {result.manifest['corpus_checksum_sha256']})",
        file=sys.stderr,
    )
    print(f"manifest written to {result.manifest_path}", file=sys.stderr)
    return 0


def _cmd_pilot_classify(args: argparse.Namespace) -> int:
    if args.dotenv:
        load_jev_key_from_dotenv(args.dotenv)

    repo_root = find_public_repo_root()
    try:
        corpus_path = assert_outside_repo(args.corpus, repo_root, label="corpus")
        out_path = assert_outside_repo(args.out, repo_root, label="out")
    except UnsafePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = run_pilot_classify(
        corpus_path=corpus_path,
        out_dir=out_path,
        concurrency=args.concurrency,
        classifier_version=args.classifier_version,
        monthly_cap_usd=args.monthly_cap_usd,
    )

    print(
        f"pilot-classify: {result.summary['records_written']} records written to "
        f"{result.records_path} (status={result.summary['status']}, "
        f"calls_made={result.summary['calls_made']}, cache_hits={result.summary['cache_hits']}, "
        f"estimated_cost_usd={result.summary['estimated_cost_usd']:.6f})",
        file=sys.stderr,
    )
    print(f"summary written to {result.summary_path}", file=sys.stderr)
    return 0


def _cmd_pilot_evaluate(args: argparse.Namespace) -> int:
    repo_root = find_public_repo_root()
    try:
        corpus_path = assert_outside_repo(args.corpus, repo_root, label="corpus")
        results_path = assert_outside_repo(args.results, repo_root, label="results")
        label_paths = [assert_outside_repo(path, repo_root, label="labels") for path in args.labels]
        private_out_path = assert_outside_repo(args.private_out, repo_root, label="private-out")
    except UnsafePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = evaluate_pilot(
        corpus_path=corpus_path,
        results_dir=results_path,
        label_paths=label_paths,
        seed=args.seed,
        bootstrap_iterations=args.bootstrap_iterations,
    )

    private_markdown = render_private_report_markdown(result)
    private_out_path.parent.mkdir(parents=True, exist_ok=True)
    private_out_path.write_text(private_markdown, encoding="utf-8")

    public_markdown = render_public_report_markdown(result)
    public_out_path = Path(args.public_out)
    public_out_path.parent.mkdir(parents=True, exist_ok=True)
    public_out_path.write_text(public_markdown, encoding="utf-8")

    print(f"private report written to {private_out_path}", file=sys.stderr)
    print(f"public report written to {public_out_path}", file=sys.stderr)
    return 0


def _cmd_benchmark_public(args: argparse.Namespace) -> int:
    if args.dotenv:
        load_jev_key_from_dotenv(args.dotenv)

    repo_root = find_public_repo_root()
    try:
        cache_dir = assert_outside_repo(args.cache_dir, repo_root, label="cache-dir")
    except UnsafePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    run = run_benchmark(
        cache_dir=cache_dir,
        registry_path=args.registry,
        mapping_path=args.mapping,
        concurrency=args.concurrency,
        classifier_version=args.classifier_version,
        monthly_cap_usd=args.monthly_cap_usd,
    )

    registry = load_registry(args.registry)
    mapping_set = load_label_mapping(args.mapping)
    evaluations = evaluate_benchmark(run, mapping_set, seed=args.seed)
    category_separations = compute_all_category_separations(run, registry)

    public_markdown = benchmark_public_report.render_public_report_markdown(
        registry, evaluations, run.manifest, category_separations=category_separations
    )
    public_out_path = Path(args.public_out)
    public_out_path.parent.mkdir(parents=True, exist_ok=True)
    public_out_path.write_text(public_markdown, encoding="utf-8")

    print(
        f"benchmark-public: status={run.run_result.status}, "
        f"calls_made={run.run_result.calls_made}, cache_hits={run.run_result.cache_hits}, "
        f"estimated_cost_usd={run.run_result.estimated_cost_usd:.6f}",
        file=sys.stderr,
    )
    print(f"public report written to {public_out_path}", file=sys.stderr)
    return 0


def _cmd_private_run(args: argparse.Namespace) -> int:
    if args.dotenv:
        load_jev_key_from_dotenv(args.dotenv)

    repo_root = find_public_repo_root()
    try:
        out_path = assert_outside_repo(args.out, repo_root, label="out")
    except UnsafePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    config = load_project(args.project)
    if config.mailing_lists is None or config.issue_tracker is None:
        print(
            "private-run requires mailing_lists and issue_tracker in the project config",
            file=sys.stderr,
        )
        return 2

    quarters = parse_quarters_arg(args.quarters)

    result = run_private_run(
        project_config=config,
        data_dir=args.data_dir,
        out_dir=out_path,
        quarters=quarters,
        seed=args.seed,
        k=args.k,
        concurrency=args.concurrency,
        monthly_cap_usd=args.monthly_cap_usd,
        classifier_version=args.classifier_version,
        sample_only=args.sample_only,
        no_classify=args.no_classify,
        newcomer_n=args.newcomer_threshold,
    )

    if args.sample_only:
        print(
            f"private-run --sample-only: sample manifest written to {result.sample_manifest_path}",
            file=sys.stderr,
        )
        return 0

    run_result = result.run_result
    print(
        f"private-run: status={run_result.status if run_result else None}, "
        f"calls_made={run_result.calls_made if run_result else 0}, "
        f"cache_hits={run_result.cache_hits if run_result else 0}, "
        f"estimated_cost_usd={run_result.estimated_cost_usd if run_result else 0.0:.6f}",
        file=sys.stderr,
    )
    partial_run = result.aggregates.get("partial_run") if result.aggregates else None
    if partial_run:
        print(
            f"private-run: PARTIAL RUN -- {partial_run['messages_classified']} of "
            f"{partial_run['messages_sampled']} sampled messages classified "
            "(see report.md's 'PARTIAL RUN' section for strata coverage)",
            file=sys.stderr,
        )
    print(f"report written to {result.report_path}", file=sys.stderr)
    print(f"aggregates written to {result.aggregates_path}", file=sys.stderr)
    print(f"threads written to {result.threads_path}", file=sys.stderr)
    return 0


def _cmd_publish_conversation_aggregates(args: argparse.Namespace) -> int:
    from_path = Path(args.from_path)
    if not from_path.is_file():
        print(f"error: --from {from_path} not found", file=sys.stderr)
        return 2

    aggregates = json.loads(from_path.read_text(encoding="utf-8"))
    run_date = date.fromisoformat(args.run_date) if args.run_date else None

    try:
        out_path = write_conversation_patterns_snapshot(
            aggregates, args.data_dir, run_date=run_date
        )
    except SanitizeError as exc:
        print(f"error: refusing to publish -- {exc}", file=sys.stderr)
        return 2

    print(f"publish-conversation-aggregates: wrote {out_path}", file=sys.stderr)

    if args.threads_path:
        threads_source = Path(args.threads_path)
        if not threads_source.is_file():
            print(f"error: --threads {threads_source} not found", file=sys.stderr)
            return 2
        rows = [
            json.loads(line)
            for line in threads_source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        effective_run_date = run_date or date.fromisoformat(aggregates["generated_at"][:10])
        try:
            threads_out_path = write_threads_snapshot(
                rows, args.data_dir, run_date=effective_run_date
            )
        except SanitizeError as exc:
            print(f"error: refusing to publish threads -- {exc}", file=sys.stderr)
            return 2
        print(f"publish-conversation-aggregates: wrote {threads_out_path}", file=sys.stderr)

    return 0


def _cmd_verify_policy_sources(args: argparse.Namespace) -> int:
    results = verify_policy_sources(args.policy)
    failed = [r for r in results if not r.ok]
    for result in results:
        status = "OK" if result.ok else "MISS"
        print(
            f"[{status}] {result.item.path} <{result.item.source_url}> -- {result.detail}",
            file=sys.stderr,
        )
    print(
        f"verify-policy-sources: {len(results)} sourced item(s) checked, {len(failed)} failed",
        file=sys.stderr,
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "peers-collect":
        return _cmd_peers_collect(args)
    if args.command == "label":
        return _cmd_label(args)
    if args.command == "pilot-sample":
        return _cmd_pilot_sample(args)
    if args.command == "pilot-subsample":
        return _cmd_pilot_subsample(args)
    if args.command == "pilot-classify":
        return _cmd_pilot_classify(args)
    if args.command == "pilot-evaluate":
        return _cmd_pilot_evaluate(args)
    if args.command == "benchmark-public":
        return _cmd_benchmark_public(args)
    if args.command == "verify-policy-sources":
        return _cmd_verify_policy_sources(args)
    if args.command == "private-run":
        return _cmd_private_run(args)
    if args.command == "publish-conversation-aggregates":
        return _cmd_publish_conversation_aggregates(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
