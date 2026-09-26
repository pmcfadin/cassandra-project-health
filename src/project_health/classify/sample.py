"""Phase 2a Jev pilot sampler (issue #44; DECISIONS.md D18;
COMMUNITY-HEALTH.md §6.1).

Builds corpus v0: a deterministic, seeded sample of 150 "prevalence" items
(proportional to source volume and stratified by year) plus 100
"enrichment" items (a keyword/heuristic pre-filter targeting the five rare
labels), drawn from dev@ mailing-list messages and JIRA comments,
2017-01-01 through 2026-08-31. Message bodies are fetched and preprocessed
transiently, in memory, exactly as `classify/text_fetch.py` already does
for the labeling pipeline (D18: "Message bodies are fetched only for the
moment of classification or labeling and are never written to the public
repo or the `data` branch") -- this module writes the final, preprocessed
corpus JSONL only to a caller-supplied path, which the CLI points at a
clone of the private `pmcfadin/cassandra-project-health-benchmark` repo,
never this repo.

## The two strata (COMMUNITY-HEALTH.md §6.1)

- **Prevalence** (`select_prevalence`, target 150): a proportional,
  year-stratified random sample meant to reflect the corpus as it actually
  is. Source weights are dev@'s *exact* eligible count (the local Phase 1
  metadata cache is fully enumerated) against JIRA's *estimated* eligible
  count (see below) -- this is the only place an estimate enters the
  pipeline, and only to set the dev@/JIRA split, never to report a "how
  common is X" number on its own.
- **Enrichment** (`select_enrichment`, target 100): drawn only from
  candidates the keyword/heuristic pre-filter
  (`classify/enrichment_filters_v1.yaml`) flags for one of the five rare
  labels (`personal_attack`, `gatekeeping`, `dismissiveness`,
  `status_authority_invocation`, `sarcasm`). **This stratum is never used to
  estimate prevalence** -- `select_prevalence` never reads its output, and
  the two strata's item sets are kept disjoint (an item selected for
  prevalence is excluded from enrichment) so nothing is ever double-counted.
  Mixing them would bias prevalence upward for exactly the labels most
  capable of causing reputational harm if overstated (§6.1's own words).

## The JIRA metadata listing is budgeted, not exhaustive

Unlike dev@ (Phase 1's `collectors/ponymail.py` already backfills full
message metadata locally -- issue #44: "the live data branch's ponymail
backfill may be incomplete, so use a full local collection"), nothing in
this project has ever enumerated JIRA *comment* ids: `collectors/
jira_comments.py`'s existing collector only searches caller-supplied issue
keys for a fixed CI-evidence term list and stores matches, never a full
listing. ASF JIRA's CASSANDRA project has 20,000+ issues, and there is no
cheap, exact "list every comment across the whole project in a date
window" JIRA REST endpoint on this instance. `scan_jira_candidates`
therefore:

1. Counts candidate issues via one JQL search (`updated >= start`,
   `maxResults=0`).
2. Draws a small, seeded-random set of contiguous issue-key blocks spanning
   that candidate range (few search calls, not one per issue).
3. Lists every comment on each block's issues, reading each comment's body
   **once, in memory**, to compute eligibility and enrichment-filter hits,
   then discards the body -- the same "read once, never persist" shape
   `collectors/jira_comments.py`'s CI-evidence collector already uses for a
   different fixed term list. Only comment id, issue key, year, word count,
   language flag and enrichment-label hits are kept.
4. Stops once a call budget is spent (`JiraScanStats.budget_exhausted`).

The resulting JIRA comment count is real (every comment this function
enumerates, it enumerates in full via `updated`), but the *frame* is a
sample of issues, not every CASSANDRA issue -- so the sampler only uses
this scan's totals to *estimate* the dev@/JIRA prevalence split
(`JiraScanStats.estimated_total_eligible`), and always draws actual sampled
items from comments the scan actually saw. This limitation is recorded in
every run's manifest, never silently assumed away.

## Exclusions

- **Automated senders** (`projects/<id>.yaml`'s `automated_senders`):
  applied from the *unobfuscated* local metadata for dev@ (Pony Mail's live
  text API partially obfuscates `From:`, per `classify/text_fetch.py`) and
  from the JIRA comment author username for JIRA -- both via
  `classify.preprocess.is_automated_sender`.
- **Short messages**: fewer than `MIN_WORDS_AFTER_PREPROCESS` (5) words
  after the full `classify.preprocess.preprocess_text` pipeline runs.
- **Non-English messages**: `is_english_heuristic` below -- a documented,
  dependency-free heuristic (common-stopword presence, or a low non-ASCII
  alphabetic-character ratio), not a language-ID model. Appropriate for a
  250-item pilot; revisit if the full benchmark (COMMUNITY-HEALTH.md §6.1)
  needs a lower false-negative rate on non-English text than this achieves.
"""

from __future__ import annotations

import glob as _glob
import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import yaml

from project_health.classify.preprocess import is_automated_sender, preprocess_text
from project_health.classify.text_fetch import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_REQUEST_INTERVAL,
    DEFAULT_TIMEOUT,
    MailMessageRef,
    PonyMailTextFetcher,
    fetch_and_preprocess_mail,
)

# --- Pilot scope constants (D18) ---------------------------------------------

FRAME_START = date(2017, 1, 1)
FRAME_END = date(2026, 8, 31)
MIN_WORDS_AFTER_PREPROCESS = 5
PREVALENCE_TARGET = 150
ENRICHMENT_TARGET = 100
RARE_LABELS: tuple[str, ...] = (
    "personal_attack",
    "gatekeeping",
    "dismissiveness",
    "status_authority_invocation",
    "sarcasm",
)
SOURCE_MAILING_LIST = "mailing_list"
SOURCE_JIRA_COMMENT = "jira_comment"

