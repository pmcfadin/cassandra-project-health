"""Deterministic text preprocessing for the Phase 2a Jev pilot (issue #43, D18).

Turns a raw dev@/JIRA message body into the plain text a Jev `system_one` call
actually classifies (`questions_v1.yaml`'s `message.text`/`parent.text`,
COMMUNITY-HEALTH.md §4.1: "normalization (strip quoting, boilerplate,
signatures...)"). Every function here is a pure string -> string (or
string -> bool) transform with no I/O and no network access, so it is fully
covered by offline, synthetic-text unit tests (`tests/test_preprocess.py`).

Pipeline (`preprocess_text`), in order:

1. `strip_quoted_text` -- drop `>`-quoted lines, "On ... wrote:" attribution
   blocks (English, and the German "Am ... schrieb"/French "Le ... a écrit"
   equivalents; wrapped across up to 3 lines), and Outlook-style
   "From:/Sent:/To:/Subject:" forwarded-header blocks (and everything
   conventionally following them).
2. `strip_jira_quoted_text` -- only for `source == "jira_comment"`: drop
   JIRA `{quote}...{quote}` blocks and `bq. ` lines entirely (someone
   else's quoted words, the JIRA equivalent of step 1's `>` lines -- not
   placeholder-replaced, since a quoter objecting to a quoted comment must
   not inherit that comment's labels).
3. `strip_signature` -- drop an RFC 3676 `-- ` signature delimiter and
   everything after it, plus a couple of common single-line footers.
4. `strip_code_blocks` -- collapse fenced (```...```) and JIRA `{code}`/
   `{noformat}` blocks to the `[code]` placeholder.
5. `strip_stack_traces` -- collapse Java/Python-style stack traces --
   including a short, headerless trace (a bare exception-class line plus
   one frame, or two-or-more consecutive frame lines with no header at
   all) -- to the `[stacktrace]` placeholder.
6. `collapse_log_dumps` -- collapse long runs of timestamped log lines to
   the `[code]` placeholder.
7. `strip_jira_wiki_markup` -- only for `source == "jira_comment"`: drop
   JIRA wiki markup (headings, bold/italic/monospace, links, bullets,
   tables) down to plain text.
8. `normalize_whitespace` -- collapse blank-line runs and trailing
   whitespace.

`is_automated_sender` is a separate, standalone check (not part of the text
pipeline itself): it is applied by the caller, and by
`classify/text_fetch.py`'s fetch-and-preprocess helpers, to a message's
*sender* (a mailing-list `From:` address or a JIRA comment author's
username) to decide whether to fetch/classify the message at all -- an
automated sender's message is dropped before fetch, not preprocessed
(COMMUNITY-HEALTH.md §4.1: automated/notification traffic is not human
communication). See `projects/cassandra.yaml`'s `automated_senders:` key.
"""

from __future__ import annotations

import re
from typing import Any

CODE_PLACEHOLDER = "[code]"
STACKTRACE_PLACEHOLDER = "[stacktrace]"

# --- 1. Quoted text -----------------------------------------------------

# "On Mon, Jan 1, 2024 at 10:00 AM, Name <email> wrote:" (Gmail/most MUAs),
# plus the German "Am ... schrieb" and French "Le ... a écrit" equivalents.
# Mail clients routinely *wrap* this line across 2-3 lines when the name/
# address is long (e.g. "On Tue, Mar 4, 2025 at 9:12 AM Bob Rev\n
# <bob@example.org> wrote:"), so this is a line-based scan
# (`_find_attribution_block_start`) rather than a single-line regex: it
# looks for a line starting with "On "/"Am "/"Le " and then checks that line
# plus up to the next two lines for the block's closing marker
# ("wrote"/"schrieb"/"a écrit"), deliberately not anchoring the marker to
# end-of-line since German/French word order puts it mid-line, not at the
# end (e.g. "Am 4. März 2025 schrieb Bob Rev <bob@example.org>:").
_ATTRIBUTION_START_RE = re.compile(r"(?i)^(?:On|Am|Le)\s")
_ATTRIBUTION_MARKER_RE = re.compile(r"(?i)\b(?:wrote|schrieb|a\s+écrit)\b")
_ATTRIBUTION_WINDOW_LINES = 3


