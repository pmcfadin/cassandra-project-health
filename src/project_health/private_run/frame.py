"""Sampling frame builders for the private Cassandra communication run
(issue #110; DECISIONS.md D1, D10, D17, D18, D22, D23; COMMUNITY-HEALTH.md
§1.2, §4, §5, §7).

Builds the population of "threads" per (venue, quarter) stratum from the
locally cached Phase 1 raw tables (`project_health.storage.read_table`,
read from a `data` branch checkout) -- never a live crawl. A thread is:

- a dev@ mailing-list thread (`ponymail/message_thread`), assigned to the
  calendar quarter of its `started_at` (root message time). Every message
  in a sampled thread is attributed to that one quarter for sampling *and*
  aggregation purposes, even if the thread's later replies spill into a
  following quarter -- a documented simplification, chosen for symmetry
  with the JIRA rule below (both use "when the conversation started", not
  "when each individual message landed").
- one JIRA issue's comment stream (`jira/issue`), assigned to the calendar
  quarter of the issue's `created_at`. A JIRA issue has no separate
  "thread" concept in this project's schema (`schema/tables.py`'s `ISSUE`
  table has no `started_at`-equivalent), and `created_at` is the closest
  analog to a mailing-list thread's root-message time -- both mark the
  moment the conversation began. This choice (an issue's "quarter" = its
  created quarter, not the quarter of any individual comment) is the one
  the issue itself asks to be documented, and is recorded verbatim in every
  run's `aggregates.json`/`report.md` (`runner.py`'s `frame_definition`).

Only the `dev` list is in scope for v1 (issue #110's "Venues: dev@ mailing
list and JIRA comments... GitHub PR comments are out of scope for v1") --
`user@` is not scanned here even though `projects/cassandra.yaml` collects
it for Phase 1 metadata purposes.

**Every function below de-duplicates by its natural key** (`thread_id`,
`issue_key`, `message_id`) before returning anything (issue #110 fixup
round 2). The raw `ponymail/message`, `ponymail/message_thread`, and
`jira/issue` tables are append-only across nightly collection runs and are
*not* guaranteed unique per key -- verified live against this project's
own `data` branch: 1,129 duplicate `message_id`s, 731 duplicate
`thread_id`s, and 11,763 duplicate `issue_key`s, all from overlapping
partitions written by separate collector runs. Without de-duplication
here, a thread or message with more than one row could be sampled twice
(inflating a stratum's population and, if both copies landed in the same
K-sized sample, its `sampled_ids`) or counted twice in `pending` (silently
double-weighting that message in every downstream rate -- discovered via
issue #112's own "n of N sampled" coverage tracking, which is what first
made the inflated counts visible). This mirrors the "dedupe at the read
site, not a smarter watermark" convention this project's own collectors
already document elsewhere (e.g. `collectors/jira.py`,
`pipeline._dedupe_commit_trailer_review_events`) -- the raw cache is
allowed to carry more than one row per key; every *reader* of it is
expected to collapse that down to one.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import polars as pl

from project_health import storage
from project_health.private_run.quarters import quarter_of


def load_dev_thread_frame(
    data_dir: str | Path, list_name: str, quarters: set[str]
) -> dict[str, list[str]]:
    """`{quarter: [thread_id, ...]}` -- every dev@ thread (`ponymail/
    message_thread`) whose `started_at` falls in one of `quarters`,
    filtered to `list_name` (this project's `"dev"`). Thread ids within a
    quarter are returned in the table's own row order; `private_run.sample`
    is what picks a reproducible subset from them.
    """
    table = storage.read_table(data_dir, "ponymail", "message_thread")
    if table.num_rows == 0:
        return {}
    frame = pl.from_arrow(table).filter(pl.col("list") == list_name)
    frame = frame.unique(subset=["thread_id"], keep="first")

    by_quarter: dict[str, list[str]] = defaultdict(list)
    for row in frame.select(["thread_id", "started_at"]).iter_rows(named=True):
        quarter = quarter_of(row["started_at"])
        if quarter in quarters:
            by_quarter[quarter].append(row["thread_id"])
    return dict(by_quarter)


def load_jira_thread_frame(
    data_dir: str | Path, project_key: str, quarters: set[str]
) -> dict[str, list[str]]:
    """`{quarter: [issue_key, ...]}` -- every CASSANDRA JIRA issue
    (`jira/issue`) whose `created_at` falls in one of `quarters`. An
    issue's own "thread" is its full comment stream (fetched separately at
    classification time, module docstring); this frame only enumerates
    which issues exist per quarter.
    """
    table = storage.read_table(data_dir, "jira", "issue")
    if table.num_rows == 0:
        return {}
    prefix = f"{project_key}-"
    frame = pl.from_arrow(table).filter(pl.col("issue_key").str.starts_with(prefix))
    frame = frame.unique(subset=["issue_key"], keep="first")

    by_quarter: dict[str, list[str]] = defaultdict(list)
    for row in frame.select(["issue_key", "created_at"]).iter_rows(named=True):
        quarter = quarter_of(row["created_at"])
        if quarter in quarters:
            by_quarter[quarter].append(row["issue_key"])
    return dict(by_quarter)


def load_dev_messages_for_threads(
    data_dir: str | Path, list_name: str, thread_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """`{thread_id: [{"message_id", "occurred_at", "sender_raw_value"}, ...]}`
    for exactly `thread_ids` (the sampled threads) -- never a full scan of
    every dev@ message ever collected. Automated-sender filtering here uses
    the local, *unobfuscated* `sender_raw_value` column, more reliable than
    Pony Mail's live text API (which partially obfuscates `From:`,
    `classify/text_fetch.py`'s module docstring) -- the same preference
    `classify/sample.py`'s `load_dev_metadata_frame` documents.
    """
    if not thread_ids:
        return {}
    table = storage.read_table(data_dir, "ponymail", "message")
    if table.num_rows == 0:
        return {}
    frame = pl.from_arrow(table).filter(
        (pl.col("list") == list_name) & pl.col("thread_id").is_in(list(thread_ids))
    )
    frame = frame.unique(subset=["message_id"], keep="first")

    by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cols = ["thread_id", "message_id", "occurred_at", "sender_raw_value"]
    for row in frame.select(cols).iter_rows(named=True):
        by_thread[row["thread_id"]].append(
            {
                "message_id": row["message_id"],
                "occurred_at": row["occurred_at"],
                "sender_raw_value": row["sender_raw_value"],
            }
        )
    return dict(by_thread)
