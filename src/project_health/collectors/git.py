"""Git ``SourceCollector`` (ARCHITECTURE.md §2.2, §3.1, §4.3).

Collects two normalized fact tables from a local clone of a project's git
repository:

- ``contribution_event`` — one row per non-merge commit (``event_type =
  'code_commit'``).
- ``review_event`` — one row per (reviewer, issue key) pair parsed from each
  commit's trailer by :mod:`project_health.collectors.reviewer_trailer`
  (``source = 'commit_trailer'``).

Merge commits are always excluded (``git log --no-merges``, ARCHITECTURE.md
§3.1's "merge-commit trap" — they carry no trailer of their own and distort
denominators). Bot authors are excluded via ``bot_patterns`` entries whose
``field == 'git_author_email'`` (``projects/<id>.yaml``).

Both fact tables are written with only *raw* identifiers
(``author_raw_type``/``author_raw_value``, ``reviewer_raw_type``/
``reviewer_raw_value``); ``*_identity_id`` is always ``null`` here —
identity resolution (#6) fills it in later by joining those raw columns
against ``identity_link`` (schema/README.md).

The watermark is the resolved branch ref's commit SHA (ARCHITECTURE.md
§4.3): ``collect`` walks ``watermark..ref`` (exclusive of `watermark`) when
a watermark is given, or the whole history reachable from `ref` on a first
run.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

from project_health.collectors.reviewer_trailer import (
    ReviewerExtractor,
    looks_like_reviewer_trailer,
)
from project_health.config import BotPattern
from project_health.schema import get_schema, validate

# Field separator (unit separator) / record separator (record separator) —
# control characters vanishingly unlikely to appear in real commit
# metadata, which lets a whole `git log` invocation be parsed with one
# split instead of one `git log` call per commit.
_UNIT_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_LOG_FORMAT = f"%H{_UNIT_SEP}%an{_UNIT_SEP}%ae{_UNIT_SEP}%aI{_UNIT_SEP}%B{_RECORD_SEP}"

_MAX_UNPARSED_EXAMPLES = 5


def github_clone_url(owner: str, name: str) -> str:
    """The GitHub HTTPS clone URL for `owner/name`."""
    return f"https://github.com/{owner}/{name}.git"


def _looks_like_git_dir(path: Path) -> bool:
    """True if `path` is already a git working copy or a bare repo."""
    return (path / ".git").exists() or (path / "HEAD").is_file()


def clone_or_fetch(remote_url: str, local_path: str | Path) -> None:
    """Idempotent local repo acquisition (issue #4).

    ``git clone --filter=blob:none --no-checkout <remote_url> <local_path>``
    if `local_path` doesn't yet look like a git repo (bare or otherwise);
    ``git fetch`` in place otherwise. The collector only ever reads history
    via ``git log``, so a populated working tree is never needed — a
    blobless, no-checkout clone is sufficient and far cheaper than a full
    clone for a repo the size of apache/cassandra.
    """
    local_path = Path(local_path)
    if _looks_like_git_dir(local_path):
        _run_git(local_path, ["fetch"])
        return

    local_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", remote_url, str(local_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _run_git(repo_path: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _resolve_ref(repo_path: Path, default_branch: str) -> str:
    """Resolve `default_branch` to a ref `git log` can walk.

    Tries the branch name directly first (works for a checked-out fixture
    repo, and for a bare clone, whose refs live at ``refs/heads/<branch>``
    directly), then falls back to ``origin/<default_branch>`` (a
    ``--no-checkout`` non-bare clone's local branch pointer can be absent
    depending on git version/config, but the remote-tracking ref is always
    created by ``git clone``).
    """
    for candidate in (default_branch, f"origin/{default_branch}"):
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", candidate],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return candidate
    raise ValueError(
        f"could not resolve ref {default_branch!r} (nor origin/{default_branch!r}) "
        f"in {repo_path}"
    )


@dataclass
class _RawCommit:
    sha: str
    author_name: str
    author_email: str
    occurred_at: datetime
    message: str


def _iter_commits(repo_path: Path, range_arg: str) -> Iterator[_RawCommit]:
    output = _run_git(repo_path, ["log", "--no-merges", f"--format={_LOG_FORMAT}", range_arg])
    for record in output.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        sha, name, email, date_iso, message = record.split(_UNIT_SEP, 4)
        occurred_at = datetime.fromisoformat(date_iso).astimezone(timezone.utc)
        yield _RawCommit(
            sha=sha,
            author_name=name,
            author_email=email,
            occurred_at=occurred_at,
            message=message.strip("\n"),
        )


def _is_bot(email: str, bot_patterns: Iterable[BotPattern]) -> bool:
    return any(
        re.search(pattern.regex, email)
        for pattern in bot_patterns
        if pattern.field == "git_author_email"
    )


def _to_table(table_name: str, rows: list[dict]) -> pa.Table:
    table = pa.Table.from_pylist(rows, schema=get_schema(table_name))
    return validate(table_name, table)


@dataclass
class GitCollectionResult:
    """Return value of :meth:`GitCollector.collect`."""

    contribution_event: pa.Table
    review_event: pa.Table
    next_watermark: str
    commits_collected: int
    bot_commits_excluded: int
    unparsed_reviewed_by_count: int
    placeholder_reviewer_commits: int
    unparsed_reviewed_by_examples: list[dict] = field(default_factory=list)


class GitCollector:
    """``SourceCollector`` for a single git repo (ARCHITECTURE.md §2.2)."""

    source_id = "git"

    def __init__(self, extractor: ReviewerExtractor | None = None) -> None:
        self._extractor = extractor or ReviewerExtractor()

    def collect(
        self,
        *,
        repo_path: str | Path,
        repo_label: str,
        default_branch: str,
        watermark: str | None,
        bot_patterns: Sequence[BotPattern],
        source_snapshot_id: str,
    ) -> GitCollectionResult:
        """Walk `repo_path`'s `default_branch` from `watermark` (exclusive) to HEAD.

        `repo_label` (e.g. ``"apache/cassandra"``) is written into every
        row's `repo` column. `watermark`, if given, is the last-collected
        commit SHA (ARCHITECTURE.md §4.3); `None` walks full history. Every
        row is validated against its table's schema (schema/README.md)
        before being returned.
        """
        repo_path = Path(repo_path)
        ref = _resolve_ref(repo_path, default_branch)
        range_arg = f"{watermark}..{ref}" if watermark else ref

        contribution_rows: list[dict] = []
        review_rows: list[dict] = []
        unparsed_count = 0
        unparsed_examples: list[dict] = []
        bot_excluded = 0
        commits_collected = 0
        placeholder_reviewer_count = 0

        for commit in _iter_commits(repo_path, range_arg):
            if _is_bot(commit.author_email, bot_patterns):
                bot_excluded += 1
                continue

            commits_collected += 1
            author_email_lower = commit.author_email.strip().lower()

            contribution_rows.append(
                {
                    "event_id": f"git:{repo_label}:{commit.sha}",
                    "identity_id": None,
                    "author_raw_type": "git_email",
                    "author_raw_value": author_email_lower,
                    "author_display_name": commit.author_name,
                    "event_type": "code_commit",
                    "occurred_at": commit.occurred_at,
                    "repo": repo_label,
                    "source_ref": commit.sha,
                    "source_snapshot_id": source_snapshot_id,
                }
            )

            attribution = self._extractor.extract(commit.message)
            if attribution is None:
                if looks_like_reviewer_trailer(commit.message):
                    unparsed_count += 1
                    if len(unparsed_examples) < _MAX_UNPARSED_EXAMPLES:
                        unparsed_examples.append({"sha": commit.sha, "message": commit.message})
                continue

            # Count commits that had any placeholder reviewers (issue #18).
            if attribution.placeholder_reviewers:
                placeholder_reviewer_count += 1

            # Only emit review_event rows if there are non-placeholder reviewers.
            if attribution.reviewers:
                issue_keys: Sequence[str | None] = attribution.issue_keys or (None,)
                for issue_key in issue_keys:
                    for reviewer in attribution.reviewers:
                        review_rows.append(
                            {
                                "event_id": (
                                    f"git:{repo_label}:{commit.sha}:review:"
                                    f"{reviewer}:{issue_key or 'none'}"
                                ),
                                "source": "commit_trailer",
                                "reviewer_identity_id": None,
                                "reviewer_raw_type": "git_name",
                                "reviewer_raw_value": reviewer,
                                "author_identity_id": None,
                                "author_raw_type": "git_email",
                                "author_raw_value": author_email_lower,
                                "issue_key": issue_key,
                                "repo": repo_label,
                                "occurred_at": commit.occurred_at,
                                "evidence": attribution.matched_text,
                                "source_snapshot_id": source_snapshot_id,
                            }
                        )

        next_watermark = _run_git(repo_path, ["rev-parse", ref]).strip()

        return GitCollectionResult(
            contribution_event=_to_table("contribution_event", contribution_rows),
            review_event=_to_table("review_event", review_rows),
            next_watermark=next_watermark,
            commits_collected=commits_collected,
            bot_commits_excluded=bot_excluded,
            unparsed_reviewed_by_count=unparsed_count,
            placeholder_reviewer_commits=placeholder_reviewer_count,
            unparsed_reviewed_by_examples=unparsed_examples,
        )

    def next_watermark(self, result: GitCollectionResult) -> str:
        return result.next_watermark