DEFAULT_JIRA_ISSUE_BLOCKS = 10
DEFAULT_JIRA_BLOCK_SIZE = 100
DEFAULT_JIRA_MAX_CALLS = 1600
DEFAULT_JIRA_BASE_URL = "https://issues.apache.org/jira"

# --- Language heuristic -------------------------------------------------------

_ENGLISH_STOPWORDS = frozenset(
    {
        "the", "and", "to", "of", "is", "in", "for", "that", "this", "with",
        "a", "it", "on", "as", "are", "be", "was", "have", "not", "but",
        "if", "we", "you", "i", "at", "or", "an", "from", "by", "would",
        "should", "can", "could", "will", "there", "what", "when", "which",
        "patch", "commit", "test", "please", "thanks", "think", "issue",
    }
)
_WORD_RE = re.compile(r"[A-Za-z']+")
_NON_ASCII_ALPHA_RATIO_THRESHOLD = 0.15


def is_english_heuristic(text: str) -> bool:
    """Simple, documented non-English detector -- see module docstring.

    Two independent signals, either sufficient on its own:

    1. At least one common English stopword/technical-list term appears as
       a whole word (case-insensitive). Catches the overwhelming majority
       of English prose, including short technical messages.
    2. Fewer than 15% of the message's alphabetic characters are non-ASCII.
       Lets through English text with an occasional accented name or quoted
       foreign word while catching text written predominantly in another
       script or language.

    Not a language-ID model -- a documented, dependency-free heuristic
    appropriate for a 250-item pilot (module docstring).
    """
    if not text or not text.strip():
        return False
    lowered = text.lower()
    if any(w in _ENGLISH_STOPWORDS for w in _WORD_RE.findall(lowered)):
        return True
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return False
    non_ascii = sum(1 for c in alpha_chars if ord(c) > 127)
    return (non_ascii / len(alpha_chars)) < _NON_ASCII_ALPHA_RATIO_THRESHOLD


# --- Enrichment keyword/heuristic pre-filter ---------------------------------

EnrichmentFilterMap = dict[str, tuple["re.Pattern[str]", ...]]