def _find_attribution_block_start(lines: list[str]) -> int | None:
    """The index of the first line starting a "someone wrote:"-style
    attribution block (English/German/French), or `None` if none is found.

    A block starts at a line matching `_ATTRIBUTION_START_RE`; it "closes"
    (confirming this is really an attribution line, not e.g. an unrelated
    sentence starting with "On") if `_ATTRIBUTION_MARKER_RE` appears
    anywhere within that line and the following `_ATTRIBUTION_WINDOW_LINES
    - 1` lines -- covering both the single-line case and a client-wrapped
    multi-line case.
    """
    for i, line in enumerate(lines):
        if not _ATTRIBUTION_START_RE.match(line):
            continue
        window = lines[i : i + _ATTRIBUTION_WINDOW_LINES]
        if any(_ATTRIBUTION_MARKER_RE.search(w) for w in window):
            return i
    return None

# Outlook-style forwarded/replied header block: a "From:" line followed
# (within a few lines) by "Sent:"/"To:"/"Subject:" lines. Order can vary
# slightly between Outlook versions, so this matches "From:" first and lets
# the caller truncate from there -- the block is always followed by the
# quoted prior message in practice, so truncating from "From:" onward is
# the same "drop everything after the attribution line" strategy as
# `_ON_WROTE_RE`.
_OUTLOOK_HEADER_RE = re.compile(
    r"(?im)^From:.*\n(?:.*\n){0,3}?Sent:.*\n(?:.*\n){0,3}?To:.*\n(?:.*\n){0,3}?Subject:.*$"
)

# A line quoted with one or more leading `>` markers (any nesting depth).
_QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")


def strip_quoted_text(text: str) -> str:
    """Drop `>`-quoted lines, "On ... wrote:" blocks, and Outlook-style
    forwarded-header blocks.

    "On ... wrote:" and the Outlook header block are both treated as "the
    rest of the message from here on is quoted" -- true for top-posted
    replies, which is the overwhelmingly common case on dev@ and in JIRA
    comments quoting a prior comment. Truncating there is a deliberate,
    documented simplification (not a full quote-depth parser): the
    alternative (only removing lines syntactically marked as quotes) would
    leave Outlook-pasted quoted bodies -- which usually carry no `>` markers
    at all -- untouched.
    """
    match = _OUTLOOK_HEADER_RE.search(text)
    if match:
        text = text[: match.start()]

    lines = text.split("\n")
    cut_at = _find_attribution_block_start(lines)
    if cut_at is not None:
        text = "\n".join(lines[:cut_at])

    text = _QUOTE_LINE_RE.sub("", text)
    return text


# --- 2. Signatures --------------------------------------------------------

# RFC 3676 signature delimiter: a line that is exactly "--" or "-- ".
_SIG_DELIM_RE = re.compile(r"(?m)^-- ?\s*$")

# A handful of common single-line mobile-client footers. Removed as single
# lines (not "everything after") since they can appear anywhere a message
# was composed on a phone, not necessarily at the very end.
_MOBILE_FOOTER_RE = re.compile(
    r"(?im)^\s*Sent from my (iPhone|iPad|Android( device| phone)?)\s*\.?\s*$"
)


def strip_signature(text: str) -> str:
    """Drop an RFC 3676 `-- ` signature block and common mobile footers."""
    match = _SIG_DELIM_RE.search(text)
    if match:
        text = text[: match.start()]
    text = _MOBILE_FOOTER_RE.sub("", text)
    return text


# --- JIRA quoted text ({quote} blocks, bq. lines) ---------------------------

