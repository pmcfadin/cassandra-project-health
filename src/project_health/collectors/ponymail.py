"""Pony Mail (lists.apache.org) mailing-list collector (ARCHITECTURE.md §2.2
`MailingListAdapter`, §4.3; issue #33), **metadata only** (D1/D16).

## Verified endpoint shapes (DATA-SOURCES.md §4, live-checked 2026-09-25)

- ``GET /api/stats.lua?list=<list>&domain=<domain>`` -> 200 JSON,
  ``{firstYear, firstMonth, lastYear, lastMonth, active_months: {...}}`` --
  used here only to discover the list's month range (first/last active
  month), never its per-month counts (the live-run acceptance criterion
  counts messages actually collected via ``mbox.lua``, not this endpoint's
  own numbers).
- ``GET /api/mbox.lua?list=<list>&domain=<domain>&date=<YYYY-MM>`` -> 200,
  ``Content-Type: application/mbox`` -- one month's messages as a standard
  RFC 5322 mbox file. This is the only endpoint this collector reads message
  content from.
- ``thread.lua`` / ``email.lua`` are **not used here** -- DATA-SOURCES.md §4
  flags their single-message/thread-lookup behavior as unverified, and
  Phase 1's metadata-only scope (sender, timestamp, thread structure) is
  fully satisfiable from ``mbox.lua``'s standard ``Message-ID:`` /
  ``In-Reply-To:`` / ``References:`` headers alone (confirmed present on
  real messages, DATA-SOURCES.md §4).

## D1/D16: metadata only -- no message body is ever persisted

``mbox.lua`` returns full RFC 5322 messages, bodies included. This module
parses each message with the standard library ``mailbox``/``email`` parsers,
reads exactly six headers (``Message-ID``, ``From``, ``Date``, ``Subject``,
``In-Reply-To``, ``References``), and discards the parsed
``email.message.Message`` object -- and therefore its body -- before
building any row. The ``Subject`` header itself is also never persisted:
only ``sha256(subject).hexdigest()`` is written (schema/tables.py `MESSAGE`
`subject_hash`), so even the subject line's text never reaches Parquet, a
log line, or a fixture. No function in this module returns, logs, or writes
a message body or subject text anywhere. This is exercised by
``tests/test_ponymail_collector.py``'s
``test_no_body_or_subject_text_reaches_any_output_column``, which scans
every string cell of every column of both output tables (and, separately,
every recorded fixture file) for a substring drawn from a real fixture
message's body/subject.

## Thread reconstruction

Per message, ``thread_id`` is a pure function of that message's own headers
(no cross-message state, so it's stable across incremental, month-bucketed
runs that never hold more than one run's worth of messages in memory at
once): the first ``Message-ID`` listed in ``References:`` if present (RFC
5322 convention -- and the convention JWZ-style threading tools rely on --
puts the thread root first), else the ``In-Reply-To:`` target if present,
else the message's own ``Message-ID`` (it's the thread root itself, so far
as this run can tell). A message whose real root was posted in a month this
run never fetched (e.g. the root predates this collector's watermark) still
gets a consistent, deterministic ``thread_id`` -- just not one this run's
``message_thread`` roll-up can necessarily attach a ``root_message_id`` to
that it has actually seen; downstream thread-level metrics work is
`projects/cassandra.yaml`/#27's concern, not this collector's.

``message_thread`` rows are a per-collection-call roll-up: grouped by
``(list, thread_id)`` over only the messages this ``collect()`` call fetched
(the same scoping `collectors/jira.py`'s ``issue``/``review_event`` tables
use). A thread whose messages span multiple runs/partitions therefore gets
one `message_thread` row per run that saw activity for it; reconciling that
into a single current view across all partitions is a read-time concern
(see ``pipeline.py``'s existing read-time dedupe for `issue`/`review_event`),
not something this module attempts.

## Watermark strategy (ARCHITECTURE.md §4.3)

"Per-list last message epoch fetched via ``stats.lua``... current month is
always re-fetched in full each run; completed months are immutable and
fetched once." Concretely: this collector tracks, per list, the most recent
*completed* month (a "YYYY-MM" string) already fetched. On each run it
fetches every month after that watermark through the list's current active
month (inclusive) -- so a re-run with an unchanged watermark still re-fetches
exactly the current month (catching late-archived messages), and a first run
(``watermark=None``) fetches the list's entire history -- unless a
`max_months_per_list` cap (issue #33 fixup) makes that impractical in one
run (a full dev@ backfill measured ~31 minutes live; nightly's
`timeout-minutes: 60` also has to fit git/JIRA/roster). When capped,
`select_backfill_months` fetches the oldest outstanding completed months up
to the cap, **plus the current month always** (never displaced by the cap),
so a capped run still advances the watermark forward every time and never
stops re-checking the current month while working through a long backlog.
`PonyMailCollectionResult.backfill`/`.partial` report, per list, how many
completed months are still outstanding after this run -- `pipeline.py`
mirrors these into the run manifest's `sources.ponymail.backfill`/`.partial`
so the site/runbook can show "still backfilling" vs. "caught up".

## Retry/backoff and pacing

Same shape as `collectors/jira.py`: exponential backoff with jitter on
429/5xx/timeout/transport errors, capped retries, and a minimum interval
between request starts (default 0.5s = 2 req/s, matching DATA-SOURCES.md
§4's "poll politely, one request at a time, no parallel fan-out" and issue
#33's "≤2 req/s like the JIRA collector").
"""