def load_enrichment_filters(path: str | Path) -> EnrichmentFilterMap:
    """Load `classify/enrichment_filters_v1.yaml`'s `labels:` map into
    compiled, case-insensitive regexes, keyed by rare label id."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    filters: EnrichmentFilterMap = {}
    for label, terms in (raw.get("labels") or {}).items():
        filters[label] = tuple(re.compile(term, re.IGNORECASE) for term in terms)
    return filters


def enrichment_hits(text: str, filters: EnrichmentFilterMap) -> frozenset[str]:
    """The set of rare labels whose pre-filter terms match `text` (already
    preprocessed). Empty if none match."""
    return frozenset(
        label for label, patterns in filters.items() if any(p.search(text) for p in patterns)
    )


# --- Deterministic selection helpers ------------------------------------------


def _stable_rank(seed: int, namespace: str, item_id: str) -> str:
    return hashlib.sha256(f"{seed}:{namespace}:{item_id}".encode("utf-8")).hexdigest()


def deterministic_sample(seed: int, namespace: str, item_ids: Sequence[str], k: int) -> list[str]:
    """Pick `min(k, len(item_ids))` ids from `item_ids`, deterministically
    given `(seed, namespace)` and independent of `item_ids`'s input order:
    candidates are sorted by a stable per-id hash, not by mutating a shared
    `random.Random` sequence, so re-running against a differently-ordered
    (but same-membership) pool always yields the same pick."""
    if k <= 0:
        return []
    ranked = sorted(item_ids, key=lambda item_id: _stable_rank(seed, namespace, item_id))
    return ranked[:k]


def largest_remainder_allocate(target_total: int, weights: dict[str, float]) -> dict[str, int]:
    """Apportion `target_total` whole units across `weights`'s keys in
    proportion to weight (Hamilton/largest-remainder apportionment), so the
    result always sums to exactly `target_total` when `sum(weights) > 0`."""
    if target_total <= 0 or not weights:
        return {k: 0 for k in weights}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {k: 0 for k in weights}
    raw = {k: target_total * w / total_weight for k, w in weights.items()}
    floors = {k: int(v) for k, v in raw.items()}
    remainder = target_total - sum(floors.values())
    order = sorted(raw, key=lambda k: (-(raw[k] - floors[k]), k))
    for k in order[:remainder]:
        floors[k] += 1
    return floors


def allocate_with_capacity(
    target_total: int, weights: dict[str, float], capacity: dict[str, int]
) -> dict[str, int]:
    """Like `largest_remainder_allocate`, but each key is capped at
    `capacity[key]`; any shortfall is redistributed, in proportion to
    weight, to keys still under capacity, iterating until either
    `target_total` is fully placed or every key is at capacity. Callers
    must check `sum(result.values()) < target_total` themselves -- this
    never pads with unavailable items."""
    allocation = {k: 0 for k in weights}
    remaining_keys = {k: w for k, w in weights.items() if capacity.get(k, 0) > 0}
    to_place = target_total
    while to_place > 0 and remaining_keys:
        share = largest_remainder_allocate(to_place, remaining_keys)
        progressed = False
        for k, want in share.items():
            give = min(want, capacity[k] - allocation[k])
            if give > 0:
                allocation[k] += give
                to_place -= give
                progressed = True
            if allocation[k] >= capacity[k]:
                remaining_keys.pop(k, None)
        if not progressed:
            break
    return allocation


# --- dev@ frame (local Phase 1 metadata) --------------------------------------


@dataclass(frozen=True)
class DevFrameRow:
    """One dev@ message from the local Phase 1 metadata cache, already
    filtered to the frame window and automated senders."""

    message_id: str
    year: int
    year_month: str


def load_dev_metadata_frame(
    data_dir: str | Path,
    list_name: str,
    automated_sender_patterns: Iterable[Any],
    start: date = FRAME_START,
    end: date = FRAME_END,
) -> list[DevFrameRow]:
    """Load the authoritative dev@ sampling frame from the Phase 1
    `raw/ponymail/message` Parquet cache under `data_dir` (issue #44: "the
    live data branch's ponymail backfill may be incomplete, so use a full
    local collection"). Filters to `list_name`, `[start, end]` (inclusive),
    and drops automated senders using the cache's *unobfuscated*
    `sender_raw_value` column (more reliable than the live text API's
    obfuscated `From:`, per `classify/text_fetch.py`).
    """
    base = Path(data_dir) / "raw" / "ponymail" / "message"
    files = sorted(_glob.glob(str(base / "**" / "*.parquet"), recursive=True))
    if not files:
        raise FileNotFoundError(f"no dev@ message Parquet files found under {base}")

    frame = pl.concat([pl.read_parquet(f) for f in files])
    frame = frame.filter(pl.col("list") == list_name)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    frame = frame.filter((pl.col("occurred_at") >= start_dt) & (pl.col("occurred_at") <= end_dt))

    patterns = list(automated_sender_patterns)
    rows: list[DevFrameRow] = []
    cols = ["message_id", "occurred_at", "sender_raw_value"]
    for rec in frame.select(cols).iter_rows(named=True):
        if is_automated_sender(rec["sender_raw_value"], patterns):
            continue
        occurred_at = rec["occurred_at"]
        rows.append(
            DevFrameRow(
                message_id=rec["message_id"],
                year=occurred_at.year,
                year_month=f"{occurred_at.year:04d}-{occurred_at.month:02d}",
            )
        )
    return rows


@dataclass(frozen=True)
class DevCandidate:
    """One dev@ frame row after transient fetch + preprocessing, with
    eligibility and enrichment-filter results computed."""

    message_id: str
    year: int
    word_count: int
    is_english: bool
    eligible: bool
    hits: frozenset[str]
    archive_url: str


def scan_dev_candidates(
    fetcher: PonyMailTextFetcher,
    frame_rows: Sequence[DevFrameRow],
    domain: str,
    list_name: str,
    automated_sender_patterns: Iterable[Any],
    enrichment_filter_map: EnrichmentFilterMap,
) -> tuple[list[DevCandidate], dict[str, str], dict[str, str | None]]:
    """Fetch + preprocess every frame row's body transiently
    (`text_fetch.fetch_and_preprocess_mail` -- never persisted), then
    compute eligibility and enrichment-filter hits in memory. Returns
    `(candidates, text_by_id, parent_text_by_id)`; only corpus items the
    sampler actually selects ever leave this in-memory pair for a file.
    """
    refs = [MailMessageRef(list_name, domain, row.year_month, row.message_id) for row in frame_rows]
    states = fetch_and_preprocess_mail(fetcher, refs, automated_sender_patterns)
    year_by_id = {row.message_id: row.year for row in frame_rows}

    # Pony Mail's own opaque `mid` (used for the public archive permalink,
    # `https://lists.apache.org/thread/<mid>`) comes from the same month
    # digests `fetch_and_preprocess_mail` already fetched -- `fetch_month_raw`
    # hits the fetcher's per-month cache, so this costs no extra network call.
    year_months = {row.year_month for row in frame_rows if row.message_id in states}
    mid_by_message_id: dict[str, str] = {}
    for year_month in year_months:
        for message_id, record in fetcher.fetch_month_raw(list_name, domain, year_month).items():
            if message_id in states and record.get("mid"):
                mid_by_message_id[message_id] = record["mid"]

    candidates: list[DevCandidate] = []
    text_by_id: dict[str, str] = {}
    parent_text_by_id: dict[str, str | None] = {}
    for message_id, state in states.items():
        text = state["message"]["text"]
        word_count = len(text.split())
        english = is_english_heuristic(text)
        eligible = word_count >= MIN_WORDS_AFTER_PREPROCESS and english
        hits = enrichment_hits(text, enrichment_filter_map) if eligible else frozenset()
        mid = mid_by_message_id.get(message_id)
        archive_url = f"https://lists.apache.org/thread/{mid}" if mid else ""
        candidates.append(
            DevCandidate(
                message_id=message_id,
                year=year_by_id[message_id],
                word_count=word_count,
                is_english=english,
                eligible=eligible,
                hits=hits,
                archive_url=archive_url,
            )
        )
        if eligible:
            text_by_id[message_id] = text
            parent = state["parent"]
            parent_text_by_id[message_id] = parent["text"] if parent else None
    return candidates, text_by_id, parent_text_by_id


# --- JIRA budgeted metadata listing + scan ------------------------------------

_JIRA_SEARCH_PATH = "/rest/api/2/search"
_JIRA_BACKOFF_BASE = 0.5
_JIRA_BACKOFF_CAP = 20.0


def _parse_jira_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").date()
    except ValueError:
        return None


class _PacedJiraScanClient:
    """Minimal paced/retrying JIRA REST client for the pilot sampler's
    budgeted, metadata-only comment listing. A small, self-contained class
    (this project's stated convention for these near-identical-but-
    independent paced clients -- see `classify/text_fetch.py`'s
    `_PacedClient` docstring -- is one small class per module, not a shared
    abstraction across `collectors/jira_comments.py`/`classify/
    text_fetch.py`/here).
    """

    def __init__(
        self,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._last_request_at: float | None = None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=timeout
        )
        # Every physical HTTP attempt, including retries -- what
        # `scan_jira_candidates`'s `max_calls` budget counts against.
        self.call_count = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "_PacedJiraScanClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _respect_rate_limit(self) -> None:
        if self._min_request_interval <= 0:
            return
        now = self._clock()
        if self._last_request_at is not None:
            wait = self._min_request_interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
        self._last_request_at = self._clock()

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = None
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            exp = min(_JIRA_BACKOFF_CAP, _JIRA_BACKOFF_BASE * (2 ** (attempt - 1)))
            delay = exp + random.uniform(0, exp * 0.25)
        self._sleep_fn(delay)

    def get(self, path: str, params: dict) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            self._respect_rate_limit()
            self.call_count += 1
            try:
                response = self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_retries:
                    raise RuntimeError(
                        f"request to {path} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue
            if response.status_code == 404:
                return response
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise RuntimeError(
                        f"request to {path} failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue
            response.raise_for_status()
            return response


@dataclass(frozen=True)
class JiraCandidate:
    """One JIRA comment found during the budgeted scan, after in-memory
    preprocessing and eligibility/enrichment checks. Never carries a body."""

    comment_id: str
    issue_key: str
    year: int
    word_count: int
    is_english: bool
    eligible: bool
    hits: frozenset[str]
    archive_url: str


@dataclass(frozen=True)
class JiraScanStats:
    candidate_issue_total: int
    issues_scanned: int
    comments_seen: int
    comments_in_window: int
    comments_eligible: int
    api_calls: int
    budget_exhausted: bool
    estimated_total_eligible: float


def scan_jira_candidates(
    client: _PacedJiraScanClient,
    seed: int,
    project_key: str,
    automated_sender_patterns: Iterable[Any],
    enrichment_filter_map: EnrichmentFilterMap,
    start: date = FRAME_START,
    end: date = FRAME_END,
    issue_blocks: int = DEFAULT_JIRA_ISSUE_BLOCKS,
    block_size: int = DEFAULT_JIRA_BLOCK_SIZE,
    max_calls: int = DEFAULT_JIRA_MAX_CALLS,
    base_url: str = DEFAULT_JIRA_BASE_URL,
) -> tuple[list[JiraCandidate], dict[str, str], dict[str, str | None], JiraScanStats]:
    """Budgeted, metadata-only JIRA comment frame builder -- see module
    docstring for the full rationale. Returns `(candidates, text_by_id,
    parent_text_by_id, stats)`.
    """
    total_resp = client.get(
        _JIRA_SEARCH_PATH,
        {"jql": f'project = {project_key} AND updated >= "{start.isoformat()}"', "maxResults": 0},
    )
    candidate_issue_total = (
        total_resp.json().get("total", 0) if total_resp.status_code == 200 else 0
    )

    issue_keys: dict[str, None] = {}
    if candidate_issue_total > 0:
        max_offset = max(candidate_issue_total - block_size, 0)
        rng = random.Random(f"{seed}:jira_issue_blocks")
        if max_offset > 0:
            num_offsets = min(issue_blocks, max_offset + 1)
            offsets = sorted(rng.sample(range(0, max_offset + 1), k=num_offsets))
        else:
            offsets = [0]
        for offset in offsets:
            if client.call_count >= max_calls:
                break
            resp = client.get(
                _JIRA_SEARCH_PATH,
                {
                    "jql": (
                        f'project = {project_key} AND updated >= "{start.isoformat()}" '
                        "ORDER BY key ASC"
                    ),
                    "startAt": offset,
                    "maxResults": block_size,
                    "fields": "key",
                },
            )
            if resp.status_code != 200:
                continue
            for issue in resp.json().get("issues", []):
                issue_keys.setdefault(issue["key"], None)

    candidates: list[JiraCandidate] = []
    text_by_id: dict[str, str] = {}
    parent_text_by_id: dict[str, str | None] = {}
    comments_seen = 0
    comments_in_window = 0
    comments_eligible = 0
    issues_scanned = 0
    budget_exhausted = False

    for issue_key in issue_keys:
        if client.call_count >= max_calls:
            budget_exhausted = True
            break
        issues_scanned += 1
        start_at = 0
        ordered_comments: list[dict] = []
        while True:
            if client.call_count >= max_calls:
                budget_exhausted = True
                break
            resp = client.get(
                f"/rest/api/2/issue/{issue_key}/comment",
                {"startAt": start_at, "maxResults": 50},
            )
            if resp.status_code == 404:
                break
            payload = resp.json()
            page = payload.get("comments", [])
            ordered_comments.extend(page)
            total = payload.get("total", len(page))
            start_at += len(page)
            if not page or start_at >= total:
                break
        if budget_exhausted:
            break

        for i, comment in enumerate(ordered_comments):
            comments_seen += 1
            created_date = _parse_jira_date(comment.get("created"))
            body = comment.get("body") or ""
            # Read once, in memory; never persisted (mirrors
            # collectors/jira_comments.py's CI-evidence collector).
            text = preprocess_text(body, "jira_comment")
            prev_text = (
                preprocess_text(ordered_comments[i - 1].get("body") or "", "jira_comment")
                if i > 0
                else None
            )
            if created_date is None or not (start <= created_date <= end):
                continue
            comments_in_window += 1
            author = (comment.get("author") or {}).get("name")
            if is_automated_sender(author, automated_sender_patterns):
                continue
            comment_id = str(comment.get("id"))
            word_count = len(text.split())
            english = is_english_heuristic(text)
            eligible = word_count >= MIN_WORDS_AFTER_PREPROCESS and english
            hits = enrichment_hits(text, enrichment_filter_map) if eligible else frozenset()
            archive_url = (
                f"{base_url}/browse/{issue_key}?focusedCommentId={comment_id}#comment-{comment_id}"
            )
            candidates.append(
                JiraCandidate(
                    comment_id=comment_id,
                    issue_key=issue_key,
                    year=created_date.year,
                    word_count=word_count,
                    is_english=english,
                    eligible=eligible,
                    hits=hits,
                    archive_url=archive_url,
                )
            )
            if eligible:
                comments_eligible += 1
                text_by_id[comment_id] = text
                parent_text_by_id[comment_id] = prev_text

    estimated_total_eligible = (
        (candidate_issue_total / issues_scanned) * comments_eligible if issues_scanned else 0.0
    )
    stats = JiraScanStats(
        candidate_issue_total=candidate_issue_total,
        issues_scanned=issues_scanned,
        comments_seen=comments_seen,
        comments_in_window=comments_in_window,
        comments_eligible=comments_eligible,
        api_calls=client.call_count,
        budget_exhausted=budget_exhausted,
        estimated_total_eligible=estimated_total_eligible,
    )
    return candidates, text_by_id, parent_text_by_id, stats


# --- Prevalence + enrichment selection ----------------------------------------


def select_prevalence(
    seed: int,
    dev_eligible: Sequence[DevCandidate],
    jira_eligible: Sequence[JiraCandidate],
    jira_stats: JiraScanStats,
    target: int = PREVALENCE_TARGET,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Select the prevalence stratum: `(dev_message_ids, jira_comment_ids)`,
    proportional to source volume and stratified by year within each source
    (COMMUNITY-HEALTH.md §6.1). See module docstring for the dev@/JIRA
    weighting rationale.
    """
    dev_weight = float(len(dev_eligible))
    jira_weight = float(jira_stats.estimated_total_eligible)
    source_alloc = (
        largest_remainder_allocate(target, {"dev": dev_weight, "jira": jira_weight})
        if (dev_weight + jira_weight) > 0
        else {"dev": 0, "jira": 0}
    )

    def _year_groups(items: Sequence[Any], id_attr: str) -> dict[int, list[str]]:
        groups: dict[int, list[str]] = {}
        for item in items:
            groups.setdefault(item.year, []).append(getattr(item, id_attr))
        return groups

    dev_groups = _year_groups(dev_eligible, "message_id")
    jira_groups = _year_groups(jira_eligible, "comment_id")

    def _pick(groups: dict[int, list[str]], n: int, namespace: str) -> list[str]:
        if not groups or n <= 0:
            return []
        weights = {str(y): float(len(ids)) for y, ids in groups.items()}
        capacity = {str(y): len(ids) for y, ids in groups.items()}
        year_alloc = allocate_with_capacity(n, weights, capacity)
        picked: list[str] = []
        for y_str, count in year_alloc.items():
            y = int(y_str)
            picked.extend(deterministic_sample(seed, f"{namespace}:{y}", groups[y], count))
        return picked

    dev_ids = _pick(dev_groups, source_alloc.get("dev", 0), "prevalence:dev")
    jira_ids = _pick(jira_groups, source_alloc.get("jira", 0), "prevalence:jira")

    stats = {
        "target": target,
        "achieved": len(dev_ids) + len(jira_ids),
        "source_allocation_target": source_alloc,
        "source_weights": {"dev": dev_weight, "jira": jira_weight},
        "dev_selected": len(dev_ids),
        "jira_selected": len(jira_ids),
        "dev_by_year": {y: len(ids) for y, ids in dev_groups.items()},
        "jira_by_year_sampled": {y: len(ids) for y, ids in jira_groups.items()},
    }
    return dev_ids, jira_ids, stats


def select_enrichment(
    seed: int,
    dev_candidates: Sequence[DevCandidate],
    jira_candidates: Sequence[JiraCandidate],
    excluded_dev_ids: set[str],
    excluded_jira_ids: set[str],
    target: int = ENRICHMENT_TARGET,
    labels: Sequence[str] = RARE_LABELS,
) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    """Select the enrichment stratum: up to `target` items, drawn only from
    the keyword/heuristic pre-filter's hits, disjoint from the prevalence
    stratum's own selections, and never used to estimate prevalence (module
    docstring / COMMUNITY-HEALTH.md §6.1). A candidate matching several rare
    labels is bucketed under its first match in `labels`'s fixed order, so
    it is counted once, not once per label. Returns
    `(selected, stats)` where `selected` is `(source, item_ref, label)`.
    """
    buckets: dict[str, list[tuple[str, str]]] = {label: [] for label in labels}
    eligible_scanned = {SOURCE_MAILING_LIST: 0, SOURCE_JIRA_COMMENT: 0}

    for item in dev_candidates:
        if not item.eligible:
            continue
        eligible_scanned[SOURCE_MAILING_LIST] += 1
        if item.message_id in excluded_dev_ids or not item.hits:
            continue
        label = next(candidate for candidate in labels if candidate in item.hits)
        buckets[label].append((SOURCE_MAILING_LIST, item.message_id))

    for item in jira_candidates:
        if not item.eligible:
            continue
        eligible_scanned[SOURCE_JIRA_COMMENT] += 1
        if item.comment_id in excluded_jira_ids or not item.hits:
            continue
        label = next(candidate for candidate in labels if candidate in item.hits)
        buckets[label].append((SOURCE_JIRA_COMMENT, item.comment_id))

    weights = {label: float(len(items)) for label, items in buckets.items()}
    capacity = {label: len(items) for label, items in buckets.items()}
    label_alloc = (
        allocate_with_capacity(target, weights, capacity)
        if sum(capacity.values()) > 0
        else dict.fromkeys(labels, 0)
    )

    selected: list[tuple[str, str, str]] = []
    for label, count in label_alloc.items():
        # `{ref: source}`, not `dict(buckets[label])` -- the latter would key
        # by `source` (which repeats across every item from the same
        # source) and silently collapse the pool to one entry per source.
        source_by_ref = {ref: source for source, ref in buckets[label]}
        pool_ids = list(source_by_ref)
        for ref in deterministic_sample(seed, f"enrichment:{label}", pool_ids, count):
            selected.append((source_by_ref[ref], ref, label))

    hits_by_source = {SOURCE_MAILING_LIST: 0, SOURCE_JIRA_COMMENT: 0}
    for items in buckets.values():
        for source, _ref in items:
            hits_by_source[source] += 1

    hit_rate_by_source = {
        source: (
            (hits_by_source[source] / eligible_scanned[source]) if eligible_scanned[source] else 0.0
        )
        for source in (SOURCE_MAILING_LIST, SOURCE_JIRA_COMMENT)
    }

    stats = {
        "target": target,
        "achieved": len(selected),
        "label_candidate_counts": {label: len(v) for label, v in buckets.items()},
        "label_selected_counts": dict(label_alloc),
        "eligible_scanned": eligible_scanned,
        "hit_rate_by_source": hit_rate_by_source,
    }
    return selected, stats


# --- Corpus assembly + I/O -----------------------------------------------------


@dataclass(frozen=True)
class CorpusItem:
    item_id: str
    stratum: str  # "prevalence" | "enrichment"
    source: str  # "mailing_list" | "jira_comment"
    archive_url: str
    year: int
    message_text: str
    parent_text: str | None
    checksum: str

    def to_jsonl_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "stratum": self.stratum,
            "source": self.source,
            "archive_url": self.archive_url,
            "year": self.year,
            "message": {"text": self.message_text},
            "parent": {"text": self.parent_text} if self.parent_text is not None else None,
            "checksum": self.checksum,
        }


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_corpus(
    dev_prevalence_ids: Sequence[str],
    jira_prevalence_ids: Sequence[str],
    enrichment_selected: Sequence[tuple[str, str, str]],
    dev_meta_by_id: dict[str, DevCandidate],
    jira_meta_by_id: dict[str, JiraCandidate],
    dev_text_by_id: dict[str, str],
    dev_parent_by_id: dict[str, str | None],
    jira_text_by_id: dict[str, str],
    jira_parent_by_id: dict[str, str | None],
) -> list[CorpusItem]:
    """Assemble the final corpus v0 item list from selected ids plus the
    text/parent-text already captured transiently during scanning."""
    items: list[CorpusItem] = []
    for message_id in dev_prevalence_ids:
        meta = dev_meta_by_id[message_id]
        text = dev_text_by_id[message_id]
        items.append(
            CorpusItem(
                item_id=f"mail:{message_id}",
                stratum="prevalence",
                source=SOURCE_MAILING_LIST,
                archive_url=meta.archive_url,
                year=meta.year,
                message_text=text,
                parent_text=dev_parent_by_id.get(message_id),
                checksum=_checksum(text),
            )
        )
    for comment_id in jira_prevalence_ids:
        meta = jira_meta_by_id[comment_id]
        text = jira_text_by_id[comment_id]
        items.append(
            CorpusItem(
                item_id=f"jira:{meta.issue_key}:{comment_id}",
                stratum="prevalence",
                source=SOURCE_JIRA_COMMENT,
                archive_url=meta.archive_url,
                year=meta.year,
                message_text=text,
                parent_text=jira_parent_by_id.get(comment_id),
                checksum=_checksum(text),
            )
        )
    for source, ref, _label in enrichment_selected:
        if source == SOURCE_MAILING_LIST:
            meta = dev_meta_by_id[ref]
            text = dev_text_by_id[ref]
            items.append(
                CorpusItem(
                    item_id=f"mail:{ref}",
                    stratum="enrichment",
                    source=SOURCE_MAILING_LIST,
                    archive_url=meta.archive_url,
                    year=meta.year,
                    message_text=text,
                    parent_text=dev_parent_by_id.get(ref),
                    checksum=_checksum(text),
                )
            )
        else:
            meta = jira_meta_by_id[ref]
            text = jira_text_by_id[ref]
            items.append(
                CorpusItem(
                    item_id=f"jira:{meta.issue_key}:{ref}",
                    stratum="enrichment",
                    source=SOURCE_JIRA_COMMENT,
                    archive_url=meta.archive_url,
                    year=meta.year,
                    message_text=text,
                    parent_text=jira_parent_by_id.get(ref),
                    checksum=_checksum(text),
                )
            )
    return items