# {quote}...{quote} wraps text quoted from someone else (JIRA's equivalent of
# a mailing-list `>` block) -- it must be *removed*, not placeholder-replaced
# like a code block, since it's the quoter's target text, not the quoter's
# own words. Left in, a quoter objecting to a hostile comment by quoting it
# back would get their own (non-hostile) message mislabeled by whatever
# labels apply to the quoted text.
_JIRA_QUOTE_BLOCK_RE = re.compile(r"\{quote\}.*?\{quote\}", re.DOTALL | re.IGNORECASE)
# `bq. some quoted line` -- JIRA wiki markup's single-line blockquote. Like
# `{quote}`, the whole line is someone else's words and is dropped entirely,
# not just the `bq. ` prefix (contrast with `strip_jira_wiki_markup`, which
# only strips *markup characters* from the author's own text).
_JIRA_BQ_LINE_RE = re.compile(r"(?m)^bq\.[ \t]?.*$")


def strip_jira_quoted_text(text: str) -> str:
    """Drop JIRA `{quote}...{quote}` blocks and `bq. ` lines entirely."""
    text = _JIRA_QUOTE_BLOCK_RE.sub("", text)
    text = _JIRA_BQ_LINE_RE.sub("", text)
    return text


# --- 3. Code blocks (fenced markdown + JIRA {code}/{noformat}) -----------

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_JIRA_CODE_RE = re.compile(r"\{code(?::[^}\n]*)?\}.*?\{code\}", re.DOTALL | re.IGNORECASE)
_JIRA_NOFORMAT_RE = re.compile(r"\{noformat\}.*?\{noformat\}", re.DOTALL | re.IGNORECASE)


def strip_code_blocks(text: str) -> str:
    """Collapse explicitly-delimited code blocks to `[code]`.

    Covers markdown-style triple-backtick fences and JIRA's `{code}`/
    `{noformat}` macros -- both self-delimiting, so collapsing them first
    (before `strip_stack_traces`/`collapse_log_dumps` run on whatever is
    left) avoids double-processing a stack trace or log dump that happens
    to already be wrapped in one of these blocks.
    """
    text = _JIRA_CODE_RE.sub(CODE_PLACEHOLDER, text)
    text = _JIRA_NOFORMAT_RE.sub(CODE_PLACEHOLDER, text)
    text = _FENCED_CODE_RE.sub(CODE_PLACEHOLDER, text)
    return text


# --- 4. Stack traces -------------------------------------------------------

_STACK_START_RE = re.compile(
    r"^(Traceback \(most recent call last\):|Exception in thread \"|Caused by:)"
)
# Java-style frame ("\tat pkg.Class.method(File.java:123)"), Python-style
# frame ("  File \"mod.py\", line 12, in func"), or a Java "... N more" elision.
_STACK_FRAME_RE = re.compile(
    r"^\s*(at\s+[\w$.<>]+\(.*\)|File \"[^\"]+\", line \d+.*|\.\.\.\s*\d+\s*more)\s*$"
)
# The final summary line of a Python traceback ("ValueError: bad input") or
# a bare Java exception class line -- consumed as part of the same block if
# it directly follows frame lines.
_EXCEPTION_TAIL_RE = re.compile(r"^[\w$.]*(Error|Exception)\b.*$")
# A bare exception-class line with no leading marker keyword at all (e.g.
# "java.lang.NullPointerException" or "java.lang.NullPointerException: msg"
# on its own, with no preceding "Exception in thread ..."/"Traceback ..."):
# same shape as `_EXCEPTION_TAIL_RE`, named separately since it plays the
# *start*-marker role here (see `_looks_like_trace_start` below), not the
# tail-consumption role `_EXCEPTION_TAIL_RE` plays inside the while loop.
_BARE_EXCEPTION_LINE_RE = _EXCEPTION_TAIL_RE


