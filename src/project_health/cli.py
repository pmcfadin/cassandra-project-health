"""`project-health` console script (ARCHITECTURE.md §5, §7.3, §11; issue #9).

Registered as a `console_scripts`-style entry point in `pyproject.toml`
(`[project.scripts] project-health = "project_health.cli:main"`). Usage:

    project-health run --project projects/cassandra.yaml \\
        --data-dir <path> --workdir <path> \\
        [--sources git,jira,ponymail] [--site-out <dir>]
"""

from __future__ import annotations

import argparse
import sys

from project_health.config import load_project
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

    return parser


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
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