def write_corpus_jsonl(items: Sequence[CorpusItem], path: str | Path) -> str:
    """Write `items` as JSONL to `path` -- the caller's own private storage
    (D18: "the pilot corpus and its labels stay private"). This module
    deliberately does not import `text_fetch.py`'s `PrivacyGuardError`
    backstop: writing the final corpus to the private benchmark repo is
    exactly the persistence D18 permits, not the case that guard exists to
    prevent. Returns the sha256 hex digest of the written file's bytes.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item.to_jsonl_dict(), ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(
    seed: int,
    items: Sequence[CorpusItem],
    prevalence_stats: dict[str, Any],
    enrichment_stats: dict[str, Any],
    jira_scan_stats: JiraScanStats,
    corpus_checksum: str,
) -> dict[str, Any]:
    """Build the run manifest: counts per stratum/source/year, the seed,
    the corpus checksum, and the pre-filter hit rates -- exactly the fields
    D18/issue #44 allow into the public repo (never text, never per-item
    ids in the public rendering -- see `render_public_manifest_markdown`).
    """
    counts_by_stratum_source_year: dict[str, int] = {}
    for item in items:
        key = f"{item.stratum}/{item.source}/{item.year}"
        counts_by_stratum_source_year[key] = counts_by_stratum_source_year.get(key, 0) + 1

    return {
        "corpus_version": "v0",
        "seed": seed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frame_window": {"start": FRAME_START.isoformat(), "end": FRAME_END.isoformat()},
        "total_items": len(items),
        "counts_by_stratum_source_year": counts_by_stratum_source_year,
        "prevalence": prevalence_stats,
        "enrichment": enrichment_stats,
        "jira_scan": {
            "candidate_issue_total": jira_scan_stats.candidate_issue_total,
            "issues_scanned": jira_scan_stats.issues_scanned,
            "comments_seen": jira_scan_stats.comments_seen,
            "comments_in_window": jira_scan_stats.comments_in_window,
            "comments_eligible": jira_scan_stats.comments_eligible,
            "api_calls": jira_scan_stats.api_calls,
            "budget_exhausted": jira_scan_stats.budget_exhausted,
            "estimated_total_eligible_in_window": jira_scan_stats.estimated_total_eligible,
            "note": (
                "JIRA comment volume is estimated from a budgeted random sample of "
                "issues (see classify/sample.py module docstring), not an exhaustive "
                "listing; used only to weight the dev@/JIRA prevalence split."
            ),
        },
        "corpus_checksum_sha256": corpus_checksum,
    }


def render_public_manifest_markdown(manifest: dict[str, Any]) -> str:
    """Render `docs/pilot/corpus-v0-manifest.md`'s content: counts per
    stratum/source/year, the seed, the corpus checksum, and pre-filter hit
    rates. No message text and no message/comment ids -- only aggregate
    counts, matching what issue #44 allows into the public repo.
    """
    lines = [
        "# Pilot corpus v0 manifest",
        "",
        "Generated by `project-health pilot-sample` (issue #44; DECISIONS.md D18; "
        "COMMUNITY-HEALTH.md §6.1). This file holds only aggregate counts -- no "
        "message text and no message/comment ids ever leave the private "
        "`pmcfadin/cassandra-project-health-benchmark` repo (D18).",
        "",
        f"- **Corpus version:** {manifest['corpus_version']}",
        f"- **Seed:** {manifest['seed']}",
        f"- **Frame window:** {manifest['frame_window']['start']} to "
        f"{manifest['frame_window']['end']}",
        f"- **Generated at:** {manifest['generated_at']}",
        f"- **Total items:** {manifest['total_items']}",
        f"- **Corpus checksum (sha256):** `{manifest['corpus_checksum_sha256']}`",
        "",
        "## Counts by stratum / source / year",
        "",
        "| Stratum | Source | Year | Count |",
        "|---|---|---|---|",
    ]
    for key in sorted(manifest["counts_by_stratum_source_year"]):
        stratum, source, year = key.split("/")
        count = manifest["counts_by_stratum_source_year"][key]
        lines.append(f"| {stratum} | {source} | {year} | {count} |")

    prevalence = manifest["prevalence"]
    lines += [
        "",
        "## Prevalence stratum",
        "",
        f"- Target: {prevalence['target']}, achieved: {prevalence['achieved']}",
        f"- Source split target (dev@ / JIRA): {prevalence['source_allocation_target']}",
        f"- Source weights used for the split (dev@ exact, JIRA estimated): "
        f"{prevalence['source_weights']}",
    ]

    enrichment = manifest["enrichment"]
    lines += [
        "",
        "## Enrichment stratum (rare-label keyword/heuristic pre-filter)",
        "",
        "**Not used to estimate prevalence** (COMMUNITY-HEALTH.md §6.1) and unreviewed "
        "(OPEN-QUESTIONS.md #11 -- flagged for a second-person review of the filter's "
        "candidate set before this stratum is treated as final).",
        "",
        f"- Target: {enrichment['target']}, achieved: {enrichment['achieved']}",
        "",
        "| Rare label | Candidates found | Selected |",
        "|---|---|---|",
    ]
    for label in RARE_LABELS:
        lines.append(
            f"| {label} | {enrichment['label_candidate_counts'].get(label, 0)} "
            f"| {enrichment['label_selected_counts'].get(label, 0)} |"
        )
    lines += [
        "",
        "Pre-filter hit rate (candidates matched / eligible messages scanned), by source:",
        "",
        "| Source | Eligible scanned | Hit rate |",
        "|---|---|---|",
    ]
    for source in (SOURCE_MAILING_LIST, SOURCE_JIRA_COMMENT):
        scanned = enrichment["eligible_scanned"].get(source, 0)
        rate = enrichment["hit_rate_by_source"].get(source, 0.0)
        lines.append(f"| {source} | {scanned} | {rate:.4f} |")

    jira_scan = manifest["jira_scan"]
    lines += [
        "",
        "## JIRA comment metadata listing (budgeted, not exhaustive)",
        "",
        f"- Candidate issues in window (`updated >= {manifest['frame_window']['start']}`): "
        f"{jira_scan['candidate_issue_total']}",
        f"- Issues scanned this run: {jira_scan['issues_scanned']}",
        f"- Comments seen / in window / eligible: {jira_scan['comments_seen']} / "
        f"{jira_scan['comments_in_window']} / {jira_scan['comments_eligible']}",
        f"- API calls used: {jira_scan['api_calls']}"
        + (" (budget exhausted)" if jira_scan["budget_exhausted"] else ""),
        f"- Estimated total eligible JIRA comments in window: "
        f"{jira_scan['estimated_total_eligible_in_window']:.1f}",
        f"- {jira_scan['note']}",
    ]
    return "\n".join(lines) + "\n"


# --- End-to-end orchestration (used by the CLI and directly by tests) --------


@dataclass(frozen=True)
class PilotSampleResult:
    items: list[CorpusItem]
    manifest: dict[str, Any]
    corpus_path: Path
    manifest_path: Path
    public_manifest_markdown: str


def run_pilot_sample(
    *,
    data_dir: str | Path,
    seed: int,
    domain: str,
    list_name: str,
    jira_base_url: str,
    jira_project_key: str,
    automated_sender_patterns: Iterable[Any],
    enrichment_filters_path: str | Path,
    corpus_output_path: str | Path,
    manifest_output_path: str | Path,
    ponymail_fetcher: PonyMailTextFetcher | None = None,
    jira_client: _PacedJiraScanClient | None = None,
    jira_issue_blocks: int = DEFAULT_JIRA_ISSUE_BLOCKS,
    jira_block_size: int = DEFAULT_JIRA_BLOCK_SIZE,
    jira_max_calls: int = DEFAULT_JIRA_MAX_CALLS,
    start: date = FRAME_START,
    end: date = FRAME_END,
) -> PilotSampleResult:
    """End-to-end pilot sampler orchestration, used by `project-health
    pilot-sample` and directly by tests (with an injected `ponymail_fetcher`/
    `jira_client` built on `httpx.MockTransport`). Builds the dev@ + JIRA
    frames, scans for eligibility/enrichment hits, selects both strata,
    writes the corpus JSONL + a full manifest to `corpus_output_path`/
    `manifest_output_path`, and renders the public manifest markdown --
    but never decides *where* those paths live. The CLI points them at a
    clone of the private `pmcfadin/cassandra-project-health-benchmark` repo
    (D18); this function has no opinion about that and would happily write
    anywhere it's told, which is exactly why the caller's path choice is
    what D18's privacy guarantee actually rests on.
    """
    enrichment_filter_map = load_enrichment_filters(enrichment_filters_path)
    patterns = list(automated_sender_patterns)

    dev_frame = load_dev_metadata_frame(data_dir, list_name, patterns, start, end)

    owns_ponymail = ponymail_fetcher is None
    fetcher = ponymail_fetcher or PonyMailTextFetcher()
    try:
        dev_candidates, dev_text_by_id, dev_parent_by_id = scan_dev_candidates(
            fetcher, dev_frame, domain, list_name, patterns, enrichment_filter_map
        )
    finally:
        if owns_ponymail:
            fetcher.close()

    owns_jira = jira_client is None
    client = jira_client or _PacedJiraScanClient(jira_base_url)
    try:
        jira_candidates, jira_text_by_id, jira_parent_by_id, jira_stats = scan_jira_candidates(
            client,
            seed,
            jira_project_key,
            patterns,
            enrichment_filter_map,
            start=start,
            end=end,
            issue_blocks=jira_issue_blocks,
            block_size=jira_block_size,
            max_calls=jira_max_calls,
            base_url=jira_base_url,
        )
    finally:
        if owns_jira:
            client.close()

    dev_eligible = [c for c in dev_candidates if c.eligible]
    jira_eligible = [c for c in jira_candidates if c.eligible]

    dev_prevalence_ids, jira_prevalence_ids, prevalence_stats = select_prevalence(
        seed, dev_eligible, jira_eligible, jira_stats
    )
    enrichment_selected, enrichment_stats = select_enrichment(
        seed,
        dev_eligible,
        jira_eligible,
        excluded_dev_ids=set(dev_prevalence_ids),
        excluded_jira_ids=set(jira_prevalence_ids),
    )

    dev_meta_by_id = {c.message_id: c for c in dev_candidates}
    jira_meta_by_id = {c.comment_id: c for c in jira_candidates}

    items = build_corpus(
        dev_prevalence_ids,
        jira_prevalence_ids,
        enrichment_selected,
        dev_meta_by_id,
        jira_meta_by_id,
        dev_text_by_id,
        dev_parent_by_id,
        jira_text_by_id,
        jira_parent_by_id,
    )

    corpus_path = Path(corpus_output_path)
    corpus_checksum = write_corpus_jsonl(items, corpus_path)

    manifest = build_manifest(
        seed, items, prevalence_stats, enrichment_stats, jira_stats, corpus_checksum
    )
    manifest_path = Path(manifest_output_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return PilotSampleResult(
        items=items,
        manifest=manifest,
        corpus_path=corpus_path,
        manifest_path=manifest_path,
        public_manifest_markdown=render_public_manifest_markdown(manifest),
    )