def _looks_like_trace_start(lines: list[str], i: int) -> bool:
    """Whether `lines[i]` starts a stack trace block.

    Three ways in, per issue #43 fixup: an explicit marker
    (`_STACK_START_RE`); a short/bare trace with no marker at all -- a bare
    exception-class line immediately followed by at least one frame line
    (`java.lang.NullPointerException` / `\tat ...`); or two-or-more
    consecutive frame lines with no exception-class line at all (a trace
    fragment pasted without its header).
    """
    line = lines[i]
    if _STACK_START_RE.match(line.strip()) or _STACK_START_RE.match(line):
        return True
    next_line = lines[i + 1] if i + 1 < len(lines) else ""
    if _BARE_EXCEPTION_LINE_RE.match(line) and _STACK_FRAME_RE.match(next_line):
        return True
    if _STACK_FRAME_RE.match(line) and _STACK_FRAME_RE.match(next_line):
        return True
    return False


def strip_stack_traces(text: str) -> str:
    """Collapse Java- or Python-style stack traces to `[stacktrace]`.

    A simple line-based state machine, not a full parser: once a start is
    detected (`_looks_like_trace_start`), every following frame/
    `Caused by:`/blank/exception-tail line is consumed into the same
    collapsed block, stopping at the first line that looks like ordinary
    prose.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _looks_like_trace_start(lines, i):
            out.append(STACKTRACE_PLACEHOLDER)
            i += 1
            # Consume every following frame line (Java "\tat ..."/Python
            # "  File ..."), any further-indented continuation line under a
            # frame (e.g. the source snippet line under a Python "File"
            # line), a "Caused by:"/blank line, or the trace's trailing
            # unindented exception-summary line -- stopping at the first
            # line that looks like ordinary, unindented prose.
            while i < n and (
                lines[i].strip() == ""
                or lines[i][:1].isspace()
                or _STACK_FRAME_RE.match(lines[i])
                or _STACK_START_RE.match(lines[i])
                or _EXCEPTION_TAIL_RE.match(lines[i].strip())
            ):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# --- 5. Long log dumps -----------------------------------------------------

# "2024-01-01 10:00:00,123" / "2024-01-01T10:00:00" / "[2024-01-01 10:00:00]"
_LOG_LINE_RE = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")

DEFAULT_LOG_DUMP_MIN_LINES = 5


def collapse_log_dumps(text: str, min_lines: int = DEFAULT_LOG_DUMP_MIN_LINES) -> str:
    """Collapse a run of `min_lines` or more consecutive timestamped log
    lines to `[code]`. Below the threshold, a couple of log lines quoted
    for context are left alone -- only genuine dumps are collapsed."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _LOG_LINE_RE.match(lines[i]):
            j = i
            while j < n and (_LOG_LINE_RE.match(lines[j]) or lines[j].strip() == ""):
                j += 1
            run = lines[i:j]
            log_line_count = sum(1 for line in run if _LOG_LINE_RE.match(line))
            if log_line_count >= min_lines:
                out.append(CODE_PLACEHOLDER)
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# --- 6. JIRA wiki markup ----------------------------------------------------

_JIRA_HEADING_RE = re.compile(r"(?m)^h[1-6]\.\s*")
_JIRA_BULLET_RE = re.compile(r"(?m)^\s*[*#]+\s+")
_JIRA_TABLE_BAR_RE = re.compile(r"\|\|")
_JIRA_TABLE_CELL_RE = re.compile(r"(?m)^\s*\|(?!\|)")
_JIRA_MONOSPACE_RE = re.compile(r"\{\{(.+?)\}\}")
_JIRA_BOLD_RE = re.compile(r"(?<![*\w])\*(\S.*?\S|\S)\*(?![*\w])")
_JIRA_ITALIC_RE = re.compile(r"(?<![_\w])_(\S.*?\S|\S)_(?![_\w])")
_JIRA_LINK_RE = re.compile(r"\[([^|\]]+)\|[^\]]*\]")
# Negative lookahead excludes this module's own `[code]`/`[stacktrace]`
# placeholders, which strip_code_blocks/strip_stack_traces (run before this
# function in `preprocess_text`) may have already inserted -- without it,
# this bracket-link regex would strip them down to bare "code"/"stacktrace".
_JIRA_BARE_LINK_RE = re.compile(r"\[(?!code\]|stacktrace\])([^|\]]+)\]")