from __future__ import annotations

import hashlib
import mailbox
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import httpx
import pyarrow as pa

from project_health.collectors.retry import exponential_backoff, is_transient_body_error
from project_health.config import ProjectConfig
from project_health.schema import get_schema, validate

# --- Tuning constants (mirrors collectors/jira.py, issue #33) ---------------

DEFAULT_MAX_RETRIES = 5
# ≤2 req/s politeness cap (DATA-SOURCES.md §4: "poll politely... no parallel
# fan-out"; issue #33: "Retry/backoff + ≤2 req/s like the JIRA collector").
DEFAULT_MIN_REQUEST_INTERVAL = 0.5
DEFAULT_TIMEOUT = 30.0

DEFAULT_BASE_URL = "https://lists.apache.org"

SENDER_RAW_TYPE = "mailing_list_address"

_MSGID_RE = re.compile(r"<[^<>\s]+>")


class CollectionError(Exception):
    """Raised when a Pony Mail request fails after exhausting all retries."""


@dataclass(frozen=True)
class PonyMailCollectionResult:
    """Output of one `PonyMailCollector.collect()` run."""

    messages: pa.Table
    message_threads: pa.Table
    # Per-list next watermark ("YYYY-MM" of the last completed month), or the
    # input watermark unchanged for a list with nothing new. A list with no
    # completed month yet (its whole history is still "this month") keeps
    # its prior watermark (`None` on a first run).
    next_watermarks: dict[str, str | None]
    message_count: int
    thread_count: int
    # Messages seen in a fetched mbox but skipped -- no parseable
    # `Message-ID` or no parseable `Date` header -- and therefore not
    # represented in `messages`/`message_threads` at all. A data-quality
    # signal, not a body/content field.
    skipped_count: int
    # Per-list backfill state (issue #33 fixup): `{"months_remaining": N}`,
    # where N is how many *completed* months are still outstanding after
    # this run (0 once a list is fully caught up). Mirrored into the
    # manifest's `sources.ponymail.backfill` by `pipeline._collect_ponymail`.
    backfill: dict[str, dict[str, int]]
    # True if any configured list still has `months_remaining > 0` after
    # this run -- lets the site/runbook distinguish "still backfilling" from
    # "done" without inspecting every list individually.
    partial: bool


# --- Month-range / watermark helpers ----------------------------------------


