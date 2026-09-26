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
        max_prs_per_repo=args.max_prs_per_repo,
        trigger=args.trigger,
        identity_overrides_path=args.identity_overrides,
        governance_since=args.governance_since,
    )
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "label":
        return _cmd_label(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
