"""Git ``SourceCollector`` (ARCHITECTURE.md §2.2, §3.1, §4.3).

Collects three normalized fact tables from a local clone of a project's git
repository:

- ``contribution_event`` — one row per non-merge commit (``event_type =
  'code_commit'``).
- ``file_change_event`` — one row per (non-merge commit, file) pair, from
  ``git log --no-merges --name-status`` (issue #53, ``truck_factor``'s
  per-file authorship input). Never reads file *contents* — only the path and
  git's one-letter change-status code — so it works against the same
  blobless, no-checkout clone ``contribution_event`` collection does; trees
  are always present in a ``--filter=blob:none`` clone, no blob fetch needed.
  Paths matching ``projects/<id>.yaml``'s ``truck_factor.excluded_path_globs``
  (generated/vendored code) are dropped before the row is ever built.
- ``review_event`` — one row per (reviewer, issue key) pair parsed from each
  commit's trailer by :mod:`project_health.collectors.reviewer_trailer`
  (``source = 'commit_trailer'``).

Merge commits are always excluded (``git log --no-merges``, ARCHITECTURE.md
§3.1's "merge-commit trap" — they carry no trailer of their own and distort
denominators). Bot authors are excluded via ``bot_patterns`` entries whose
``field == 'git_author_email'`` (``projects/<id>.yaml``).

All three fact tables are written with only *raw* identifiers
(``author_raw_type``/``author_raw_value``, ``reviewer_raw_type``/
``reviewer_raw_value``); ``*_identity_id`` is always ``null`` here —
identity resolution (#6) fills it in later by joining those raw columns
against ``identity_link`` (schema/README.md).

The watermark is the resolved branch ref's commit SHA (ARCHITECTURE.md
§4.3): ``collect`` walks ``watermark..ref`` (exclusive of `watermark`) when
a watermark is given, or the whole history reachable from `ref` on a first
run.

``file_change_event`` is collected with its own, independent watermark
(``file_change_watermark``) — a second ``git log --name-status`` invocation
over ``file_change_watermark..ref`` (or the whole history reachable from
`ref` when ``file_change_watermark`` is ``None``), *not* reusing `watermark`.
This is deliberate, fixing a real bug found in review (issue #53 fixup
cycle 1): a data dir collected before `file_change_event` existed already
has a ``git`` watermark sitting at HEAD (from years of ``contribution_event``
collection); if `file_change_event`'s first-ever walk reused that same
watermark, its range would be ``HEAD..HEAD`` — empty — and its entire
backfill would be silently skipped forever, with `truck_factor` computing
zero rows on every subsequent run (the exact "registered metric produced no
rows" failure `pipeline.py`'s degraded-run check exists to catch, but only
after the damage of never backfilling is already done). Both watermarks
converge to the same `next_watermark` after any run that walks all the way
to `ref` (`GitCollector.collect`'s only mode), so once `file_change_event`
has been backfilled once, it advances in lockstep with `watermark` from
then on — this two-watermark design only matters for that one-time catch-up.
See `storage.read_watermark`/`write_watermark`'s `table` parameter, which
`pipeline.py` uses to keep `file_change_event`'s watermark independent in
`state/watermarks.json`; any future raw table added to an existing source
should use the same pattern.
"""

from __future__ import annotations

import fnmatch
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
from project_health.schema import CODE_COMMIT, get_schema, validate

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


# Separate format for the ``--name-status`` walk (issue #53): the record
# separator is a *prefix* here (`RS%H...`), unlike `_LOG_FORMAT` above where
# it trails the (multi-line) commit message. `--name-status` always appends
# its file lines immediately after the pretty-printed header, before the next
# commit's output begins, so putting `RS` at the very start of the format
# string is what gives each record a clean, unambiguous boundary to split on.
_FILE_LOG_FORMAT = f"{_RECORD_SEP}%H{_UNIT_SEP}%an{_UNIT_SEP}%ae{_UNIT_SEP}%aI"


@dataclass
class _RawFileChange:
    sha: str
    author_name: str
    author_email: str
    occurred_at: datetime
    change_type: str
    file_path: str