def _month_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_range(first_year: int, first_month: int, last_year: int, last_month: int) -> list[str]:
    """Every "YYYY-MM" month from `(first_year, first_month)` to
    `(last_year, last_month)` inclusive, in chronological order."""
    months = []
    y, m = first_year, first_month
    while (y, m) <= (last_year, last_month):
        months.append(_month_str(y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def months_needed(all_months: list[str], watermark: str | None) -> list[str]:
    """Months this run must fetch: everything after `watermark` (or
    everything, if `watermark` is `None`) through the list's last active
    month -- always including that last (current) month even when it equals
    `watermark`'s successor, since the current month is never "completed"
    (ARCHITECTURE.md §4.3)."""
    if watermark is None:
        return list(all_months)
    return [m for m in all_months if m > watermark]


def select_backfill_months(
    needed: list[str], current_month: str | None, max_months_per_list: int | None
) -> tuple[list[str], int]:
    """Decide which of `needed`'s months (oldest-first, from `months_needed`
    -- always ending in `current_month` when one exists) this run actually
    fetches, honoring a per-run cap (issue #33 fixup: an uncapped first-run
    backfill of a long-lived list can take 30+ minutes at the pacing cap,
    which doesn't fit the nightly job's `timeout-minutes: 60` alongside
    git/JIRA/roster).

    `max_months_per_list=None` means unlimited (fetch everything needed, as
    before this fixup) -- returns `(needed, 0)`. Otherwise: backfill is
    oldest-first (each capped run still advances the watermark forward, per
    ARCHITECTURE.md §4.3), and **the current month always gets one of the
    cap's slots**, regardless of backfill state -- it is never "completed"
    and is always re-fetched every run (late-archived messages), so a tight
    cap must not silently stop re-checking it while working through a large
    historical backlog.

    Returns `(fetch_months, months_remaining)`: `fetch_months` is what to
    actually fetch this call, and `months_remaining` is how many *completed*
    months (i.e. excluding `current_month`, which is never "backfill debt")
    are still outstanding after this run -- the manifest's
    `sources.ponymail.backfill.<list>.months_remaining` (0 once a list is
    fully caught up).
    """
    if max_months_per_list is None or len(needed) <= max_months_per_list:
        return list(needed), 0

    completed = [m for m in needed if m != current_month]
    keep = completed[: max(0, max_months_per_list - 1)]
    fetch_months = list(keep)
    if current_month is not None and current_month not in fetch_months:
        fetch_months.append(current_month)
    months_remaining = len(completed) - len(keep)
    return fetch_months, months_remaining


def next_watermark_for(
    fetched_months: list[str], current_month: str | None, previous_watermark: str | None
) -> str | None:
    """The next watermark given the months this run actually fetched.

    A "completed" month is any fetched month other than `current_month`
    (the list's last active month per `stats.lua` -- never "done" until a
    later month exists). This is computed from `fetched_months` (what this
    call actually retrieved, which `max_months_per_list` may have capped
    short of the list's full needed range) rather than the full theoretical
    month range, mirroring `collectors/jira.py`'s `next_watermark` -- "the
    full-precision max(updated) *seen*", not a value implying data that
    wasn't actually collected. If nothing fetched this run was a completed
    month (e.g. only the current month was fetched, or nothing was fetched
    at all), `previous_watermark` is returned unchanged.
    """
    completed = [m for m in fetched_months if m != current_month]
    if not completed:
        return previous_watermark
    return max(completed)


# --- Backoff (mirrors collectors/jira.py) -----------------------------------


# --- Message parsing (D1/D16: headers only, body/subject text never kept) --


def _clean_msgid(value: str | None) -> str | None:
    if not value:
        return None
    match = _MSGID_RE.search(value)
    return match.group(0) if match else value.strip() or None


def _header_str(message: Message, name: str) -> str | None:
    """`message.get(name)`, coerced to a plain `str` (or `None`).

    Python's `email` parser doesn't always hand back a plain `str` for a
    header: a header containing raw, non-MIME-encoded 8-bit bytes (common in
    real-world archive mail going back to 2009 -- confirmed live against a
    real 2021 dev@cassandra.apache.org message during issue #33's live-run
    testing) comes back as an `email.header.Header` instance instead.
    `Header` also defines an `.encode(maxlinelen, splitchars, linesep)`
    method with a completely different signature from `str.encode(encoding,
    errors)` -- calling `str.encode`-style arguments on a `Header` raises a
    confusing `TypeError` deep inside `email.header` (`unsupported operand
    type(s) for -: 'str' and 'int'`) rather than anything mentioning
    encoding. Every header this module reads is coerced through this
    function first specifically to avoid that trap; `str(a_header_instance)`
    is its RFC 2047-folded representation, which is fine here since the
    result is only ever hashed (`subject_hash`) or regex-matched
    (`Message-ID`/`In-Reply-To`/`References`), never displayed.
    """
    value = message.get(name)
    if value is None:
        return None
    return str(value)


def _parse_references(value: str | None) -> list[str] | None:
    if not value:
        return None
    ids = _MSGID_RE.findall(value)
    return ids or None


def _parse_date_header(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_message_meta(message: Message, list_name: str) -> dict | None:
    """Extract exactly the six metadata headers this module ever reads from
    a message, and nothing else -- `message` (and its body) is never
    touched, returned, or stored beyond this function's local scope.

    Returns `None` for a message with no usable `Message-ID` or `Date`
    header (both required, non-nullable columns in schema/tables.py
    `MESSAGE`) -- the caller counts these as `skipped_count` rather than
    writing a row with a synthesized key/timestamp.
    """
    message_id = _clean_msgid(_header_str(message, "Message-ID"))
    if not message_id:
        return None

    occurred_at = _parse_date_header(_header_str(message, "Date"))
    if occurred_at is None:
        return None

    display_name, address = parseaddr(_header_str(message, "From") or "")
    sender_raw_value = (address or "").strip().lower()

    # The Subject header text is read here only long enough to hash it --
    # it is never assigned to a variable that outlives this expression, and
    # this module writes no other reference to it anywhere (D1/D16).
    subject_hash = hashlib.sha256(
        (_header_str(message, "Subject") or "").encode("utf-8", "surrogateescape")
    ).hexdigest()

    references = _parse_references(_header_str(message, "References"))
    in_reply_to = _clean_msgid(_header_str(message, "In-Reply-To"))

    if references:
        thread_id = references[0]
    elif in_reply_to:
        thread_id = in_reply_to
    else:
        thread_id = message_id

    return {
        "message_id": message_id,
        "list": list_name,
        "sender_identity_id": None,
        "sender_raw_type": SENDER_RAW_TYPE,
        "sender_raw_value": sender_raw_value,
        "sender_display_name": display_name or None,
        "occurred_at": occurred_at,
        "subject_hash": subject_hash,
        "in_reply_to": in_reply_to,
        "references": references,
        "thread_id": thread_id,
    }


def iter_mbox_messages(raw_mbox: bytes) -> Iterator[Message]:
    """Yield each `email.message.Message` in a raw mbox byte string.

    The standard library's `mailbox.mbox` parser (not a naive "From "-line
    split) correctly handles ``>From`` body-line escaping, so it's used here
    via a throwaway temp file -- `mailbox.mbox` requires a real path. The
    temp file (and its directory) is removed before this function returns or
    raises, and holds only what `mbox.lua` itself returned -- nothing this
    module writes persists any of it past this call.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        mbox_path = Path(tmpdir) / "month.mbox"
        mbox_path.write_bytes(raw_mbox)
        box = mailbox.mbox(str(mbox_path))
        try:
            yield from box
        finally:
            box.close()


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


def _build_message_thread_rows(message_rows: list[dict], source_snapshot_id: str) -> list[dict]:
    """Roll `message_rows` (this call's fetched messages only -- see module
    docstring's "Thread reconstruction" section) up into one row per
    `(list, thread_id)`."""
    threads: dict[tuple[str, str], dict] = {}
    for row in message_rows:
        key = (row["list"], row["thread_id"])
        info = threads.get(key)
        if info is None:
            threads[key] = {
                "message_ids": {row["message_id"]},
                "started_at": row["occurred_at"],
                "last_activity_at": row["occurred_at"],
            }
            continue
        info["message_ids"].add(row["message_id"])
        if row["occurred_at"] < info["started_at"]:
            info["started_at"] = row["occurred_at"]
        if row["occurred_at"] > info["last_activity_at"]:
            info["last_activity_at"] = row["occurred_at"]

    rows = []
    for (list_name, thread_id), info in threads.items():
        rows.append(
            {
                "thread_id": thread_id,
                "list": list_name,
                "root_message_id": thread_id,
                "started_at": info["started_at"],
                "last_activity_at": info["last_activity_at"],
                "message_count": len(info["message_ids"]),
                "source_snapshot_id": source_snapshot_id,
            }
        )
    return rows


# --- Collector ----------------------------------------------------------


class PonyMailCollector:
    """Pony Mail (lists.apache.org) collector -- M0 subset of
    `MailingListAdapter` (metadata only, D1/D16).

    Reads `domain`/`lists` from `config.mailing_lists`
    (`project_health.config.MailingListsConfig`). Anonymous, unauthenticated
    access (DATA-SOURCES.md §4).
    """

    source_id = "ponymail"

    def __init__(
        self,
        config: ProjectConfig,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        mailing_lists = config.mailing_lists
        domain = getattr(mailing_lists, "domain", None) if mailing_lists else None
        lists = getattr(mailing_lists, "lists", None) if mailing_lists else None
        if not domain or not lists:
            raise ValueError(
                "config.mailing_lists.domain and .lists are required for "
                "PonyMailCollector"
            )

        self._domain = domain
        self._lists = list(lists)

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

    def __enter__(self) -> "PonyMailCollector":
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
            delay = exponential_backoff(attempt)
        self._sleep_fn(delay)

    def _get_with_retry(
        self, path: str, params: dict, *, expect_json: bool = False
    ) -> httpx.Response:
        """GET `path`, retrying timeouts/transport errors/429/5xx with
        backoff. `expect_json=True` (issue #86, `fetch_stats`'s JSON body --
        `fetch_month_mbox`'s raw mbox bytes never pass this) also retries a
        truncated/undecodable body exactly like a 5xx, since a truncated
        JSON response is exactly as transient as one."""
        attempt = 0
        while True:
            attempt += 1
            self._respect_rate_limit()
            try:
                response = self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"Pony Mail request to {path} timed out after {attempt} "
                        f"attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"Pony Mail request to {path} failed after {attempt} "
                        f"attempt(s): {exc}"
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"Pony Mail request to {path} failed after {attempt} "
                        f"attempt(s): HTTP {response.status_code}"
                    )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            if expect_json:
                try:
                    response.json()
                except Exception as exc:
                    if not is_transient_body_error(exc):
                        raise
                    if attempt >= self._max_retries:
                        raise CollectionError(
                            f"Pony Mail request to {path} returned an undecodable "
                            f"response body after {attempt} attempt(s): {exc}"
                        ) from exc
                    self._backoff(attempt, retry_after=None)
                    continue
            return response

    def fetch_stats(self, list_name: str) -> dict:
        """Raw `stats.lua` JSON for `list_name` (used only for its
        `firstYear`/`firstMonth`/`lastYear`/`lastMonth` month-range)."""
        response = self._get_with_retry(
            "/api/stats.lua", {"list": list_name, "domain": self._domain}, expect_json=True
        )
        return response.json()

    def fetch_month_mbox(self, list_name: str, year_month: str) -> bytes:
        """Raw mbox bytes for one `list_name`/`year_month` ("YYYY-MM")."""
        response = self._get_with_retry(
            "/api/mbox.lua",
            {"list": list_name, "domain": self._domain, "date": year_month},
        )
        return response.content

    def collect(
        self,
        watermarks: dict[str, str | None] | None = None,
        snapshot_id: str | None = None,
        max_months_per_list: int | None = None,
    ) -> PonyMailCollectionResult:
        """Fetch every list's needed months and return validated `message` /
        `message_thread` tables plus each list's next watermark.

        `watermarks` maps list name -> last-completed-month string
        ("YYYY-MM") or `None`; a list absent from the mapping is treated the
        same as a first run for that list. `max_months_per_list` caps how
        many of a list's needed months are actually fetched this call
        (`None` = unlimited); backfill within that cap is oldest-first, with
        the current month always fetched regardless -- see
        `select_backfill_months`.
        """
        snapshot_id = snapshot_id or str(uuid.uuid4())
        watermarks = dict(watermarks or {})

        message_rows: list[dict] = []
        next_watermarks: dict[str, str | None] = {}
        backfill: dict[str, dict[str, int]] = {}
        skipped = 0

        for list_name in self._lists:
            stats = self.fetch_stats(list_name)
            all_months = month_range(
                stats["firstYear"], stats["firstMonth"], stats["lastYear"], stats["lastMonth"]
            )
            watermark = watermarks.get(list_name)
            needed = months_needed(all_months, watermark)
            current_month = all_months[-1] if all_months else None
            fetch_months, months_remaining = select_backfill_months(
                needed, current_month, max_months_per_list
            )

            for year_month in fetch_months:
                raw_mbox = self.fetch_month_mbox(list_name, year_month)
                for message in iter_mbox_messages(raw_mbox):
                    meta = _extract_message_meta(message, list_name)
                    if meta is None:
                        skipped += 1
                        continue
                    meta["source_snapshot_id"] = snapshot_id
                    message_rows.append(meta)

            next_watermarks[list_name] = next_watermark_for(fetch_months, current_month, watermark)
            backfill[list_name] = {"months_remaining": months_remaining}

        messages_table = validate("message", _rows_to_table(message_rows, get_schema("message")))
        thread_rows = _build_message_thread_rows(message_rows, snapshot_id)
        threads_table = validate(
            "message_thread", _rows_to_table(thread_rows, get_schema("message_thread"))
        )
        partial = any(info["months_remaining"] > 0 for info in backfill.values())

        return PonyMailCollectionResult(
            messages=messages_table,
            message_threads=threads_table,
            next_watermarks=next_watermarks,
            message_count=len(message_rows),
            thread_count=len(thread_rows),
            skipped_count=skipped,
            backfill=backfill,
            partial=partial,
        )
