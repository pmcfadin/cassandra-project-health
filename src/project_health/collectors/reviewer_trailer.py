"""Pure commit-trailer reviewer extraction (ARCHITECTURE.md §2.2, §3.1).

``ReviewerExtractor`` is the concrete implementation of the illustrative
``ReviewerExtractor`` ``Protocol`` in ARCHITECTURE.md §2.2, selected by
``reviewer_extraction.commit_trailer.type: commit_message_regex`` in
``projects/<id>.yaml``. It is a pure function over a commit message string —
no I/O, no network, no filesystem access — so it is trivially unit-testable
and safe to call once per commit from :mod:`project_health.collectors.git`.

Cassandra's commit convention is ``patch by X; reviewed by Y for
CASSANDRA-NNNNN`` (ARCHITECTURE.md §3.1), but real history is looser than
that one literal string:

- the separator between the ``patch by`` clause and ``reviewed by`` is
  sometimes ``;`` and sometimes ``,``;
- ``patch by ...`` is sometimes omitted entirely;
- multiple reviewers are joined with ``,``, ``and``, or ``&``;
- multiple issue keys are joined with ``,`` after a single ``for``;
- trailing punctuation (a period, extra whitespace) commonly follows the
  last name or issue key.

This module handles all of the above. A message that contains the
case-insensitive phrase "reviewed by" but that the regex still fails to
parse into at least one reviewer is *not* silently dropped — callers use
:func:`looks_like_reviewer_trailer` to detect that case and count/log it
separately (issue #4 acceptance criteria), rather than treating "no match"
as "nothing to see here".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Bumped whenever this module's parsing behavior changes in a way that can
# change an already-collected commit's parsed reviewers/issue-keys (issue
# #77). Collectors that persist parsed output (`collectors/git.py`'s
# `review_event` rows, `collectors/governance_git.py`'s `commit_record`
# rows via `pipeline.py`) stamp every row with the `PARSER_VERSION` that
# produced it; `pipeline.py`'s read-time dedup then keeps only the
# highest-`parser_version` row(s) for a given commit, so a version bump plus
# a full-history re-derivation (`pipeline._collect_git`,
# `pipeline._collect_governance_commit_records`) is what makes a parser fix
# *replace* every historical commit's stale attribution, without ever
# rewriting or deleting the original append-only raw Parquet rows (D2 rule
# 6, D3, ARCHITECTURE.md §3's "a correction is a new row, never an in-place
# update").
#
# History:
#   1 (implicit, pre-issue-#77): the original single-physical-line match.
#   2 (issue #77): unwraps a line-wrapped "patch by"/"reviewed by"/
#      "authored by" trailer paragraph (`_unwrap_trailer_paragraphs`) before
#      matching, so a reviewer name split across a line wrap (e.g. "...and
#      Sam\nTunnicliffe for CASSANDRA-21189") is no longer truncated to
#      "Sam".
PARSER_VERSION = 2

# Matches one trailer line of the form (all parts case-insensitive):
#   [patch by <patch_by> [(;|,)]] reviewed by[:] <reviewed_by> [for <issue_tail>]
# `patch by ...` is optional so a bare "reviewed by X for Y" line still
# parses, and the `;`/`,` separator between the two clauses is itself
# optional — real history includes "patch by X; reviewed by Y" (semicolon,
# the documented convention), "patch by X, reviewed by Y" (comma), and
# occasionally "patch by X reviewed by Y" with no punctuation at all.
# `.+?` is non-greedy, anchored to end-of-line by `$` under MULTILINE, so it
# never spills into a following trailer line (e.g. a `Co-authored-by:` line
# right after the reviewer trailer).
_TRAILER_LINE_RE = re.compile(
    r"""(?ix)
    ^[ \t]*
    (?:patch\s+by\s+(?P<patch_by>.+?)\s*[;,]?\s*)?
    reviewed\s+by\s*:?\s*
    (?P<reviewed_by>.+?)
    (?:\s+for\s+(?P<issue_tail>.+?))?
    [ \t]*\.?[ \t]*$
    """,
    re.MULTILINE,
)

# --- Governance engine (issue #36) extension: two forms real trunk history
# uses that the strict trailer above misses (governance-policy.yaml
# `reviewer-present.review_wording_check.regression_examples`,
# docs/spec/GOVERNANCE.md §8):
#
#   1. "reviewed <Name>" — the reviewer is named without the word "by" at
#      all (sha 208d87513f: "patch by Mick Semb Wever; reviewed Štefan
#      Miklošovič for CASSANDRA-21489").
#   2. "Authored by <Name>; Reviewed by <Name> for ..." — the line opens
#      with "Authored by", not "patch by"/"reviewed by", so the strict
#      regex's `^`-anchor never reaches the real "Reviewed by" clause later
#      in the same line (sha 05186d7869: "Authored by Lorina Poland
#      (polandll); Reviewed by Branimir Lambov (blambov) for
#      CASSANDRA-18236").
#
# Only tried as a *fallback*, after `_TRAILER_LINE_RE` has already failed to
# match (see `ReviewerExtractor.extract`) — every message the strict regex
# already parses keeps parsing exactly the same way; this can only turn a
# previous `None` into a real attribution, never change an existing one.
#
# `reviewed_by`'s required first character (`[^\Wa-z0-9_]`) — a word
# character that is not a lowercase ASCII letter/digit/underscore — accepts
# a capitalized or accented name (`Štefan`, `Brandon`) while rejecting a
# lowercase filler word (`a`, `the`, `on`, ...), which is what keeps a
# sentence like "reviewed a fix for CASSANDRA-100" from being mistaken for a
# named reviewer. The `for <issue_tail>` clause is *mandatory* here (unlike
# the strict regex, where it's optional) as a second guard against the same
# false-positive risk — both real regression examples have one.
#
# Case-insensitivity is applied per-keyword via scoped inline groups
# (`(?i:...)`), never as a global `(?i)` flag: a *global* flag would also
# case-fold the `[^\Wa-z0-9_]` character class's `a-z` range to match
# uppercase too, silently defeating the "starts with a capital" guard this
# regex depends on (found live while testing against the regression shas —
# a global `(?i)` made `reviewed_by`'s required-uppercase-first-char check a
# no-op).
_TRAILER_LINE_RE_EXT = re.compile(
    r"""(?x)
    ^[ \t]*
    (?:(?i:(?:patch|authored)\s+by)\s+(?P<patch_by>.+?)\s*[;,]?\s*)?
    (?i:reviewed)\s+(?:(?i:by)\s*:?\s*)?
    (?P<reviewed_by>[^\Wa-z0-9_].+?)
    \s+(?i:for)\s+(?P<issue_tail>.+?)
    [ \t]*\.?[ \t]*$
    """,
    re.MULTILINE,
)

# Splits a "reviewed by" clause into individual names on `,`, `&`, or a
# whole-word `and` (case-insensitive) — e.g. "Alice, Bob and Carol" ->
# ["Alice", "Bob", "Carol"].
_NAME_SPLIT_RE = re.compile(r"(?i)\s*(?:,|&|\band\b)\s*")

# JIRA-style issue keys: an all-caps project key, a hyphen, a number.
# findall (rather than a single match) is what lets "CASSANDRA-103,
# CASSANDRA-104" resolve to two keys, and it naturally ignores any trailing
# punctuation since `\d+` simply stops at the first non-digit.
_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,15}-\d+\b")

# Some real trailers drop (or typo) the "for" before the issue key entirely,
# e.g. "reviewed by Brandon Williams CASSANDRA-18555", "... from
# CASSANDRA-18830", "... (CASSANDRA-18772)", "... or CASSANDRA-16843" (a
# typo for "for"). Without this, the glued-on issue key becomes part of the
# "reviewer name" and inflates unique-reviewer counts with garbage. This
# matches a trailing issue-key run — optionally preceded by a connector
# word and/or wrapped in parens — at the *end* of an already-isolated
# "reviewed by" clause (only tried when the main pattern found no explicit
# "for" clause; see `ReviewerExtractor.extract`), and never touches the
# uppercase-only requirement with a stray case-insensitive flag, so it
# can't mistake a capitalized name for an issue key.
_TRAILING_ISSUE_RE = re.compile(
    r"""(?x)
    \s*
    \(?
    \s*
    (?:(?i:for|from|in|or|on|per)\b\s*)?
    (?P<tail_keys>[A-Z][A-Z0-9]{1,15}-\d+(?:\s*,\s*[A-Z][A-Z0-9]{1,15}-\d+)*)
    \s*\)?
    \s*\Z
    """
)

# Case-insensitive "reviewed by" substring check, used to flag a message as
# "should have parsed but didn't" (issue #4 acceptance criteria: unparsed-
# but-contains-"reviewed by" messages are counted and logged, not silently
# dropped).
_REVIEWED_BY_PHRASE_RE = re.compile(r"(?i)reviewed\s+by")

# --- Line-wrap unwrapping (issue #77) ---------------------------------------
#
# Real trunk history line-wraps a "patch by ...; reviewed by ..." trailer
# paragraph at ~72 columns, e.g.:
#
#   Patch by Sam Lightfoot; reviewed by Dmitry Konstantinov and Sam
#   Tunnicliffe for CASSANDRA-21189
#
# `_TRAILER_LINE_RE`/`_TRAILER_LINE_RE_EXT` above are anchored `^...$` under
# `re.MULTILINE` specifically so they never spill into an unrelated
# following line (a `Co-authored-by:` trailer, blank line, or new paragraph)
# -- but that same anchoring truncates a wrapped trailer at the first
# newline, silently turning "Sam Tunnicliffe" into "Sam". `_unwrap_trailer_
# paragraphs` runs *before* either regex and rejoins a wrapped trailer
# paragraph into one logical line (a single space where the wrap was),
# without touching any other line in the message.
_TRAILER_START_RE = re.compile(r"(?i)^[ \t]*(?:patch\s+by|authored\s+by|reviewed\s+by)\b")

# A line that starts a *different* trailer -- reaching one of these means
# the paragraph being unwrapped has ended, even if no blank line separates
# them (real trunk history sometimes puts a `Co-authored-by:` line directly
# after the reviewer trailer with no blank line in between).
_NEW_TRAILER_START_RE = re.compile(
    r"(?i)^[ \t]*(?:patch\s+by|authored\s+by|reviewed(?:\s+by)?|co-authored-by|signed-off-by)\b"
)

# The trailer paragraph's own terminator: a "for <ISSUE-KEY>" clause. Once
# the text accumulated so far contains this, the paragraph is complete --
# continuing to pull in further lines (a blank line, a new trailer, or just
# unrelated commit-body prose) must never happen, even if those lines
# happen to still be adjacent.
_FOR_ISSUE_TERMINATOR_RE = re.compile(r"(?i:for)\s+[A-Z][A-Z0-9]{1,15}-\d+\b")


def _unwrap_trailer_paragraphs(message: str) -> str:
    """Join a wrapped "patch by"/"reviewed by"/"authored by" trailer
    paragraph's continuation lines into its opening line (issue #77).

    A line is only ever pulled into the paragraph it continues while *all*
    of these hold: the paragraph hasn't already reached its "for <ISSUE-KEY>"
    terminator, the candidate line isn't blank, and the candidate line
    doesn't itself open a new trailer (`Co-authored-by:`, another `patch
    by`, ...). Every other line in `message` -- including everything after
    the paragraph ends -- passes through completely unchanged, so an
    unrelated following line (a blank line, a `Co-authored-by:` trailer, or
    ordinary commit-body prose) is never swallowed.
    """
    lines = message.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() and _TRAILER_START_RE.match(line):
            accumulated = [line.strip()]
            j = i + 1
            while j < n:
                combined = " ".join(accumulated)
                if _FOR_ISSUE_TERMINATOR_RE.search(combined):
                    break
                candidate = lines[j]
                if candidate.strip() == "":
                    break
                if _NEW_TRAILER_START_RE.match(candidate):
                    break
                accumulated.append(candidate.strip())
                j += 1
            out.append(" ".join(accumulated))
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


# Placeholder reviewer names that should be filtered out (issue #18).
# These are matched case-insensitively, so stored in lowercase.
_PLACEHOLDER_NAMES = frozenset(("tbd", "tba", "none", "nobody", "n/a", "na", "?", "unknown"))


@dataclass(frozen=True)
class ReviewAttribution:
    """One parsed commit-trailer reviewer attribution.

    ``reviewers``, ``issue_keys``, and ``placeholder_reviewers`` are each
    de-duplicated, order-preserved lists; either may be empty. ``reviewers``
    may be empty when a commit has only placeholder reviewers (issue #18).

    :meth:`ReviewerExtractor.extract` returns ``None`` if no line matches
    the "reviewed by" trailer shape or if the regex matched but no names
    (placeholder or real) could be extracted.

    ``placeholder_reviewers`` records which reviewer names were dropped because
    they matched placeholder patterns (TBD, none, n/a, etc.; see _PLACEHOLDER_NAMES).
    """

    patch_by: str | None
    reviewers: tuple[str, ...]
    issue_keys: tuple[str, ...]
    matched_text: str
    placeholder_reviewers: tuple[str, ...] = ()


def _is_placeholder(name: str) -> bool:
    """True if `name` matches a placeholder pattern (case-insensitive)."""
    return name.lower() in _PLACEHOLDER_NAMES


def _clean_name(raw: str) -> str:
    return raw.strip().strip(".,;:& \t")


def _split_names(raw: str) -> tuple[str, ...]:
    names = [_clean_name(part) for part in _NAME_SPLIT_RE.split(raw)]
    seen: dict[str, None] = {}
    for name in names:
        if name and name not in seen:
            seen[name] = None
    return tuple(seen)


def _extract_issue_keys(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    keys = _ISSUE_KEY_RE.findall(raw)
    seen: dict[str, None] = {}
    for key in keys:
        if key not in seen:
            seen[key] = None
    return tuple(seen)


def extract_issue_keys(message: str) -> tuple[str, ...]:
    """Every CASSANDRA-N-style issue key anywhere in `message` (order-
    preserved, de-duplicated).

    Public wrapper around the same `_ISSUE_KEY_RE` used to parse a trailer's
    "for <issue_tail>" clause, for callers (governance engine, issue #36)
    that need "does this commit reference a JIRA key at all" independent of
    whether a reviewer trailer was found.
    """
    return _extract_issue_keys(message)


def looks_like_reviewer_trailer(commit_message: str) -> bool:
    """True if `commit_message` contains the phrase "reviewed by" (any case).

    Used by callers to distinguish "this commit has no reviewer trailer at
    all" (not worth logging) from "this commit looks like it should have a
    reviewer trailer but the regex couldn't parse it" (must be counted and
    logged per issue #4's acceptance criteria, not silently dropped).
    """
    return bool(_REVIEWED_BY_PHRASE_RE.search(commit_message))


class ReviewerExtractor:
    """Selected by ``reviewer_extraction.commit_trailer.type`` — pure, no I/O.

    `pattern` is accepted (and stored) for parity with
    ``projects/<id>.yaml``'s ``reviewer_extraction.commit_trailer.pattern``
    config field, but the actual parsing here is a fixed, more permissive
    implementation (see module docstring) rather than a literal
    ``re.compile(pattern)`` — the configured pattern documents Cassandra's
    convention, but real commit history uses `,` as well as `;`, omits
    "patch by" sometimes, and lists multiple reviewers/issue keys, all of
    which a single hard-coded pattern string can't express as cleanly as
    this module's dedicated regex + post-processing.
    """

    def __init__(self, pattern: str | None = None) -> None:
        self.pattern = pattern

    def extract(self, commit_message: str) -> ReviewAttribution | None:
        """Parse the first reviewer trailer line in `commit_message`.

        Returns ``None`` if no line matches the "reviewed by" trailer shape,
        or if a line matches but no reviewer name (real or placeholder) survives
        cleaning (e.g. an empty ``reviewed by`` clause).

        Returns a ReviewAttribution with empty ``reviewers`` but non-empty
        ``placeholder_reviewers`` when a trailer matched but contained only
        placeholder names (issue #18); the GitCollector counts these to signal
        data quality issues.
        """
        unwrapped_message = _unwrap_trailer_paragraphs(commit_message)
        match = _TRAILER_LINE_RE.search(unwrapped_message)
        if match is None:
            # Fallback for the "reviewed <Name>" (missing "by") and
            # "Authored by ...; Reviewed by ..." forms (issue #36) — see
            # `_TRAILER_LINE_RE_EXT`'s docstring above. Never changes the
            # result for a message the strict regex already parses.
            match = _TRAILER_LINE_RE_EXT.search(unwrapped_message)
        if match is None:
            return None

        reviewed_by_raw = match.group("reviewed_by")
        issue_tail = match.group("issue_tail")

        if issue_tail is None:
            # No explicit "for <issue>" clause — check whether the tail of
            # the reviewer list is actually a glued-on issue key (a missing
            # or mistyped "for"; see module docstring), and if so, split it
            # out rather than let it become part of a "reviewer name".
            trailing = _TRAILING_ISSUE_RE.search(reviewed_by_raw)
            if trailing is not None:
                issue_tail = trailing.group("tail_keys")
                reviewed_by_raw = reviewed_by_raw[: trailing.start()]

        all_names = _split_names(reviewed_by_raw)

        # Return None if no names at all (empty reviewed_by clause).
        if not all_names:
            return None

        # Separate actual reviewers from placeholders (issue #18).
        reviewers_list = []
        placeholders_list = []
        for name in all_names:
            if _is_placeholder(name):
                placeholders_list.append(name)
            else:
                reviewers_list.append(name)

        reviewers = tuple(reviewers_list)
        placeholders = tuple(placeholders_list)

        patch_by_raw = match.group("patch_by")
        patch_by = _clean_name(patch_by_raw) if patch_by_raw else None
        issue_keys = _extract_issue_keys(issue_tail)

        return ReviewAttribution(
            patch_by=patch_by,
            reviewers=reviewers,
            issue_keys=issue_keys,
            matched_text=match.group(0).strip(),
            placeholder_reviewers=placeholders,
        )