def strip_jira_wiki_markup(text: str) -> str:
    """Drop JIRA wiki markup, leaving the underlying plain text.

    Handles the markup families that actually change a message's meaning if
    left in (headings, bold/italic/monospace emphasis, `[text|url]` links,
    bullet/numbered lists, `||`/`|` tables) -- not a full wiki-markup
    renderer.
    """
    text = _JIRA_HEADING_RE.sub("", text)
    text = _JIRA_BULLET_RE.sub("", text)
    text = _JIRA_TABLE_BAR_RE.sub("|", text)
    text = _JIRA_TABLE_CELL_RE.sub("", text)
    text = _JIRA_MONOSPACE_RE.sub(r"\1", text)
    text = _JIRA_BOLD_RE.sub(r"\1", text)
    text = _JIRA_ITALIC_RE.sub(r"\1", text)
    text = _JIRA_LINK_RE.sub(r"\1", text)
    text = _JIRA_BARE_LINK_RE.sub(r"\1", text)
    return text


# --- 7. Whitespace normalization -------------------------------------------

_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def normalize_whitespace(text: str) -> str:
    """Strip trailing whitespace per line, collapse 3+ blank lines to one
    blank line, and trim the whole text."""
    text = _TRAILING_WS_RE.sub("", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


# --- Orchestration ----------------------------------------------------------

VALID_SOURCES = frozenset({"mailing_list", "jira_comment", "github_pr_comment"})


def preprocess_text(raw_text: str, source: str) -> str:
    """Run the full deterministic pipeline over one message's raw body.

    `source` must be one of `questions_v1.yaml`'s `message.source` enum
    values -- JIRA wiki-markup stripping only runs for `jira_comment`, since
    mailing-list/PR-comment text is never JIRA-wiki-formatted and treating
    it as such risks mangling ordinary prose (e.g. `*emphasis*` used in
    plain English).
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {sorted(VALID_SOURCES)}, got {source!r}")
    if raw_text is None:
        raw_text = ""

    text = raw_text
    text = strip_quoted_text(text)
    if source == "jira_comment":
        text = strip_jira_quoted_text(text)
    text = strip_signature(text)
    text = strip_code_blocks(text)
    text = strip_stack_traces(text)
    text = collapse_log_dumps(text)
    if source == "jira_comment":
        text = strip_jira_wiki_markup(text)
    text = normalize_whitespace(text)
    return text


# --- Automated senders -------------------------------------------------------


def _pattern_regex(pattern: Any) -> str:
    if isinstance(pattern, str):
        return pattern
    if isinstance(pattern, dict):
        return pattern["regex"]
    return pattern.regex  # e.g. project_health.config.AutomatedSenderPattern


def is_automated_sender(sender: str | None, patterns: Any) -> bool:
    """Whether `sender` (a mailing-list `From:` address/display-name string,
    or a JIRA comment author username) matches any automated-sender pattern.

    `patterns` accepts plain regex strings, `{"regex": ...}` dicts, or
    `project_health.config.AutomatedSenderPattern` objects (or any object
    with a `.regex` attribute) -- whatever `config.automated_senders`
    deserializes to, without this module depending on `project_health.config`.

    Note: Pony Mail's month-digest endpoint (`classify/text_fetch.py`'s
    `PonyMailTextFetcher`) returns a *partially obfuscated* `From:` address
    (e.g. `yc...@gmail.com`) to deter scraping -- the display name and
    domain stay intact, but a pattern anchored on an exact, unobfuscated
    local-part (e.g. `^jira@`) will not reliably match. JIRA comment author
    usernames are never obfuscated. `projects/cassandra.yaml`'s
    `automated_senders:` patterns should be written with this in mind
    (domain/display-name anchors for mail; username anchors for JIRA).
    """
    if not sender:
        return False
    for pattern in patterns:
        if re.search(_pattern_regex(pattern), sender):
            return True
    return False
