"""Transient text fetch for the Phase 2a Jev pilot (issue #43; DECISIONS.md D18,
D17; COMMUNITY-HEALTH.md §4.1).

Fetches dev@ message bodies (Pony Mail) and JIRA comment bodies (ASF JIRA
REST API) **on demand, for a caller-supplied list of message/comment
references only** -- never a blanket crawl. Bodies are fetched, preprocessed
(`classify/preprocess.py`), and handed back as in-memory objects matching
`questions_v1.yaml`'s `state_schema` exactly:
``{"message": {"text", "source"}, "parent": {"text"} | None}``. Nothing in
this module writes text anywhere -- persistence, if any, is entirely the
caller's decision (D18: "Message bodies are fetched only for the moment of
classification or labeling and are never written to the public repo or the
`data` branch... Pilot texts live only in the private corpus"). This module
additionally *refuses* to write text under the public repo checkout or a
`data` directory at all (`guard_write_path`/`write_text` below), as a
defense-in-depth backstop for a caller that tries anyway.

## Verified endpoints (live-checked 2026-09-25, this issue)

- **Pony Mail month digest**, `GET /api/stats.lua?list=<list>&domain=<domain>
  &d=<YYYY-MM>` -> 200 JSON, `{"emails": [{"message-id", "mid", "from",
  "in-reply-to", "body", ...}, ...]}` -- confirmed live against
  `dev@cassandra.apache.org`'s current month: every message in the month is
  returned in one call, body included, keyed by the same RFC 5322
  `Message-ID` the already-merged `collectors/ponymail.py` stores in its
  `message.message_id` column. This is the endpoint this module actually
  uses (not the bare `stats.lua` the merged metadata collector uses for
  month-range discovery, and not `mbox.lua` -- this one hands back
  already-parsed JSON with bodies, so no MIME parsing is needed here).
  `hits` and `len(emails)` matched exactly in the live check (175/175), so a
  single month is fetched in one request with no pagination.
- `GET /api/email.lua?id=<mid>` -> 200 JSON (single message, keyed by Pony
  Mail's own opaque `mid`, confirmed 404 for an unknown id) and
  `GET /api/thread.lua?id=<mid>` (subtree rooted at `mid`, not ancestors)
  were also verified live and work, but are **not used**: fetching a whole
  month once is cheaper than one `email.lua` call per message when several
  of a run's requested ids fall in the same month (as they do for a
  contiguous pilot sample), and it is what makes parent-message lookup by
  RFC `Message-ID` possible at all -- `email.lua`/`thread.lua` are keyed by
  Pony Mail's own `mid`, which this project's Phase 1 schema never stores.
- Pony Mail's month digest returns a **partially obfuscated** sender address
  (e.g. `"Yifan Cai <yc...@gmail.com>"` for `yc25code@gmail.com`) to deter
  scraping. See `preprocess.is_automated_sender`'s docstring for what this
  means for `automated_senders` pattern design.
- **JIRA comment listing**, `GET /rest/api/2/issue/{key}/comment` (the same
  endpoint `collectors/jira_comments.py` already uses) -- paginated via
  `startAt`/`maxResults`, ordered oldest-first by `created` (JIRA's default),
  which is what lets `resolve_parent` find "the previous comment on the same
  issue" without a second lookup. A single-comment endpoint,
  `GET /rest/api/2/issue/{key}/comment/{id}`, was also verified live and
  works, but is not used here for the same reason as `email.lua` above: this
  module needs the full ordered list anyway to resolve parents, so fetching
  it once per issue (cached per fetcher instance) is strictly cheaper than
  one request per requested comment id plus a second request for its parent.

## Pacing and retries

Both fetchers mirror `collectors/ponymail.py`/`collectors/jira_comments.py`:
exponential backoff with jitter on 429/5xx/timeout/transport errors, capped
retries, and a minimum interval between request starts (default 0.5s = 2
req/s, DATA-SOURCES.md §4's "poll politely... no parallel fan-out"). Both
accept an injectable `httpx.BaseTransport` so tests never touch the network
(`tests/test_text_fetch.py` uses `httpx.MockTransport`; the suite-wide
network block in `tests/conftest.py` would fail any real request regardless).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from project_health.classify.preprocess import is_automated_sender, preprocess_text

# --- Tuning constants (mirrors collectors/ponymail.py, collectors/jira_comments.py) --

DEFAULT_MAX_RETRIES = 5
# ≤2 req/s politeness cap (DATA-SOURCES.md §4; issue #43).
DEFAULT_MIN_REQUEST_INTERVAL = 0.5
DEFAULT_TIMEOUT = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 20.0

DEFAULT_PONYMAIL_BASE_URL = "https://lists.apache.org"
DEFAULT_JIRA_PAGE_SIZE = 50


class FetchError(Exception):
    """Raised when a text-fetch request fails after exhausting all retries."""


# --- Privacy guard (D1, D18: transient text only) ---------------------------


class PrivacyGuardError(RuntimeError):
    """Raised when code tries to persist Phase 2a text somewhere it must
    never go: the public repo checkout, or any `data` directory (D1/D18 --
    fetched/preprocessed text is transient; only the caller's own private
    storage, e.g. the `pmcfadin/cassandra-project-health-benchmark` repo,
    may persist it)."""


def _repo_root() -> Path:
    # src/project_health/classify/text_fetch.py -> classify -> project_health
    # -> src -> <repo root>.
    return Path(__file__).resolve().parents[3]


def guard_write_path(path: str | Path, *, repo_root: Path | None = None) -> Path:
    """Raise `PrivacyGuardError` if `path` resolves under the public repo
    checkout or any directory literally named `data`; otherwise return the
    resolved path.

    This is a defense-in-depth backstop, not the primary control: the
    primary control is that no function in this module ever writes text
    anywhere on its own (fetch/preprocess return in-memory objects only).
    `write_text` below is the one function that writes at all, and it always
    calls this guard first.
    """
    resolved = Path(path).expanduser().resolve()
    root = (repo_root if repo_root is not None else _repo_root()).resolve()
    if resolved == root or root in resolved.parents:
        raise PrivacyGuardError(
            f"refusing to write text under the public repo checkout ({root}): {resolved}"
        )
    if "data" in resolved.parts:
        raise PrivacyGuardError(f"refusing to write text under a 'data' directory: {resolved}")
    return resolved


def write_text(path: str | Path, text: str, *, repo_root: Path | None = None) -> None:
    """Write `text` to `path`, after `guard_write_path` clears it.

    Provided only so the privacy guard has something concrete to enforce
    against (and a test can prove it enforces it) -- this module's own
    fetch/preprocess functions never call it. The pilot's actual persistence
    (issue #44, the private benchmark repo) is the caller's own code, outside
    this module.
    """
    resolved = guard_write_path(path, repo_root=repo_root)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")


# --- Shared paced/retrying HTTP client --------------------------------------


class _PacedClient:
    """Retry/backoff + ≤2 req/s pacing shared by both fetchers below.

    Deliberately a plain internal base class within this one module (not a
    cross-module shared class) -- `collectors/jira_comments.py` makes the
    same call for its own similarly-shaped collector: different endpoints,
    no other shared state, not worth a shared abstraction outside one file.
    """

    def __init__(
        self,
        base_url: str,
        transport: httpx.BaseTransport | None,
        max_retries: int,
        min_request_interval: float,
        sleep_fn: Callable[[float], None],
        clock: Callable[[], float],
        timeout: float,
    ) -> None:
        self._max_retries = max_retries
        self._min_request_interval = min_request_interval
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._last_request_at: float | None = None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=timeout
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "_PacedClient":
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
            exp = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
            delay = exp + random.uniform(0, exp * 0.25)
        self._sleep_fn(delay)

    def _get_with_retry(self, path: str, params: dict) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            self._respect_rate_limit()
            try:
                response = self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_retries:
                    raise FetchError(
                        f"request to {path} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue

            if response.status_code == 404:
                return response  # not found -- caller handles
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise FetchError(
                        f"request to {path} failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            return response


# --- Mailing list (Pony Mail) -------------------------------------------------


@dataclass(frozen=True)
class MailMessageRef:
    """One dev@/user@ message to fetch text for.

    `message_id` is the RFC 5322 `Message-ID` header value (with angle
    brackets, e.g. `"<abc@mail.gmail.com>"`) -- the same key
    `collectors/ponymail.py` stores in `message.message_id`, so a caller
    sampling from the already-collected Phase 1 metadata never needs a
    second id scheme. `year_month` ("YYYY-MM") is required because Pony
    Mail's month-digest endpoint is fetched per list/month; a caller
    sampling from Phase 1 metadata already has `occurred_at` to derive it.
    """

    list_name: str
    domain: str
    year_month: str
    message_id: str


@dataclass(frozen=True)
class RawMailMessage:
    """One fetched, not-yet-preprocessed mailing-list message."""

    message_id: str
    sender: str | None
    in_reply_to: str | None
    text: str


def _previous_month(year_month: str) -> str:
    year, month = (int(part) for part in year_month.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def _raw_mail_from_record(record: dict) -> RawMailMessage:
    return RawMailMessage(
        message_id=record.get("message-id") or "",
        sender=record.get("from"),
        in_reply_to=record.get("in-reply-to") or None,
        text=record.get("body") or "",
    )


class PonyMailTextFetcher(_PacedClient):
    """Fetches dev@/user@ message bodies on demand, by `MailMessageRef`."""

    source_id = "ponymail_text"

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
        base_url: str = DEFAULT_PONYMAIL_BASE_URL,
    ) -> None:
        super().__init__(
            base_url, transport, max_retries, min_request_interval, sleep_fn, clock, timeout
        )
        # (domain, list_name, year_month) -> {message_id: raw record dict}.
        # Caches whole-month fetches so N refs in the same month cost one
        # request, and so `resolve_parent` can look the parent up locally
        # without a second network round trip when it falls in a month
        # already fetched for one of the caller's refs.
        self._month_cache: dict[tuple[str, str, str], dict[str, dict]] = {}

    def _fetch_month(self, list_name: str, domain: str, year_month: str) -> dict[str, dict]:
        key = (domain, list_name, year_month)
        cached = self._month_cache.get(key)
        if cached is not None:
            return cached
        response = self._get_with_retry(
            "/api/stats.lua", {"list": list_name, "domain": domain, "d": year_month}
        )
        if response.status_code == 404:
            by_message_id: dict[str, dict] = {}
        else:
            payload = response.json()
            by_message_id = {
                record["message-id"]: record
                for record in payload.get("emails", []) or []
                if record.get("message-id")
            }
        self._month_cache[key] = by_message_id
        return by_message_id

    def fetch_messages(self, refs: Iterable[MailMessageRef]) -> dict[str, RawMailMessage]:
        """Fetch raw bodies for exactly the given refs (deduplicated by
        message id). Only the `(list, domain, year_month)` groups actually
        referenced are fetched -- never a full-history crawl. A ref whose
        message id isn't found in its stated month's digest is silently
        omitted from the result (the caller can detect this from a missing
        key)."""
        result: dict[str, RawMailMessage] = {}
        for ref in refs:
            if ref.message_id in result:
                continue
            month = self._fetch_month(ref.list_name, ref.domain, ref.year_month)
            record = month.get(ref.message_id)
            if record is not None:
                result[ref.message_id] = _raw_mail_from_record(record)
        return result

    def resolve_parent(
        self, ref: MailMessageRef, raw: RawMailMessage, lookback_months: int = 1
    ) -> RawMailMessage | None:
        """The in-reply-to message for `raw`, or `None` if it is a thread
        root or its parent can't be found.

        Looks in `ref`'s own month first (the common case), then up to
        `lookback_months` earlier months for the same list (a reply
        occasionally lands in the month after its parent was posted).
        Every month checked is cached, so a batch of refs from the same
        thread/month never re-fetches.
        """
        if not raw.in_reply_to:
            return None

        year_month = ref.year_month
        for _ in range(lookback_months + 1):
            month = self._fetch_month(ref.list_name, ref.domain, year_month)
            record = month.get(raw.in_reply_to)
            if record is not None:
                return _raw_mail_from_record(record)
            year_month = _previous_month(year_month)
        return None


# --- JIRA comments -----------------------------------------------------------


@dataclass(frozen=True)
class JiraCommentRef:
    """One JIRA comment to fetch text for."""

    issue_key: str
    comment_id: str


@dataclass(frozen=True)
class RawJiraComment:
    """One fetched, not-yet-preprocessed JIRA comment."""

    comment_id: str
    issue_key: str
    author: str | None
    created_at: str | None
    text: str


def _raw_jira_comment_from_json(issue_key: str, payload: dict) -> RawJiraComment:
    return RawJiraComment(
        comment_id=str(payload.get("id")),
        issue_key=issue_key,
        author=(payload.get("author") or {}).get("name"),
        created_at=payload.get("created"),
        text=payload.get("body") or "",
    )


class JiraCommentTextFetcher(_PacedClient):
    """Fetches JIRA comment bodies on demand, by `JiraCommentRef`.

    `base_url` is the JIRA instance root (e.g.
    `https://issues.apache.org/jira`, `config.issue_tracker.base_url`).
    """

    source_id = "jira_comment_text"

    def __init__(
        self,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        page_size: int = DEFAULT_JIRA_PAGE_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(
            base_url, transport, max_retries, min_request_interval, sleep_fn, clock, timeout
        )
        self._page_size = page_size
        # issue_key -> comments, ordered ascending by JIRA's default (created
        # order) -- what makes "the previous comment on the same issue"
        # (D18/COMMUNITY-HEALTH.md §4.1's parent rule for JIRA) a local list
        # lookup instead of a second request.
        self._issue_cache: dict[str, list[RawJiraComment]] = {}

    def _fetch_issue_comments(self, issue_key: str) -> list[RawJiraComment]:
        cached = self._issue_cache.get(issue_key)
        if cached is not None:
            return cached
        comments: list[RawJiraComment] = []
        start_at = 0
        while True:
            response = self._get_with_retry(
                f"/rest/api/2/issue/{issue_key}/comment",
                {"startAt": start_at, "maxResults": self._page_size},
            )
            if response.status_code == 404:
                break
            payload = response.json()
            page = payload.get("comments", [])
            comments.extend(_raw_jira_comment_from_json(issue_key, c) for c in page)
            total = payload.get("total", len(page))
            start_at += len(page)
            if not page or start_at >= total:
                break
        self._issue_cache[issue_key] = comments
        return comments

    def fetch_comments(self, refs: Iterable[JiraCommentRef]) -> dict[str, RawJiraComment]:
        """Fetch raw bodies for exactly the given refs. Only the issues
        actually referenced are fetched -- never a blanket issue crawl."""
        result: dict[str, RawJiraComment] = {}
        for ref in refs:
            if ref.comment_id in result:
                continue
            comments = self._fetch_issue_comments(ref.issue_key)
            match = next((c for c in comments if c.comment_id == ref.comment_id), None)
            if match is not None:
                result[ref.comment_id] = match
        return result

    def resolve_parent(self, ref: JiraCommentRef) -> RawJiraComment | None:
        """The comment immediately preceding `ref` on the same issue (by
        JIRA's default, created-ascending order), or `None` if `ref` is the
        issue's first comment or isn't found at all."""
        comments = self._fetch_issue_comments(ref.issue_key)
        index = next((i for i, c in enumerate(comments) if c.comment_id == ref.comment_id), None)
        if index is None or index == 0:
            return None
        return comments[index - 1]


# --- State-shape assembly ----------------------------------------------------


def build_state(message_text: str, message_source: str, parent_text: str | None) -> dict[str, Any]:
    """Assemble the question-set state shape exactly
    (`questions_v1.yaml`'s `state_schema`; COMMUNITY-HEALTH.md §4.1/§4.4):
    ``{"message": {"text", "source"}, "parent": {"text"} | None}``."""
    return {
        "message": {"text": message_text, "source": message_source},
        "parent": {"text": parent_text} if parent_text is not None else None,
    }


def fetch_and_preprocess_mail(
    fetcher: PonyMailTextFetcher,
    refs: Iterable[MailMessageRef],
    automated_sender_patterns: Iterable[Any] = (),
) -> dict[str, dict[str, Any]]:
    """Fetch, filter, and preprocess dev@/user@ messages for `refs`.

    Returns `{message_id: state}` for every ref whose body was found and
    whose sender did not match `automated_sender_patterns` -- an automated
    sender's message is dropped entirely (never fetched into a state object
    at all), per COMMUNITY-HEALTH.md §4.1. A ref not present in the result
    was either not found upstream or dropped as automated.
    """
    refs = list(refs)
    raws = fetcher.fetch_messages(refs)
    result: dict[str, dict[str, Any]] = {}
    for ref in refs:
        raw = raws.get(ref.message_id)
        if raw is None or is_automated_sender(raw.sender, automated_sender_patterns):
            continue
        parent_raw = fetcher.resolve_parent(ref, raw)
        parent_text = (
            preprocess_text(parent_raw.text, "mailing_list") if parent_raw is not None else None
        )
        result[ref.message_id] = build_state(
            preprocess_text(raw.text, "mailing_list"), "mailing_list", parent_text
        )
    return result


def fetch_and_preprocess_jira(
    fetcher: JiraCommentTextFetcher,
    refs: Iterable[JiraCommentRef],
    automated_sender_patterns: Iterable[Any] = (),
) -> dict[str, dict[str, Any]]:
    """Fetch, filter, and preprocess JIRA comments for `refs`. Same
    semantics as `fetch_and_preprocess_mail`, keyed by `comment_id`."""
    refs = list(refs)
    raws = fetcher.fetch_comments(refs)
    result: dict[str, dict[str, Any]] = {}
    for ref in refs:
        raw = raws.get(ref.comment_id)
        if raw is None or is_automated_sender(raw.author, automated_sender_patterns):
            continue
        parent_raw = fetcher.resolve_parent(ref)
        parent_text = (
            preprocess_text(parent_raw.text, "jira_comment") if parent_raw is not None else None
        )
        result[ref.comment_id] = build_state(
            preprocess_text(raw.text, "jira_comment"), "jira_comment", parent_text
        )
    return result
