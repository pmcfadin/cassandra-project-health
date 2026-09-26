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
import sys
import webbrowser
from pathlib import Path

from project_health.classify.sample import (
    DEFAULT_JIRA_BLOCK_SIZE,
    DEFAULT_JIRA_MIN_ISSUES_PER_YEAR,
    DEFAULT_JIRA_TOTAL_ISSUE_TARGET,
    DEFAULT_JIRA_MAX_CALLS,
    run_pilot_sample,
)
from project_health.config import load_project
from project_health.label.label_set import LabelSetError
from project_health.label.question_set import QuestionSetReadError
from project_health.label.safety import UnsafePathError, assert_outside_repo, find_public_repo_root
from project_health.label.server import LabelQuestionMismatchError, make_server
from project_health.label.store import CorpusError
from project_health.pipeline import ALL_SOURCES, run_pipeline


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
            corpus_path=corpus_path, labels_path=labels_path, rater=rater, port=args.port
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
        trigger=args.trigger,
        identity_overrides_path=args.identity_overrides,
        governance_since=args.governance_since,
    )
    return result.exit_code


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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "label":
        return _cmd_label(args)
    if args.command == "pilot-sample":
        return _cmd_pilot_sample(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