def _iter_file_changes(repo_path: Path, range_arg: str) -> Iterator[_RawFileChange]:
    """Walk `range_arg` a second time with ``--name-status`` (issue #53).

    A second ``git log`` invocation, over the exact same `range_arg` as
    `_iter_commits`, rather than folding ``--name-status`` into that first
    call's format string: `_LOG_FORMAT` ends in a free-text, possibly
    multi-line commit message (`%B`), and `--name-status`'s file lines would
    land *between* that message and the next commit's leading separator with
    no reliable boundary of their own. Two simple, unambiguous parses beat one
    fragile one. Never reads file contents — only the path and status letter
    git reports from the tree diff, which a blobless clone already has.
    """
    output = _run_git(
        repo_path,
        ["log", "--no-merges", f"--format={_FILE_LOG_FORMAT}", "--name-status", range_arg],
    )
    for record in output.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        header, _, rest = record.partition("\n\n")
        sha, name, email, date_iso = header.split(_UNIT_SEP, 3)
        occurred_at = datetime.fromisoformat(date_iso).astimezone(timezone.utc)
        for line in rest.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status_raw = parts[0]
            # A rename/copy ('R100', 'C100', ...) carries both the source and
            # destination path; the destination is what a maintainer would
            # touch going forward, so that's what's attributed here (issue
            # #53's documented "per literal path, not rename-followed"
            # limitation — see `schema/tables.py`'s `FILE_CHANGE_EVENT`).
            file_path = parts[-1]
            change_type = status_raw[0] if status_raw else "?"
            yield _RawFileChange(
                sha=sha,
                author_name=name,
                author_email=email,
                occurred_at=occurred_at,
                change_type=change_type,
                file_path=file_path,
            )


def _is_excluded_path(file_path: str, excluded_path_globs: Iterable[str]) -> bool:
    """True if `file_path` matches any of `excluded_path_globs` (issue #53).

    fnmatch-style glob against the path exactly as git reports it
    (repo-relative, `/`-separated); `fnmatch` matches `*` across `/`.
    """
    return any(fnmatch.fnmatch(file_path, pattern) for pattern in excluded_path_globs)


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
    file_change_event: pa.Table
    review_event: pa.Table
    next_watermark: str
    commits_collected: int
    file_changes_collected: int
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
        excluded_path_globs: Sequence[str] = (),
        file_change_watermark: str | None = None,
    ) -> GitCollectionResult:
        """Walk `repo_path`'s `default_branch` from `watermark` (exclusive) to HEAD.

        `repo_label` (e.g. ``"apache/cassandra"``) is written into every
        row's `repo` column. `watermark`, if given, is the last-collected
        commit SHA for `contribution_event`/`review_event`
        (ARCHITECTURE.md §4.3); `None` walks full history. Every row is
        validated against its table's schema (schema/README.md) before being
        returned. `excluded_path_globs` (issue #53, `projects/<id>.yaml`'s
        `truck_factor.excluded_path_globs`) drops matching paths from
        `file_change_event` before a row is ever built — generated/vendored
        code is never counted as anyone's authorship.

        `file_change_watermark` (issue #53 fixup cycle 1) is
        `file_change_event`'s *own* watermark, independent of `watermark` —
        see this module's docstring for why the two must never be conflated:
        a `None` here walks `file_change_event`'s full history even when
        `watermark` is already caught up to HEAD, which is exactly the
        one-time backfill a `file_change_event`-unaware watermark would
        otherwise skip forever. Defaults to `None` (full history) so a
        caller that doesn't yet track it separately (e.g. an ad hoc script)
        gets a correct, if unnecessarily-repeated, full walk rather than a
        silently-empty one.
        """
        repo_path = Path(repo_path)
        ref = _resolve_ref(repo_path, default_branch)
        range_arg = f"{watermark}..{ref}" if watermark else ref
        file_range_arg = f"{file_change_watermark}..{ref}" if file_change_watermark else ref

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
                    "event_type": CODE_COMMIT,
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

        file_change_rows: list[dict] = []
        file_changes_collected = 0
        for file_change in _iter_file_changes(repo_path, file_range_arg):
            if _is_bot(file_change.author_email, bot_patterns):
                continue
            if _is_excluded_path(file_change.file_path, excluded_path_globs):
                continue

            file_changes_collected += 1
            file_change_rows.append(
                {
                    "event_id": (
                        f"git:{repo_label}:{file_change.sha}:file:{file_change.file_path}"
                    ),
                    "identity_id": None,
                    "author_raw_type": "git_email",
                    "author_raw_value": file_change.author_email.strip().lower(),
                    "author_display_name": file_change.author_name,
                    "change_type": file_change.change_type,
                    "file_path": file_change.file_path,
                    "occurred_at": file_change.occurred_at,
                    "repo": repo_label,
                    "source_ref": file_change.sha,
                    "source_snapshot_id": source_snapshot_id,
                }
            )

        next_watermark = _run_git(repo_path, ["rev-parse", ref]).strip()

        return GitCollectionResult(
            contribution_event=_to_table("contribution_event", contribution_rows),
            file_change_event=_to_table("file_change_event", file_change_rows),
            review_event=_to_table("review_event", review_rows),
            next_watermark=next_watermark,
            commits_collected=commits_collected,
            file_changes_collected=file_changes_collected,
            bot_commits_excluded=bot_excluded,
            unparsed_reviewed_by_count=unparsed_count,
            placeholder_reviewer_commits=placeholder_reviewer_count,
            unparsed_reviewed_by_examples=unparsed_examples,
        )

    def next_watermark(self, result: GitCollectionResult) -> str:
        return result.next_watermark
