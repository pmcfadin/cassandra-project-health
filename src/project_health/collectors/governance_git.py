"""Governance-scoped git walk (issue #36).

Distinct from `collectors/git.py`'s `GitCollector` in three ways the
governance engine needs and the M0 collector deliberately doesn't provide
(docs/spec/GOVERNANCE.md §3):

1. **Every commit, including merges.** `GitCollector` always runs
   `--no-merges` (ARCHITECTURE.md §3.1's "merge-commit trap" — ordinary merge
   commits carry no trailer of their own and would distort *metric*
   denominators). But a live sample found real forward-merge commits (`git
   merge -s ours` + `git commit --amend`) that squash a genuine `patch by
   X; reviewed by Y for CASSANDRA-N` trailer into a two-parent commit —
   filtering those out would wrongly show a named, evidenced commit as
   `unknown`. This module walks every commit; `is_merge` (parent count >= 2)
   is carried on each row so *aggregate* rate calculations
   (`governance/metrics.py`) can still exclude merges from their
   denominators without ever hiding a real per-commit result.
2. **Committer, not just author**, and the full changed-path list — neither
   is in `contribution_event` (ARCHITECTURE.md §3's fact-table shape doesn't
   need them for the M0 metrics), but `commit_compliance` does.
3. **Every branch commit_compliance cares about**, not just one
   `default_branch` — trunk plus the active `cassandra-N` release branches
   (`governance-policy.yaml` `branches_in_scope`), each walked so a commit is
   attributed to the *oldest* branch tip that reaches it (see
   `collect_multi_branch`), matching docs/spec/GOVERNANCE.md §3: "score each
   landing on each branch as its own commit."

This module never writes to the `raw/` partition layout
(`project_health.storage`) or validates against an ARCHITECTURE.md §3 fact
table — its output (`CommitRecord`) feeds `governance/checks.py`'s
`CommitFacts` directly, an intermediate shape private to the governance
engine, not one of the shared normalized tables.

Changed-path collection (`git show --name-only <sha>`, per every
`scored: false` rule's `check_method` in `governance-policy.yaml`) is done
with one bulk `git log --no-merges --name-only` call across the whole walked
range rather than one `git show` subprocess per commit — same output for
every non-merge commit (a single-parent diff), far cheaper for a range of
thousands of commits. Merge commits get `changed_paths=None` (git's default
`--name-only` skips diff generation for merges entirely); this only affects
the three `scored: false` display facts, never a scored check.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from project_health.collectors.reviewer_trailer import ReviewerExtractor, extract_issue_keys

_US = "\x1f"  # unit separator — field boundary within one commit's header
_STX = "\x02"  # start-of-record marker, precedes each commit's header
_ETX = "\x03"  # end-of-header marker; anything after it (up to the next STX)
# is that commit's `--name-only` changed-path lines, when present.

_MAIN_FORMAT = f"{_STX}%H{_US}%an{_US}%ae{_US}%cn{_US}%ce{_US}%cI{_US}%P{_US}%B{_ETX}"
_PATHS_FORMAT = f"{_STX}%H{_ETX}"


@dataclass(frozen=True)
class CommitRecord:
    """One commit, as walked for the governance engine — evidence-agnostic,
    matches `governance/checks.py`'s `CommitFacts` field-for-field plus the
    raw `author_email`/`committer_email` this module additionally carries
    (not needed for scoring, but useful for identity cross-referencing)."""

    sha: str
    branch: str
    commit_date: datetime  # committer date (%cI), UTC-aware
    message: str
    author: str
    author_email: str
    committer: str
    committer_email: str
    is_merge: bool
    trailer_reviewers: tuple[str, ...]
    issue_keys: tuple[str, ...]
    changed_paths: tuple[str, ...] | None


def _run_git(repo_path: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def resolve_ref(repo_path: Path, branch: str) -> str:
    """Resolve `branch` to a ref `git log` can walk — tries the branch name
    directly (a bare clone's local branch refs), then `origin/<branch>`
    (a non-bare clone's remote-tracking ref)."""
    for candidate in (branch, f"origin/{branch}"):
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", candidate],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return candidate
    raise ValueError(f"could not resolve branch {branch!r} (nor origin/{branch!r}) in {repo_path}")


def resolve_sha(repo_path: str | Path, branch: str) -> str:
    """The current commit SHA `branch` resolves to — used by
    `pipeline.py`'s `_collect_governance` to compute this run's watermark
    value the same way `collectors/git.py`'s `GitCollector` does
    (`git rev-parse <ref>`), regardless of whether any new commits were
    actually walked this run."""
    repo_path = Path(repo_path)
    ref = resolve_ref(repo_path, branch)
    return _run_git(repo_path, ["rev-parse", ref]).strip()


def _parse_git_date(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _iter_changed_paths(repo_path: Path, range_args: list[str]) -> dict[str, tuple[str, ...]]:
    """`sha -> changed file paths`, non-merge commits only, one bulk
    `git log --no-merges --name-only` call (see module docstring).

    `range_args` is a list of separate `git log` positional/option
    arguments (e.g. `["trunk", "^cassandra-6.0"]`) — never a single
    space-joined string, since `subprocess.run` with a list doesn't do
    shell word-splitting and git needs each rev/exclusion as its own argv
    entry.
    """
    output = _run_git(
        repo_path,
        ["log", "--no-merges", "--name-only", f"--format={_PATHS_FORMAT}", *range_args],
    )
    paths_by_sha: dict[str, tuple[str, ...]] = {}
    for chunk in output.split(_STX):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        header, _, rest = chunk.partition(_ETX)
        sha = header.strip()
        if not sha:
            continue
        paths = tuple(line for line in rest.strip("\n").split("\n") if line.strip())
        paths_by_sha[sha] = paths
    return paths_by_sha


def _iter_raw_commits(repo_path: Path, range_args: list[str]) -> Iterator[dict]:
    output = _run_git(repo_path, ["log", f"--format={_MAIN_FORMAT}", *range_args])
    for chunk in output.split(_STX):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        header, _, _rest = chunk.partition(_ETX)
        sha, author, author_email, committer, committer_email, date_iso, parents, message = (
            header.split(_US, 7)
        )
        yield {
            "sha": sha,
            "author": author,
            "author_email": author_email,
            "committer": committer,
            "committer_email": committer_email,
            "commit_date": _parse_git_date(date_iso),
            "parent_count": len(parents.split()) if parents.strip() else 0,
            "message": message.strip("\n"),
        }


def collect_commits(
    repo_path: str | Path,
    branch: str,
    *,
    branch_label: str | None = None,
    since: str | None = None,
    since_sha: str | None = None,
    extractor: ReviewerExtractor | None = None,
    exclude_refs: Sequence[str] = (),
) -> list[CommitRecord]:
    """Walk every commit on `branch` (merges included), evidence-agnostic.

    `branch_label` is what's stored in `CommitRecord.branch` — defaults to
    `branch` itself, but `collect_multi_branch` passes the *nominal* branch
    name even when `branch` resolved to `origin/<branch>`. `since`, if given,
    is a `git log --since=<since>` date string (e.g. `"2023-01-01"`).
    `since_sha`, if given, is a prior run's watermark (a commit SHA) — the
    walk becomes `git log <since_sha>..<ref>` (exclusive of `since_sha`),
    the same incremental-range convention `collectors/git.py`'s own
    `GitCollector` uses, so a governance pipeline run only re-walks commits
    it hasn't already persisted (issue #36 fixup cycle 1: this walk itself
    is cheap/local/unbounded, but persisting its output lets every other
    per-run step — scoring, JIRA/GitHub evidence fetch eligibility — read
    a stable, already-collected commit set instead of re-deriving it).
    `exclude_refs`, if given, are passed as `git log <branch> ^<ref>...` —
    this is what makes `collect_multi_branch` attribute a shared-ancestor
    commit to only the oldest branch tip that reaches it, never once per
    branch. `since_sha` and `exclude_refs` are independent and may combine,
    though the pipeline's single-branch incremental walk only ever uses
    `since_sha`.
    """
    repo_path = Path(repo_path)
    ref = resolve_ref(repo_path, branch)
    extractor = extractor or ReviewerExtractor()

    primary_rev = f"{since_sha}..{ref}" if since_sha else ref
    range_args = [primary_rev, *(f"^{excluded}" for excluded in exclude_refs)]
    log_args = list(range_args)
    if since:
        log_args = [f"--since={since}", *log_args]

    changed_paths_by_sha = _iter_changed_paths(repo_path, log_args)

    records = []
    for raw in _iter_raw_commits(repo_path, log_args):
        attribution = extractor.extract(raw["message"])
        trailer_reviewers = attribution.reviewers if attribution else ()
        records.append(
            CommitRecord(
                sha=raw["sha"],
                branch=branch_label or branch,
                commit_date=raw["commit_date"],
                message=raw["message"],
                author=raw["author"],
                author_email=raw["author_email"],
                committer=raw["committer"],
                committer_email=raw["committer_email"],
                is_merge=raw["parent_count"] >= 2,
                trailer_reviewers=trailer_reviewers,
                issue_keys=extract_issue_keys(raw["message"]),
                changed_paths=changed_paths_by_sha.get(raw["sha"]),
            )
        )
    return records


def collect_multi_branch(
    repo_path: str | Path,
    branches: Sequence[str],
    *,
    since: str | None = None,
    extractor: ReviewerExtractor | None = None,
) -> list[CommitRecord]:
    """Walk every branch in `branches` (oldest first), attributing each
    commit to the *first* (oldest) branch in the list whose tip reaches it —
    never duplicating a shared-ancestor commit once per branch that can
    still see it (docs/spec/GOVERNANCE.md §3: "score each landing on each
    branch as its own commit", which only makes sense for commits that
    genuinely differ per branch, not one shared history walked N times).

    `branches` order matters: pass oldest-to-newest (e.g. `["cassandra-4.0",
    "cassandra-4.1", "cassandra-5.0", "cassandra-6.0", "trunk"]`) so a commit
    common to several of them lands on the oldest one, matching where it was
    actually first committed before being forward-merged onward.
    """
    repo_path = Path(repo_path)
    extractor = extractor or ReviewerExtractor()

    all_records: list[CommitRecord] = []
    resolved_so_far: list[str] = []
    for branch in branches:
        ref = resolve_ref(repo_path, branch)
        records = collect_commits(
            repo_path,
            branch,
            branch_label=branch,
            since=since,
            extractor=extractor,
            exclude_refs=tuple(resolved_so_far),
        )
        all_records.extend(records)
        resolved_so_far.append(ref)
    return all_records
